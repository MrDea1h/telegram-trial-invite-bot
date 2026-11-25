"""SQLite storage: one row per person in `trials`, plus a tiny key-value `settings`.

Times are unix seconds (UTC). The primary key on tg_user_id is what makes the trial
one-time: a person can never own a second row.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    tg_user_id      INTEGER PRIMARY KEY,
    username        TEXT,
    display_name    TEXT NOT NULL,
    status          TEXT NOT NULL,
    requested_at    INTEGER NOT NULL,
    decided_at      INTEGER,
    decided_by      INTEGER,
    invite_link     TEXT,
    link_expires_at INTEGER,
    joined_at       INTEGER,
    expires_at      INTEGER,
    reminded_at     INTEGER,
    removed_at      INTEGER,
    fail_notified   INTEGER NOT NULL DEFAULT 0,
    remind          INTEGER NOT NULL DEFAULT 0,
    extra_invite_link TEXT,
    extra_joined_at INTEGER,
    extra_tagged_at INTEGER,
    followup_sent_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_trials_status_expires ON trials (status, expires_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spins (
    tg_user_id INTEGER PRIMARY KEY,
    prize      TEXT NOT NULL,
    days       INTEGER NOT NULL DEFAULT 0,
    applied    INTEGER NOT NULL DEFAULT 0,
    spun_at    INTEGER NOT NULL
);
"""

# pending  - asked, waiting for an admin
# approved - admin said yes, the person has a link but has not joined yet
# active   - in the chat, the 7 days are running
# expired  - removed when the time ran out
# rejected - admin said no
# left     - left the chat on their own before the end
# revoked  - removed (or un-approved) by an admin
# reset    - an admin allowed a new request; behaves like "no row"
USED_UP = ("expired", "rejected", "left", "revoked")


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # Databases created before newer columns existed need light migrations.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(trials)")}
        if "remind" not in columns:
            self.db.execute("ALTER TABLE trials ADD COLUMN remind INTEGER NOT NULL DEFAULT 0")
        if "extra_invite_link" not in columns:
            self.db.execute("ALTER TABLE trials ADD COLUMN extra_invite_link TEXT")
        if "extra_joined_at" not in columns:
            self.db.execute("ALTER TABLE trials ADD COLUMN extra_joined_at INTEGER")
        if "extra_tagged_at" not in columns:
            self.db.execute("ALTER TABLE trials ADD COLUMN extra_tagged_at INTEGER")
        if "followup_sent_at" not in columns:
            self.db.execute("ALTER TABLE trials ADD COLUMN followup_sent_at INTEGER")
            # Do not retroactively message historical expired trials on migration.
            self.db.execute(
                "UPDATE trials SET followup_sent_at = 0 WHERE status = 'expired'"
            )

    # --- settings -----------------------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- prize wheel --------------------------------------------------------------------

    def get_spin(self, user_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM spins WHERE tg_user_id = ?", (user_id,)).fetchone()

    def add_spin(self, user_id: int, prize: str, days: int, now: int) -> bool:
        """Record a real prize win (replaces any legacy empty-sector row where days < 0)."""
        existing = self.get_spin(user_id)
        if existing is not None and existing["days"] >= 0:
            return False
        self.db.execute(
            "INSERT INTO spins (tg_user_id, prize, days, spun_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (tg_user_id) DO UPDATE SET prize = excluded.prize, "
            "days = excluded.days, applied = 0, spun_at = excluded.spun_at "
            "WHERE spins.days < 0",
            (user_id, prize, days, now),
        )
        return True

    def mark_spin_applied(self, user_id: int) -> None:
        self.db.execute("UPDATE spins SET applied = 1 WHERE tg_user_id = ?", (user_id,))

    def spins(self) -> list[sqlite3.Row]:
        """Every spin with the person's name, oldest first."""
        return self.db.execute(
            "SELECT s.*, t.username, t.display_name FROM spins s "
            "JOIN trials t ON t.tg_user_id = s.tg_user_id ORDER BY s.spun_at"
        ).fetchall()

    # --- trials -------------------------------------------------------------------------

    def get(self, user_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM trials WHERE tg_user_id = ?", (user_id,)).fetchone()

    def find(self, ref: str) -> sqlite3.Row | None:
        """By numeric Telegram id or by @username (as last seen by the bot)."""
        ref = ref.strip()
        if ref.lstrip("-").isdigit():
            return self.get(int(ref))
        return self.db.execute(
            "SELECT * FROM trials WHERE lower(username) = ?", (ref.lstrip("@").lower(),)
        ).fetchone()

    def request(self, user_id: int, username: str | None, name: str, now: int) -> None:
        """New request, or a fresh one over a row an admin has reset."""
        self.db.execute(
            "INSERT INTO trials (tg_user_id, username, display_name, status, requested_at) "
            "VALUES (?, ?, ?, 'pending', ?) "
            "ON CONFLICT (tg_user_id) DO UPDATE SET username = excluded.username, "
            "display_name = excluded.display_name, status = 'pending', "
            "requested_at = excluded.requested_at, decided_at = NULL, decided_by = NULL, "
            "invite_link = NULL, link_expires_at = NULL, joined_at = NULL, expires_at = NULL, "
            "reminded_at = NULL, removed_at = NULL, fail_notified = 0, remind = 0, "
            "extra_invite_link = NULL, extra_joined_at = NULL, extra_tagged_at = NULL, "
            "followup_sent_at = NULL "
            "WHERE trials.status = 'reset'",
            (user_id, username, name, now),
        )

    def move(self, user_id: int, expected: str | tuple[str, ...], **fields) -> bool:
        """Update a row only if it is still in `expected` status. False if someone was first."""
        expected = (expected,) if isinstance(expected, str) else expected
        assignments = ", ".join(f"{column} = ?" for column in fields)
        marks = ", ".join("?" for _ in expected)
        cursor = self.db.execute(
            f"UPDATE trials SET {assignments} WHERE tg_user_id = ? AND status IN ({marks})",
            (*fields.values(), user_id, *expected),
        )
        return cursor.rowcount == 1

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ", ".join("?" for _ in statuses)
        return self.db.execute(
            f"SELECT * FROM trials WHERE status IN ({marks}) "
            "ORDER BY coalesce(expires_at, requested_at)",
            statuses,
        ).fetchall()

    def all_trials(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trials ORDER BY requested_at DESC").fetchall()

    def awaiting_link(self, extra_chat: bool = False) -> list[sqlite3.Row]:
        """Approved people whose join link(s) could not be created yet."""
        if extra_chat:
            return self.db.execute(
                "SELECT * FROM trials WHERE status = 'approved' "
                "AND (invite_link IS NULL OR extra_invite_link IS NULL) "
                "ORDER BY decided_at"
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM trials WHERE status = 'approved' AND invite_link IS NULL "
            "ORDER BY decided_at"
        ).fetchall()

    def due(self, now: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM trials WHERE status = 'active' AND expires_at <= ? ORDER BY expires_at",
            (now,),
        ).fetchall()

    def to_remind(self, now: int, ahead: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM trials WHERE status = 'active' AND reminded_at IS NULL "
            "AND expires_at > ? AND expires_at <= ? ORDER BY expires_at",
            (now, now + ahead),
        ).fetchall()

    def set_extra_joined(self, user_id: int, now: int) -> bool:
        """Record when an approved person joined the 2nd chat (only once per join)."""
        cursor = self.db.execute(
            "UPDATE trials SET extra_joined_at = ?, extra_tagged_at = NULL "
            "WHERE tg_user_id = ? AND status IN ('approved', 'active', 'expired', 'left') "
            "AND (extra_joined_at IS NULL OR extra_tagged_at = 0)",
            (now, user_id),
        )
        return cursor.rowcount == 1

    def set_extra_tagged(self, user_id: int, value: int) -> None:
        """Mark the 2nd-chat tag done (unix timestamp) or skipped (0 = not in chat)."""
        self.db.execute(
            "UPDATE trials SET extra_tagged_at = ? WHERE tg_user_id = ?", (value, user_id)
        )

    def due_extra_tag(self, now: int, after_sec: int) -> list[sqlite3.Row]:
        """Approved people whose 2nd-chat member tag is due."""
        cutoff = now - after_sec
        return self.db.execute(
            "SELECT * FROM trials WHERE extra_tagged_at IS NULL "
            "AND status IN ('approved', 'active', 'expired', 'left') "
            "AND ((extra_joined_at IS NOT NULL AND extra_joined_at <= ?) "
            "OR (extra_joined_at IS NULL AND joined_at IS NOT NULL AND joined_at <= ?)) "
            "ORDER BY coalesce(extra_joined_at, joined_at)",
            (cutoff, cutoff),
        ).fetchall()

    def due_followup(self, now: int, delay_sec: int) -> list[sqlite3.Row]:
        """Expired people who are due for the post-expiration wheel follow-up."""
        cutoff = now - delay_sec
        return self.db.execute(
            "SELECT * FROM trials WHERE status = 'expired' AND followup_sent_at IS NULL "
            "AND removed_at IS NOT NULL AND removed_at <= ? ORDER BY removed_at",
            (cutoff,),
        ).fetchall()

    def set_followup_sent(self, user_id: int, value: int) -> None:
        self.db.execute(
            "UPDATE trials SET followup_sent_at = ? WHERE tg_user_id = ?", (value, user_id)
        )

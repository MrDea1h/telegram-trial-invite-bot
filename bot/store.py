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
    fail_notified   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_trials_status_expires ON trials (status, expires_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
            "reminded_at = NULL, removed_at = NULL, fail_notified = 0 "
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

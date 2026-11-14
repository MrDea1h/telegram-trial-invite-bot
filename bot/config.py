import os
from dataclasses import dataclass

DAY = 86400
HOUR = 3600


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: frozenset[int]
    trial_sec: int = 7 * DAY
    link_ttl_sec: int = 24 * HOUR
    remind_sec: int = 24 * HOUR
    db_path: str = "./data/bot.db"

    @property
    def bot_id(self) -> int:
        # The part of the token before the colon is the bot's own user id.
        return int(self.token.split(":", 1)[0])


def from_env() -> Config:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if ":" not in token:
        raise SystemExit("BOT_TOKEN is not set")
    admins = frozenset(
        int(part) for part in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if part
    )
    if not admins:
        raise SystemExit("ADMIN_IDS is not set")
    return Config(
        token=token,
        admin_ids=admins,
        trial_sec=int(float(os.environ.get("TRIAL_DAYS", "7")) * DAY),
        link_ttl_sec=int(float(os.environ.get("LINK_TTL_HOURS", "24")) * HOUR),
        remind_sec=int(float(os.environ.get("REMIND_HOURS", "24")) * HOUR),
        db_path=os.environ.get("DB_PATH", "./data/bot.db"),
    )

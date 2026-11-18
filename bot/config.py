import os
from dataclasses import dataclass
from pathlib import Path

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
    wheel_logo: str = ""  # picture for the hub of the prize wheel; none if the file is absent
    wheel_brand: str = ""  # a short word on the plaque under the wheel

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
    db_path = os.environ.get("DB_PATH", "./data/bot.db")
    return Config(
        token=token,
        admin_ids=admins,
        trial_sec=int(float(os.environ.get("TRIAL_DAYS", "7")) * DAY),
        link_ttl_sec=int(float(os.environ.get("LINK_TTL_HOURS", "24")) * HOUR),
        remind_sec=int(float(os.environ.get("REMIND_HOURS", "24")) * HOUR),
        db_path=db_path,
        # By default the logo lies next to the database, i.e. on the data volume.
        wheel_logo=os.environ.get("WHEEL_LOGO") or str(Path(db_path).parent / "wheel-logo.png"),
        wheel_brand=os.environ.get("WHEEL_BRAND", "").strip(),
    )

"""Entry point: long polling plus the time-based tick, in one loop."""

import logging
import signal
import time

from bot.config import from_env
from bot.service import ALLOWED_UPDATES, Service
from bot.store import Store
from bot.tg import Api, TgError

log = logging.getLogger("bot")
POLL_SEC = 20


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs request URLs at INFO, and Bot API URLs contain the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = from_env()
    api = Api(cfg.token)
    service = Service(api, Store(cfg.db_path), cfg)

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    me = api.call("getMe")
    api.call("deleteWebhook")  # polling and a webhook cannot coexist
    log.info("started as @%s, chat bound: %s", me.get("username"), service.chat_id is not None)

    offset = None
    while running:
        try:
            updates = api.call(
                "getUpdates", _timeout=POLL_SEC + 10, offset=offset, timeout=POLL_SEC,
                allowed_updates=ALLOWED_UPDATES,
            )  # fmt: skip
        except TgError as exc:
            log.warning("getUpdates failed: %s", exc)
            time.sleep(5)
            updates = []
        for update in updates:
            offset = update["update_id"] + 1
            try:
                service.handle(update)
            except Exception:  # one bad update must not stop the bot
                log.exception("update %s failed", update.get("update_id"))
        try:
            service.tick()
        except Exception:
            log.exception("tick failed")
    api.close()
    log.info("stopped")


if __name__ == "__main__":
    main()

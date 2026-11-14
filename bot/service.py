"""All bot behaviour: requests, admin decisions, joining, reminders and removal.

`handle(update)` reacts to one Telegram update; `tick()` does the time-based work.
Both are safe to repeat: every state change is guarded by the row's current status.
"""

import logging
import time
from datetime import UTC, datetime, timedelta, timezone
from html import escape

from bot.config import DAY, Config
from bot.store import USED_UP, Store
from bot.tg import TgError

log = logging.getLogger("bot")
MSK = timezone(timedelta(hours=3))  # Moscow has no DST

ALLOWED_UPDATES = ["message", "callback_query", "chat_join_request", "chat_member"]

ADMIN_HELP = (
    "<b>Команды</b>\n"
    "/pending — заявки, которые ждут решения\n"
    "/list — кто сейчас в чате и до какого числа\n"
    "/extend <code>id</code> <code>дней</code> — продлить доступ\n"
    "/revoke <code>id</code> — убрать из чата досрочно или отменить одобрение\n"
    "/reset <code>id</code> — разрешить человеку подать заявку заново\n"
    "/link — разовая ссылка в чат без заявки и без срока\n\n"
    "Вместо id можно писать @username.\n"
    "Чат привязывается командой /bind, отправленной в самом чате."
)


def when(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone(MSK).strftime("%d.%m в %H:%M МСК")


def days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def person(row) -> str:
    name = escape(row["display_name"])
    user = f" (@{escape(row['username'])})" if row["username"] else ""
    return f"{name}{user}, id <code>{row['tg_user_id']}</code>"


class Service:
    def __init__(self, api, store: Store, cfg: Config, clock=time.time):
        self.api = api
        self.store = store
        self.cfg = cfg
        self._clock = clock

    def now(self) -> int:
        return int(self._clock())

    @property
    def chat_id(self) -> int | None:
        value = self.store.get_setting("chat_id")
        return int(value) if value else None

    # --- sending ------------------------------------------------------------------------

    def send(self, chat_id: int, text: str, **extra) -> bool:
        """False when the message could not be delivered (e.g. the person blocked the bot)."""
        try:
            self.api.call(
                "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True, **extra,
            )  # fmt: skip
            return True
        except TgError as exc:
            log.warning("sendMessage to %s failed: %s", chat_id, exc)
            return False

    def tell_admins(self, text: str, **extra) -> None:
        for admin_id in sorted(self.cfg.admin_ids):
            self.send(admin_id, text, **extra)

    # --- dispatch -----------------------------------------------------------------------

    def handle(self, update: dict) -> None:
        if message := update.get("message"):
            self.on_message(message)
        elif query := update.get("callback_query"):
            self.on_callback(query)
        elif request := update.get("chat_join_request"):
            self.on_join_request(request)
        elif change := update.get("chat_member"):
            self.on_chat_member(change)

    def on_message(self, message: dict) -> None:
        sender = message.get("from") or {}
        text = (message.get("text") or "").strip()
        if sender.get("is_bot") or "id" not in sender or not text.startswith("/"):
            return
        command, _, rest = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        args = rest.split()
        chat = message["chat"]
        is_admin = sender["id"] in self.cfg.admin_ids

        if chat["type"] != "private":
            if command == "/bind" and is_admin:
                self.bind(chat)
            return
        if not is_admin:
            if command == "/start":
                self.on_start(sender)
            return

        handlers = {
            "/pending": self.cmd_pending,
            "/list": self.cmd_list,
            "/extend": self.cmd_extend,
            "/revoke": self.cmd_revoke,
            "/reset": self.cmd_reset,
            "/link": self.cmd_link,
        }
        if command in handlers:
            handlers[command](sender["id"], args)
        else:
            self.send(sender["id"], ADMIN_HELP)

    # --- binding the chat ---------------------------------------------------------------

    def bind(self, chat: dict) -> None:
        chat_id = chat["id"]
        try:
            me = self.api.call("getChatMember", chat_id=chat_id, user_id=self.cfg.bot_id)
        except TgError as exc:
            self.send(chat_id, f"Не получилось проверить мои права: {escape(exc.description)}")
            return
        missing = []
        if me.get("status") != "administrator":
            missing.append("сделать меня администратором")
        else:
            if not me.get("can_invite_users"):
                missing.append("разрешить «Пригласительные ссылки»")
            if not me.get("can_restrict_members"):
                missing.append("разрешить «Блокировка пользователей»")
        if chat["type"] != "supergroup":
            missing.append("превратить группу в супергруппу (например, включить историю чата "
                           "для новых участников)")  # fmt: skip
        if missing:
            self.send(chat_id, "Привязать чат пока нельзя. Нужно: " + "; ".join(missing) + ".")
            return
        self.store.set_setting("chat_id", str(chat_id))
        self.store.set_setting("chat_title", chat.get("title") or "")
        self.send(chat_id, "Чат привязан. Заявки принимаю в личных сообщениях.")

    # --- a person asks for access -------------------------------------------------------

    def on_start(self, sender: dict) -> None:
        user_id = sender["id"]
        row = self.store.get(user_id)
        status = row["status"] if row else None

        if status == "pending":
            self.send(user_id, "Заявка уже у администратора. Как только он решит — напишу.")
        elif status == "approved":
            self.send_link(self.store.get(user_id))
        elif status == "active":
            self.send(user_id, f"Доступ в чат открыт до {when(row['expires_at'])}.")
        elif status in USED_UP:
            self.send(user_id, "Пробный доступ уже был использован, выдать его повторно нельзя.")
        elif self.chat_id is None:
            self.send(user_id, "Бот ещё настраивается. Попробуйте позже.")
        else:
            name = (
                " ".join(
                    part for part in (sender.get("first_name"), sender.get("last_name")) if part
                )
                or sender.get("username")
                or f"id {user_id}"
            )
            self.store.request(user_id, sender.get("username"), name[:128], self.now())
            row = self.store.get(user_id)
            self.send(
                user_id,
                "Заявка отправлена администратору. Когда её одобрят, я пришлю ссылку в чат. "
                f"Доступ — на {days_word(self.cfg.trial_sec // DAY)} с момента входа.",
            )
            self.tell_admins(
                f"Заявка в чат: {person(row)}",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "Одобрить", "callback_data": f"ok:{user_id}"},
                            {"text": "Отклонить", "callback_data": f"no:{user_id}"},
                        ]
                    ]
                },
            )

    def send_link(self, row) -> bool:
        """Send the join link, making a new one when there is none or it has run out."""
        user_id = row["tg_user_id"]
        now = self.now()
        link = row["invite_link"]
        if not link or (row["link_expires_at"] or 0) <= now + 60:
            expires = now + self.cfg.link_ttl_sec
            try:
                created = self.api.call(
                    "createChatInviteLink", chat_id=self.chat_id, name=f"trial {user_id}"[:32],
                    expire_date=expires, creates_join_request=True,
                )  # fmt: skip
            except TgError as exc:
                log.error("createChatInviteLink failed: %s", exc)
                self.send(user_id, "Не получилось создать ссылку. Попробуйте /start чуть позже.")
                return False
            link = created["invite_link"]
            self.store.move(user_id, "approved", invite_link=link, link_expires_at=expires)
        return self.send(
            user_id,
            "Заявка одобрена. Ссылка в чат (только для вас, действует сутки):\n"
            f"{link}\n\n"
            "Нажмите её и подайте запрос на вступление — я подтвержу его сразу. "
            f"Доступ продлится {days_word(self.cfg.trial_sec // DAY)} с момента входа.",
        )

    # --- an admin decides ---------------------------------------------------------------

    def on_callback(self, query: dict) -> None:
        admin_id = query["from"]["id"]
        action, _, raw_id = (query.get("data") or "").partition(":")
        if admin_id not in self.cfg.admin_ids or action not in ("ok", "no") or not raw_id.isdigit():
            self.answer(query, "Недоступно")
            return
        user_id = int(raw_id)
        now = self.now()
        new_status = "approved" if action == "ok" else "rejected"
        moved = self.store.move(
            user_id, "pending", status=new_status, decided_at=now, decided_by=admin_id
        )
        row = self.store.get(user_id)
        if row is None:
            self.answer(query, "Заявка не найдена")
            return
        if not moved:
            self.answer(query, "Заявка уже обработана")
            self.mark_decided(query, row)
            return

        note = ""
        if action == "ok":
            if not self.send_link(row):
                note = "\nНе смог написать человеку: пусть отправит мне /start, пришлю ссылку."
        else:
            self.send(user_id, "К сожалению, заявку отклонили.")
        self.answer(query, "Одобрено" if action == "ok" else "Отклонено")
        self.mark_decided(query, self.store.get(user_id), note)

    def answer(self, query: dict, text: str) -> None:
        try:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=text)
        except TgError:
            pass

    def mark_decided(self, query: dict, row, note: str = "") -> None:
        """Replace the buttons under the request with its outcome."""
        message = query.get("message") or {}
        if "message_id" not in message:
            return
        outcome = {"approved": "одобрена", "rejected": "отклонена"}.get(row["status"])
        outcome = f"Заявка {outcome}" if outcome else f"Статус: {row['status']}"
        try:
            self.api.call(
                "editMessageText", chat_id=message["chat"]["id"],
                message_id=message["message_id"], parse_mode="HTML",
                text=f"{outcome}: {person(row)}{note}",
            )  # fmt: skip
        except TgError:
            pass

    # --- joining and leaving ------------------------------------------------------------

    def on_join_request(self, request: dict) -> None:
        chat_id = request["chat"]["id"]
        link = request.get("invite_link") or {}
        # Only requests that came through this bot's own links are ours to decide; the chat
        # may have other join-request links handled by people.
        if chat_id != self.chat_id or (link.get("creator") or {}).get("id") != self.cfg.bot_id:
            return
        user_id = request["from"]["id"]
        row = self.store.get(user_id)
        if row is None or row["status"] != "approved":
            # Someone else used a forwarded link, or the approval was revoked.
            self.safe("declineChatJoinRequest", chat_id=chat_id, user_id=user_id)
            return
        try:
            self.api.call("approveChatJoinRequest", chat_id=chat_id, user_id=user_id)
        except TgError as exc:
            log.error("approveChatJoinRequest for %s failed: %s", user_id, exc)
            return
        now = self.now()
        expires = now + self.cfg.trial_sec
        self.store.move(user_id, "approved", status="active", joined_at=now, expires_at=expires)
        if row["invite_link"]:
            self.safe("revokeChatInviteLink", chat_id=chat_id, invite_link=row["invite_link"])
        self.send(user_id, f"Добро пожаловать! Доступ в чат открыт до {when(expires)}.")

    def on_chat_member(self, change: dict) -> None:
        if change["chat"]["id"] != self.chat_id:
            return
        member = change.get("new_chat_member") or {}
        user_id = (member.get("user") or {}).get("id")
        if user_id is None or member.get("status") not in ("left", "kicked"):
            return
        # Our own removals change the status first, so only a real early exit matches here.
        if self.store.move(user_id, "active", status="left", removed_at=self.now()):
            row = self.store.get(user_id)
            self.tell_admins(f"Вышел из чата до конца срока: {person(row)}")

    def safe(self, method: str, **params) -> bool:
        try:
            self.api.call(method, **params)
            return True
        except TgError as exc:
            log.warning("%s failed: %s", method, exc)
            return False

    # --- time-based work ----------------------------------------------------------------

    def tick(self) -> None:
        now = self.now()
        chat_id = self.chat_id
        if chat_id is None:
            return
        for row in self.store.to_remind(now, self.cfg.remind_sec):
            if self.store.move(row["tg_user_id"], "active", reminded_at=now):
                self.send(
                    row["tg_user_id"],
                    f"Напоминание: пробный доступ в чат закончится {when(row['expires_at'])}.",
                )
        for row in self.store.due(now):
            self.remove(row, "expired", now)

    def remove(self, row, new_status: str, now: int) -> bool:
        """Ban the person in the chat (the ban stays: the trial is one-time)."""
        user_id = row["tg_user_id"]
        try:
            self.api.call("banChatMember", chat_id=self.chat_id, user_id=user_id)
        except TgError as exc:
            log.error("banChatMember for %s failed: %s", user_id, exc)
            if not row["fail_notified"]:
                self.store.move(user_id, "active", fail_notified=1)
                self.tell_admins(
                    f"Не могу убрать из чата: {person(row)}\n"
                    f"Причина: {escape(exc.description)}\n"
                    "Буду пробовать дальше; проверьте мои права в чате."
                )
            return False
        self.store.move(user_id, "active", status=new_status, removed_at=now)
        if new_status == "expired":
            self.send(user_id, "Пробный доступ в чат закончился. Спасибо, что были с нами!")
            self.tell_admins(f"Срок вышел, убран из чата: {person(row)}")
        return True

    # --- admin commands -----------------------------------------------------------------

    def target(self, admin_id: int, args: list[str], usage: str):
        if not args:
            self.send(admin_id, f"Формат: {usage}")
            return None
        row = self.store.find(args[0])
        if row is None:
            self.send(admin_id, "Такого человека в заявках нет.")
        return row

    def send_long(self, chat_id: int, title: str, lines: list[str]) -> None:
        chunk = title
        for line in lines:
            if len(chunk) + len(line) > 3500:
                self.send(chat_id, chunk)
                chunk = ""
            chunk += ("\n" if chunk else "") + line
        self.send(chat_id, chunk)

    def cmd_pending(self, admin_id: int, args: list[str]) -> None:
        rows = self.store.by_status("pending")
        if not rows:
            self.send(admin_id, "Заявок, ждущих решения, нет.")
            return
        for row in rows:
            self.send(
                admin_id,
                f"Заявка в чат: {person(row)}",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "Одобрить", "callback_data": f"ok:{row['tg_user_id']}"},
                            {"text": "Отклонить", "callback_data": f"no:{row['tg_user_id']}"},
                        ]
                    ]
                },
            )

    def cmd_list(self, admin_id: int, args: list[str]) -> None:
        active = self.store.by_status("active")
        waiting = self.store.by_status("approved")
        if not active and not waiting:
            self.send(admin_id, "Сейчас в чате никого с пробным доступом нет.")
            return
        lines = [f"• {person(row)} — до {when(row['expires_at'])}" for row in active]
        if waiting:
            lines.append("\n<b>Одобрены, но ещё не вошли</b>")
            lines += [f"• {person(row)}" for row in waiting]
        self.send_long(admin_id, f"<b>В чате: {len(active)}</b>", lines)

    def cmd_extend(self, admin_id: int, args: list[str]) -> None:
        usage = "/extend <code>id</code> <code>дней</code>"
        row = self.target(admin_id, args, usage)
        if row is None:
            return
        if len(args) < 2 or not args[1].isdigit() or not 1 <= int(args[1]) <= 365:
            self.send(admin_id, f"Формат: {usage} — дней от 1 до 365.")
            return
        if row["status"] != "active":
            self.send(admin_id, "Продлить можно только тому, кто сейчас в чате.")
            return
        expires = row["expires_at"] + int(args[1]) * DAY
        # A new reminder is due before the new end date.
        self.store.move(
            row["tg_user_id"], "active", expires_at=expires, reminded_at=None, fail_notified=0
        )
        self.send(admin_id, f"Продлил: {person(row)} — теперь до {when(expires)}.")
        self.send(row["tg_user_id"], f"Доступ в чат продлён до {when(expires)}.")

    def cmd_revoke(self, admin_id: int, args: list[str]) -> None:
        row = self.target(admin_id, args, "/revoke <code>id</code>")
        if row is None:
            return
        user_id, now = row["tg_user_id"], self.now()
        if row["status"] == "active":
            if self.remove(row, "revoked", now):
                self.send(user_id, "Администратор закрыл доступ в чат.")
                self.send(admin_id, f"Убрал из чата: {person(row)}")
            else:
                self.send(admin_id, "Не получилось убрать из чата — проверьте мои права.")
        elif row["status"] in ("pending", "approved"):
            self.store.move(user_id, ("pending", "approved"), status="revoked", removed_at=now)
            if row["invite_link"]:
                self.safe(
                    "revokeChatInviteLink", chat_id=self.chat_id, invite_link=row["invite_link"]
                )
            self.send(admin_id, f"Отменил: {person(row)} — в чат он не попадёт.")
        else:
            self.send(admin_id, "Этот человек сейчас не в чате и заявки у него нет.")

    def cmd_link(self, admin_id: int, args: list[str]) -> None:
        """A link for one plain join: no request, no trial row, so no removal later."""
        if self.chat_id is None:
            self.send(admin_id, "Чат ещё не привязан: отправьте /bind в самом чате.")
            return
        expires = self.now() + self.cfg.link_ttl_sec
        try:
            created = self.api.call(
                "createChatInviteLink", chat_id=self.chat_id, name=f"admin {admin_id}"[:32],
                expire_date=expires, member_limit=1,
            )  # fmt: skip
        except TgError as exc:
            log.error("createChatInviteLink failed: %s", exc)
            self.send(admin_id, f"Не получилось создать ссылку: {escape(exc.description)}")
            return
        self.send(
            admin_id,
            f"Разовая ссылка в чат:\n{created['invite_link']}\n\n"
            f"Сработает один раз, до {when(expires)}. Кто войдёт по ней — остаётся в чате "
            "без срока, заявка не нужна.",
        )

    def cmd_reset(self, admin_id: int, args: list[str]) -> None:
        row = self.target(admin_id, args, "/reset <code>id</code>")
        if row is None:
            return
        if not self.store.move(row["tg_user_id"], USED_UP, status="reset"):
            self.send(admin_id, "Сбросить можно только использованный или отклонённый доступ.")
            return
        if self.chat_id is not None:
            # Lift the ban left by the removal, otherwise a new link would not let them in.
            self.safe(
                "unbanChatMember", chat_id=self.chat_id, user_id=row["tg_user_id"],
                only_if_banned=True,
            )  # fmt: skip
        self.send(admin_id, f"Готово: {person(row)} может снова отправить /start.")

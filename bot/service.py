"""All bot behaviour: requests, admin decisions, joining, reminders and removal.

`handle(update)` reacts to one Telegram update; `tick()` does the time-based work.
Both are safe to repeat: every state change is guarded by the row's current status.
"""

import json
import logging
import random
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from html import escape

from bot import wheel
from bot.config import DAY, Config
from bot.store import USED_UP, Store
from bot.tg import TgError

log = logging.getLogger("bot")
MSK = timezone(timedelta(hours=3))  # Moscow has no DST

ALLOWED_UPDATES = ["message", "callback_query", "chat_join_request", "chat_member"]

LINK_RETRY_SEC = 30  # pause after a failed link creation when Telegram names no delay
UNDELIVERED = "Не смог написать человеку: пусть отправит мне /start, пришлю ссылку."
REMIND_COOLDOWN_SEC = 600  # two admins pressing in a row must not write to everyone twice
REMIND_ALL = "remind_all"  # callback data of the button under /list
EXPORT_TXT = "export_txt"  # callback data of the export-to-txt button under /list
LINK_NOTES = {
    "sent": "",
    "undelivered": "\n" + UNDELIVERED,
    "queued": "\nСсылку пришлю человеку чуть позже: Telegram ограничил частоту создания ссылок.",
}

ADMIN_HELP = (
    "<b>Команды</b>\n"
    "/pending — заявки, которые ждут решения\n"
    "/list — кто сейчас в чате и до какого числа\n"
    "/export — скачать список участников (активных и всех) в .txt\n"
    "/remind — ещё раз прислать ссылку всем, кого одобрили, но кто так и не вошёл "
    "(та же кнопка есть под /list)\n"
    "/extend <code>id</code> <code>дней</code> — продлить доступ\n"
    "/revoke <code>id</code> — убрать из чата досрочно или отменить одобрение\n"
    "/reset <code>id</code> — разрешить человеку подать заявку заново\n"
    "/link — разовая ссылка в чат без заявки и без срока\n"
    "/prizes — призы колеса и кто что выиграл\n"
    "/setprizes — задать призы колеса (без параметров покажу формат)\n\n"
    "Вместо id можно писать @username.\n"
    "Чат привязывается командой /bind, отправленной в самом чате.\n"
    "Второй чат (бессрочный, с тегом спустя неделю) — командой /bind2 в нём."
)


SPIN = "/hesoyam"  # what a person sends to spin the wheel; Telegram commands ignore case
MAX_PRIZES = 24
MAX_LABEL = 30  # the short text on the wheel itself
PRIZES_HELP = (
    "Каждая строка — один приз:\n"
    "<code>/setprizes\n"
    "30 | Стикерпак\n"
    "10 | Неделя в чате | +7\n"
    "1 | Мерч школы | x3</code>\n\n"
    "Первое число — вес: чем он больше, тем чаще приз выпадает.\n"
    "<code>+7</code> — я сам продлю победителю доступ в чат на 7 дней.\n"
    "<code>x3</code> — таких призов всего три, потом он перестанет выпадать.\n"
    "<code>=Коротко</code> — подпись на самом колесе, если название длинное.\n"
    "<code>-</code> — пустой сектор: человеку ничего не полагается.\n"
    "Остальные призы вручает администратор: я напишу, кто что выиграл.\n\n"
    "/setprizes off — выключить колесо."
)


def parse_prizes(text: str) -> tuple[list[dict], str | None]:
    """Prizes from the lines of /setprizes, or the reason the text cannot be read."""
    prizes = []
    for number, line in enumerate((part.strip() for part in text.splitlines()), 1):
        if not line:
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 2 or not fields[0].isdigit() or not 1 <= int(fields[0]) <= 100000:
            return [], f"Строка {number}: в начале нужен вес — целое число от 1, затем «|»."
        if not 1 <= len(fields[1]) <= 60:
            return [], f"Строка {number}: название приза — от 1 до 60 символов."
        prize = {"title": fields[1], "weight": int(fields[0]), "days": 0, "left": None}
        for extra in fields[2:]:
            if match := re.fullmatch(r"\+\s*(\d{1,3})", extra):
                prize["days"] = int(match[1])
            elif match := re.fullmatch(r"[xXхХ×]\s*(\d{1,6})", extra):
                prize["left"] = int(match[1])
            elif extra.startswith("=") and 1 <= len(extra[1:].strip()) <= MAX_LABEL:
                prize["label"] = extra[1:].strip()
            elif extra in ("-", "—", "–"):
                prize["empty"] = True
            elif extra:
                return [], (
                    f"Строка {number}: не понял «{escape(extra)}». Можно +дней, xКоличество, "
                    f"=подпись на колесе (до {MAX_LABEL} символов) или «-» для пустого сектора."
                )
        if prize["days"] > 365 or prize["left"] == 0:
            return [], f"Строка {number}: дней — не больше 365, количество — от 1."
        if prize["days"] and prize.get("empty"):
            return [], f"Строка {number}: пустой сектор не может продлевать доступ."
        prizes.append(prize)
    if not prizes:
        return [], "Не нашёл ни одного приза."
    if len(prizes) > MAX_PRIZES:
        return [], f"Слишком много призов: на колесо помещается не больше {MAX_PRIZES}."
    return prizes, None


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
    def __init__(self, api, store: Store, cfg: Config, clock=time.time, rng=None):
        self.api = api
        self.store = store
        self.cfg = cfg
        self._clock = clock
        self.rng = rng or random.SystemRandom()  # draws the prize
        self.links_after = 0  # no link creation before this moment (rate limit)
        self.extra_tag_warned = False  # warn admins at most once about missing tag rights

    def now(self) -> int:
        return int(self._clock())

    @property
    def chat_id(self) -> int | None:
        value = self.store.get_setting("chat_id")
        return int(value) if value else None

    @property
    def extra_chat_id(self) -> int | None:
        value = self.store.get_setting("extra_chat_id")
        return int(value) if value else self.cfg.extra_chat_id

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
        command, rest = (text.split(maxsplit=1) + [""])[:2]  # a newline ends a command too
        command = command.split("@", 1)[0].lower()
        args = rest.split()
        chat = message["chat"]
        is_admin = sender["id"] in self.cfg.admin_ids

        if chat["type"] != "private":
            if is_admin:
                if command == "/bind":
                    self.bind(chat)
                elif command == "/bind2":
                    self.bind_extra(chat)
            return
        if not is_admin:
            if command == "/start":
                self.on_start(sender)
            elif command == SPIN:
                self.on_spin(sender)
            return
        if command == SPIN:
            self.demo_spin(sender["id"])
            return
        if command == "/setprizes":
            self.cmd_setprizes(sender["id"], rest)
            return

        handlers = {
            "/pending": self.cmd_pending,
            "/list": self.cmd_list,
            "/export": self.cmd_export,
            "/remind": self.cmd_remind,
            "/extend": self.cmd_extend,
            "/revoke": self.cmd_revoke,
            "/reset": self.cmd_reset,
            "/link": self.cmd_link,
            "/prizes": self.cmd_prizes,
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

    def bind_extra(self, chat: dict) -> None:
        chat_id = chat["id"]
        if chat_id == self.chat_id:
            self.send(chat_id, "Этот чат уже привязан как основной.")
            return
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
            if not me.get("can_manage_tags"):
                missing.append("разрешить «Управление тегами»")
        if chat["type"] != "supergroup":
            missing.append("превратить группу в супергруппу (например, включить историю чата "
                           "для новых участников)")  # fmt: skip
        if missing:
            self.send(
                chat_id, "Привязать второй чат пока нельзя. Нужно: " + "; ".join(missing) + "."
            )
            return
        self.store.set_setting("extra_chat_id", str(chat_id))
        self.store.set_setting("extra_chat_title", chat.get("title") or "")
        self.extra_tag_warned = False
        tag_note = (
            f" Спустя {days_word(self.cfg.trial_sec // DAY)} буду ставить участникам тег "
            f"«{escape(self.cfg.extra_chat_tag)}»."
            if self.cfg.extra_chat_tag
            else ""
        )
        self.send(chat_id, f"Второй чат привязан. Из него никого не удаляю.{tag_note}")

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

    def _make_join_link(self, chat_id: int, name: str, expires: int, now: int) -> str | None:
        if now < self.links_after:
            return None
        try:
            created = self.api.call(
                "createChatInviteLink", chat_id=chat_id, name=name[:32],
                expire_date=expires, creates_join_request=True,
            )  # fmt: skip
            return created["invite_link"]
        except TgError as exc:
            self.links_after = now + (exc.retry_after or LINK_RETRY_SEC) + 1
            log.warning("createChatInviteLink for %s failed: %s", chat_id, exc)
            return None

    def send_link(self, row, retrying: bool = False, remind: bool = False) -> str:
        """Send personal join link(s), making new ones when missing or expired.

        Returns "sent", "undelivered" (the person cannot be written to) or "queued": Telegram
        limits how often links are created, so `tick` makes the link later and sends it then.
        `remind` words the message as a reminder to someone who already got a link once.
        """
        user_id = row["tg_user_id"]
        now = self.now()
        extra_chat_id = self.extra_chat_id
        link = row["invite_link"]
        extra_link = row["extra_invite_link"]
        expires = row["link_expires_at"] or 0
        stale = expires <= now + 60
        if stale:
            link = None
            extra_link = None
            expires = now + self.cfg.link_ttl_sec

        need_main = link is None
        need_extra = extra_chat_id is not None and extra_link is None
        if need_main:
            link = self._make_join_link(self.chat_id, f"trial {user_id}", expires, now)
        if need_extra and link is not None:
            extra_link = self._make_join_link(extra_chat_id, f"extra {user_id}", expires, now)

        if link is None or (extra_chat_id is not None and extra_link is None):
            # Save whatever link was created and leave the row for `tick` to finish.
            self.store.move(
                user_id, "approved", invite_link=link, extra_invite_link=extra_link,
                link_expires_at=expires if link else None,
                remind=1 if remind else row["remind"],
            )  # fmt: skip
            if not retrying:
                self.send(
                    user_id,
                    "Заявка одобрена. Ссылку в чат пришлю чуть позже — обычно в течение "
                    "нескольких минут, ничего нажимать не нужно.",
                )
            return "queued"

        if need_main or need_extra:
            self.store.move(
                user_id, "approved", invite_link=link, extra_invite_link=extra_link,
                link_expires_at=expires, remind=0,
            )  # fmt: skip

        days_text = days_word(self.cfg.trial_sec // DAY)
        if extra_link:
            intro = (
                "Напоминаем: заявка одобрена, а вы ещё не вошли в чат. "
                f"Персональные ссылки (только для вас, действуют до {when(expires)}):"
                if remind
                else "Заявка одобрена. Персональные ссылки (только для вас, действуют сутки):"
            )
            text = (
                f"{intro}\n\n"
                f"Основной чат (доступ на {days_text} с момента входа):\n{link}\n\n"
                f"Второй чат (доступ бессрочный):\n{extra_link}\n\n"
                "Нажмите ссылки и подайте запросы на вступление — я подтвержу их сразу."
            )
        else:
            intro = (
                "Напоминаем: заявка одобрена, а вы ещё не вошли в чат. "
                f"Ссылка (только для вас, действует до {when(expires)}):\n"
                if remind
                else "Заявка одобрена. Ссылка в чат (только для вас, действует сутки):\n"
            )
            text = (
                f"{intro}{link}\n\n"
                "Нажмите её и подайте запрос на вступление — я подтвержу его сразу. "
                f"Доступ продлится {days_text} с момента входа."
            )
        delivered = self.send(user_id, text)
        return "sent" if delivered else "undelivered"

    # --- prize wheel --------------------------------------------------------------------

    def prizes(self) -> list[dict]:
        return json.loads(self.store.get_setting("prizes") or "[]")

    def draw(self, prizes: list[dict]) -> int | None:
        """Index of a prize picked by weight among those still in stock."""
        available = [index for index, prize in enumerate(prizes) if prize["left"] != 0]
        if not available:
            return None
        return self.rng.choices(available, weights=[prizes[i]["weight"] for i in available])[0]

    def show_wheel(
        self, chat_id: int, prizes: list[dict], index: int, intro: str, result: str
    ) -> bool:
        """The spinning wheel with the result under a spoiler, or plain text if the picture
        cannot be made or sent. False when the person could not be written to at all."""
        try:
            data = wheel.render(
                [prize.get("label") or prize["title"] for prize in prizes],
                index, self.rng.uniform(-0.35, 0.35),
                logo=self.cfg.wheel_logo, brand=self.cfg.wheel_brand,
            )  # fmt: skip
            self.api.upload(
                "sendAnimation", {"animation": ("wheel.gif", data, "image/gif")},
                chat_id=chat_id, parse_mode="HTML",
                caption=f"{intro}\n<tg-spoiler>{result}</tg-spoiler>",
            )  # fmt: skip
            return True
        except TgError as exc:
            log.warning("sendAnimation to %s failed: %s", chat_id, exc)
        except Exception:  # the prize is drawn already: a broken picture must not lose it
            log.exception("wheel picture failed")
        return self.send(chat_id, f"{intro}\n{result}")

    def on_spin(self, sender: dict) -> None:
        user_id = sender["id"]
        row = self.store.get(user_id)
        if row is None or not row["joined_at"]:
            self.send(user_id, "Этот код срабатывает только у тех, кто вошёл в чат по моей ссылке.")
            return
        if spin := self.store.get_spin(user_id):
            if spin["days"] < 0:
                self.send(user_id, "Чит-код уже использован: в тот раз колесо выпало без приза.")
                return
            self.send(
                user_id, f"Чит-код уже использован. Ваш приз: <b>{escape(spin['prize'])}</b>."
            )
            return
        prizes = self.prizes()
        if not prizes:
            self.send(user_id, "Колесо пока не запущено.")
            return
        index = self.draw(prizes)
        if index is None:
            self.send(user_id, "Призы закончились.")
            return
        prize = prizes[index]
        empty = bool(prize.get("empty"))  # stored as days = -1: nothing to hand over
        days = -1 if empty else prize["days"]
        if not self.store.add_spin(user_id, prize["title"], days, self.now()):
            return
        if prize["left"] is not None:
            prize["left"] -= 1
            self.store.set_setting("prizes", json.dumps(prizes, ensure_ascii=False))

        title = escape(prize["title"])
        outcome = "Администратор напишет вам, чтобы вручить приз."
        admin_note = "Приз нужно вручить."
        if prize["days"] and row["status"] == "active":
            expires = row["expires_at"] + prize["days"] * DAY
            if self.store.move(
                user_id, "active", expires_at=expires, reminded_at=None, fail_notified=0
            ):
                self.store.mark_spin_applied(user_id)
                outcome = f"Доступ в чат продлён до {when(expires)}."
                admin_note = f"Доступ продлил сам — теперь до {when(expires)}."
        elif prize["days"]:
            admin_note = (
                f"Приз — {days_word(prize['days'])} в чате, но человек уже не в чате: "
                "продлить сам не могу, решите вручную."
            )
        result = f"Ваш приз: <b>{title}</b>\n{outcome}"
        if empty:
            result = "В этот раз без приза — так выпало колесо."
        delivered = self.show_wheel(
            user_id, prizes, index, "Чит-код принят. Колесо крутится…", result
        )  # fmt: skip
        if empty:
            return  # admins have nothing to do; the spin is in /prizes
        if not delivered:
            admin_note += "\nНе смог написать человеку о выигрыше."
        self.tell_admins(f"Колесо: {person(row)}\nПриз: <b>{title}</b>\n{admin_note}")

    def demo_spin(self, admin_id: int) -> None:
        """An admin's own spin: shows the wheel, records nothing, takes no prize from stock."""
        prizes = self.prizes()
        index = self.draw(prizes)
        if index is None:
            self.send(admin_id, "Призов нет: задайте их командой /setprizes.")
            return
        self.show_wheel(
            admin_id, prizes, index, "Пробный прокрут: в розыгрыш не записан.",
            f"Выпало: <b>{escape(prizes[index]['title'])}</b>",
        )  # fmt: skip

    def cmd_prizes(self, admin_id: int, args: list[str]) -> None:
        prizes = self.prizes()
        if not prizes:
            self.send(admin_id, "Колесо выключено: призов нет.\n\n" + PRIZES_HELP)
            return
        in_stock = sum(prize["weight"] for prize in prizes if prize["left"] != 0)
        lines = []
        for number, prize in enumerate(prizes, 1):
            if prize["left"] == 0:
                note = "закончился"
            else:
                note = f"шанс {prize['weight'] * 100 / in_stock:.1f}%"
                if prize["left"] is not None:
                    note += f", осталось {prize['left']}"
            if prize["days"]:
                note += f", продлеваю сам на {days_word(prize['days'])}"
            if prize.get("empty"):
                note += ", пустой сектор"
            if prize.get("label"):
                note += f", на колесе «{escape(prize['label'])}»"
            lines.append(f"{number}. {escape(prize['title'])} — {note}")
        spins = self.store.spins()
        lines.append(f"\n<b>Крутили: {len(spins)}</b>")
        for spin in spins:
            done = "выдан" if spin["applied"] else "вручить"
            if spin["days"] < 0:
                done = "без приза"
            lines.append(f"• {person(spin)} — {escape(spin['prize'])} ({done})")
        self.send_long(
            admin_id, "<b>Призы колеса</b> — ученик крутит командой /HESOYAM, один раз", lines
        )

    def cmd_setprizes(self, admin_id: int, text: str) -> None:
        if not text.strip():
            self.send(admin_id, PRIZES_HELP)
            return
        if text.strip().lower() == "off":
            self.store.set_setting("prizes", "[]")
            self.send(admin_id, "Колесо выключено. Уже выигранные призы остались в /prizes.")
            return
        prizes, error = parse_prizes(text)
        if error:
            self.send(admin_id, f"{error}\n\n{PRIZES_HELP}")
            return
        self.store.set_setting("prizes", json.dumps(prizes, ensure_ascii=False))
        self.send(
            admin_id,
            f"Призов на колесе: {len(prizes)}. Проверьте список: /prizes, "
            "пробный прокрут — /HESOYAM.",
        )

    # --- an admin decides ---------------------------------------------------------------

    def on_callback(self, query: dict) -> None:
        admin_id = query["from"]["id"]
        action, _, raw_id = (query.get("data") or "").partition(":")
        if action == EXPORT_TXT and admin_id in self.cfg.admin_ids:
            self.answer(query, "Готовлю файл")
            self.cmd_export(admin_id, [])
            return
        if action == REMIND_ALL and admin_id in self.cfg.admin_ids:
            self.answer(query, "Рассылаю")
            # The count on the button is stale after this press: take the button away.
            message = query.get("message") or {}
            if "message_id" in message:
                self.safe(
                    "editMessageReplyMarkup", chat_id=message["chat"]["id"],
                    message_id=message["message_id"], reply_markup={"inline_keyboard": []},
                )  # fmt: skip
            self.cmd_remind(admin_id, [])
            return
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
            note = LINK_NOTES[self.send_link(row)]
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
        if (link.get("creator") or {}).get("id") != self.cfg.bot_id:
            return
        user_id = request["from"]["id"]
        row = self.store.get(user_id)
        if chat_id == self.extra_chat_id:
            if (
                row is None
                or row["status"] not in ("approved", "active")
                or row["extra_joined_at"] is not None
            ):
                self.safe("declineChatJoinRequest", chat_id=chat_id, user_id=user_id)
                return
            try:
                self.api.call("approveChatJoinRequest", chat_id=chat_id, user_id=user_id)
            except TgError as exc:
                log.error("approveChatJoinRequest (extra) for %s failed: %s", user_id, exc)
                return
            self.store.set_extra_joined(user_id, self.now())
            if row["extra_invite_link"]:
                self.safe(
                    "revokeChatInviteLink", chat_id=chat_id, invite_link=row["extra_invite_link"]
                )
            return
        if chat_id != self.chat_id:
            return
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
        chat_id = change["chat"]["id"]
        member = change.get("new_chat_member") or {}
        user_id = (member.get("user") or {}).get("id")
        if user_id is None:
            return
        if chat_id == self.extra_chat_id:
            if member.get("status") == "member":
                self.store.set_extra_joined(user_id, self.now())
            return
        if chat_id != self.chat_id or member.get("status") not in ("left", "kicked"):
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
        for row in self.store.awaiting_link(extra_chat=self.extra_chat_id is not None):
            if now < self.links_after:
                break
            if self.send_link(row, retrying=True, remind=bool(row["remind"])) == "undelivered":
                self.tell_admins(f"{UNDELIVERED}\n{person(row)}")
        for row in self.store.to_remind(now, self.cfg.remind_sec):
            if self.store.move(row["tg_user_id"], "active", reminded_at=now):
                self.send(
                    row["tg_user_id"],
                    f"Напоминание: пробный доступ в чат закончится {when(row['expires_at'])}.",
                )
        for row in self.store.due(now):
            self.remove(row, "expired", now)
        if self.extra_chat_id is not None and self.cfg.extra_chat_tag:
            for row in self.store.due_extra_tag(now, self.cfg.trial_sec):
                self.tag_extra_member(row, now)

    def tag_extra_member(self, row, now: int) -> bool:
        """Set the post-trial member tag in the 2nd chat without removing the person."""
        user_id = row["tg_user_id"]
        try:
            self.api.call(
                "setChatMemberTag", chat_id=self.extra_chat_id,
                user_id=user_id, tag=self.cfg.extra_chat_tag,
            )  # fmt: skip
        except TgError as exc:
            desc = exc.description.lower()
            if any(
                s in desc
                for s in ("user_not_participant", "user not found", "participant_id_invalid")
            ):
                # Not in the 2nd chat: mark skipped (0) so tick does not retry every 20s;
                # if they join later, on_chat_member resets it.
                self.store.set_extra_tagged(user_id, 0)
            else:
                log.warning("setChatMemberTag for %s failed: %s", user_id, exc)
                if not self.extra_tag_warned and any(
                    s in desc for s in ("right", "admin", "can_manage_tags", "forbidden")
                ):
                    self.extra_tag_warned = True
                    self.tell_admins(
                        "Не могу поставить тег во втором чате: "
                        f"{escape(exc.description)}\n"
                        "Проверьте, что у бота есть право «Управление тегами»."
                    )
            return False
        self.store.set_extra_tagged(user_id, now)
        return True

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

    def send_long(self, chat_id: int, title: str, lines: list[str], **extra) -> None:
        """`extra` (e.g. buttons) goes with the last message."""
        chunk = title
        for line in lines:
            if len(chunk) + len(line) > 3500:
                self.send(chat_id, chunk)
                chunk = ""
            chunk += ("\n" if chunk else "") + line
        self.send(chat_id, chunk, **extra)

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
        export_btn = {"text": "Скачать список (.txt)", "callback_data": EXPORT_TXT}
        if not active and not waiting:
            self.send(
                admin_id,
                "Сейчас в чате никого с пробным доступом нет.",
                reply_markup={"inline_keyboard": [[export_btn]]},
            )
            return
        lines = [f"• {person(row)} — до {when(row['expires_at'])}" for row in active]
        keyboard = []
        if waiting:
            lines.append("\n<b>Одобрены, но ещё не вошли</b>")
            lines += [f"• {person(row)}" for row in waiting]
            keyboard.append(
                [
                    {
                        "text": f"Напомнить не вошедшим ({len(waiting)})",
                        "callback_data": REMIND_ALL,
                    }
                ]
            )
        keyboard.append([export_btn])
        self.send_long(
            admin_id,
            f"<b>В чате: {len(active)}</b>",
            lines,
            reply_markup={"inline_keyboard": keyboard},
        )

    def cmd_export(self, admin_id: int, args: list[str]) -> None:
        """Send a UTF-8 .txt report of active chat members and everyone who interacted."""
        rows = self.store.all_trials()
        now = self.now()
        stamp = datetime.fromtimestamp(now, UTC).astimezone(MSK)
        active = [r for r in rows if r["status"] == "active"]
        in_extra = [r for r in rows if r["extra_joined_at"] is not None]
        approved = [r for r in rows if r["status"] == "approved"]
        pending = [r for r in rows if r["status"] == "pending"]
        other = [r for r in rows if r["status"] not in ("active", "approved", "pending")]

        def fmt_row(row, detail: str = "") -> str:
            uname = f"@{row['username']}" if row["username"] else "-"
            tail = f" | {detail}" if detail else ""
            return f"{row['tg_user_id']} | {uname} | {row['display_name']} | {row['status']}{tail}"

        lines = [
            f"Выгрузка участников бота ({stamp.strftime('%d.%m.%Y %H:%M МСК')})",
            f"Всего взаимодействовало: {len(rows)}",
            f"Активных в основном чате: {len(active)}",
            f"Во втором чате: {len(in_extra)}",
            f"Одобрены, не вошли: {len(approved)}",
            f"Ожидают решения: {len(pending)}",
            "",
            f"=== Активные в основном чате ({len(active)}) ===",
        ]
        for r in active:
            lines.append(fmt_row(r, f"вошёл {when(r['joined_at'])}, до {when(r['expires_at'])}"))
        lines += ["", f"=== Во втором чате ({len(in_extra)}) ==="]
        for r in in_extra:
            tag_state = (
                f"тег поставлен {when(r['extra_tagged_at'])}"
                if r["extra_tagged_at"] and r["extra_tagged_at"] > 0
                else "без тега"
            )
            lines.append(fmt_row(r, f"вошёл {when(r['extra_joined_at'])}, {tag_state}"))
        lines += ["", f"=== Одобрены, но ещё не вошли ({len(approved)}) ==="]
        for r in approved:
            lines.append(fmt_row(r, f"одобрен {when(r['decided_at'])}" if r["decided_at"] else ""))
        lines += ["", f"=== Ожидают решения ({len(pending)}) ==="]
        for r in pending:
            lines.append(fmt_row(r, f"заявка {when(r['requested_at'])}"))
        lines += ["", f"=== Остальные, взаимодействовавшие с ботом ({len(other)}) ==="]
        for r in other:
            ts = r["removed_at"] or r["decided_at"] or r["requested_at"]
            lines.append(fmt_row(r, when(ts)))

        payload = ("\n".join(lines) + "\n").encode("utf-8")
        filename = f"participants-{stamp.strftime('%Y%m%d-%H%M')}.txt"
        caption = (
            f"Участники: активных {len(active)}, во 2-м чате {len(in_extra)}, всего {len(rows)}."
        )
        try:
            self.api.upload(
                "sendDocument",
                {"document": (filename, payload, "text/plain; charset=utf-8")},
                chat_id=admin_id,
                caption=caption,
            )
        except TgError as exc:
            log.warning("sendDocument to %s failed: %s", admin_id, exc)
            self.send(admin_id, f"Не получилось отправить файл: {escape(exc.description)}")

    def cmd_remind(self, admin_id: int, args: list[str]) -> None:
        """Send the join link again to everyone who was approved but has not joined."""
        if self.chat_id is None:
            self.send(admin_id, "Чат ещё не привязан: отправьте /bind в самом чате.")
            return
        rows = self.store.by_status("approved")
        if not rows:
            self.send(admin_id, "Одобренных, но не вошедших в чат, сейчас нет.")
            return
        now = self.now()
        last = int(self.store.get_setting("reminded_all_at") or 0)
        wait = last + REMIND_COOLDOWN_SEC - now
        if wait > 0:
            self.send(
                admin_id,
                f"Напоминание уже рассылали {when(last)}. "
                f"Повторить можно через {-(-wait // 60)} мин.",
            )
            return
        self.store.set_setting("reminded_all_at", str(now))

        sent, queued, undelivered = 0, 0, []
        for row in rows:
            # A link that has run out is made anew; when Telegram limits link creation the
            # row is left to `tick`, which sends the link as soon as it can make one.
            result = self.send_link(row, retrying=True, remind=True)
            if result == "sent":
                sent += 1
            elif result == "queued":
                queued += 1
            else:
                undelivered.append(row)

        lines = []
        if queued:
            lines.append(
                f"Ещё {queued} получат ссылку чуть позже: Telegram ограничил частоту "
                "создания ссылок, дошлю сам."
            )
        if undelivered:
            lines.append(
                f"\n<b>Не смог написать: {len(undelivered)}</b> — пусть отправят мне /start"
            )
            lines += [f"• {person(row)}" for row in undelivered]
        self.send_long(admin_id, f"Напоминание отправлено: {sent} из {len(rows)}.", lines)

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
            if row["extra_invite_link"] and self.extra_chat_id is not None:
                self.safe(
                    "revokeChatInviteLink",
                    chat_id=self.extra_chat_id,
                    invite_link=row["extra_invite_link"],
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
            log.warning("createChatInviteLink failed: %s", exc)
            if exc.retry_after:
                self.links_after = self.now() + exc.retry_after + 1
                self.send(
                    admin_id,
                    "Telegram ограничил частоту создания ссылок. "
                    f"Повторите /link через {exc.retry_after} с.",
                )
            else:
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

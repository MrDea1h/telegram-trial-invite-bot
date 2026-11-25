import pytest

from bot.config import DAY, HOUR, Config
from bot.service import Service
from bot.store import Store
from bot.tg import TgError

BOT_ID = 777
ADMIN, ADMIN2 = 100, 101
CHAT = -1001
EVA = {"id": 5, "first_name": "Ева", "last_name": "Орлова", "username": "eva"}
BOB = {"id": 6, "first_name": "Боб"}


class FakeApi:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, TgError] = {}
        self.links = 0
        self.bot_member = {
            "status": "administrator", "can_invite_users": True, "can_restrict_members": True,
        }  # fmt: skip

    def call(self, method, _timeout=None, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise self.fail[method]
        if method == "createChatInviteLink":
            self.links += 1
            return {"invite_link": f"https://t.me/+link{self.links}"}
        if method == "getChatMember":
            return self.bot_member
        return True

    def upload(self, method, files, **params):
        return self.call(method, files=files, **params)

    def sent(self, chat_id) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "sendMessage" and p["chat_id"] == chat_id]

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def last(self, method) -> dict:
        return next(p for m, p in reversed(self.calls) if m == method)


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    api, clock = FakeApi(), Clock()
    cfg = Config(token=f"{BOT_ID}:secret", admin_ids=frozenset({ADMIN, ADMIN2}), db_path=":memory:")
    service = Service(api, Store(":memory:"), cfg, clock)
    service.handle(group_msg("/bind", ADMIN))
    api.calls.clear()
    return service, api, clock


def private(text, sender):
    sender = sender if isinstance(sender, dict) else {"id": sender, "first_name": "Админ"}
    return {
        "message": {"chat": {"id": sender["id"], "type": "private"}, "from": sender, "text": text}
    }


def group_msg(text, sender_id, chat_type="supergroup"):
    return {
        "message": {
            "chat": {"id": CHAT, "type": chat_type, "title": "Чат"},
            "from": {"id": sender_id, "first_name": "Админ"},
            "text": text,
        }
    }


def press(action, user_id, admin_id=ADMIN):
    return {
        "callback_query": {
            "id": "q1",
            "from": {"id": admin_id},
            "data": f"{action}:{user_id}",
            "message": {"message_id": 9, "chat": {"id": admin_id}},
        }
    }


def join_request(user, creator=BOT_ID, chat=CHAT):
    return {
        "chat_join_request": {
            "chat": {"id": chat},
            "from": user,
            "invite_link": {"invite_link": "https://t.me/+link1", "creator": {"id": creator}},
        }
    }


def member_left(user, status="left"):
    return {
        "chat_member": {"chat": {"id": CHAT}, "new_chat_member": {"user": user, "status": status}}
    }


def admit(service, user=EVA):
    service.handle(private("/start", user))
    service.handle(press("ok", user["id"]))
    service.handle(join_request(user))


def test_full_path_request_approve_join_remind_expire(env):
    service, api, clock = env
    service.handle(private("/start", EVA))
    assert "Заявка отправлена" in api.sent(EVA["id"])[-1]
    # Every admin gets the request with both buttons.
    for admin in (ADMIN, ADMIN2):
        assert "Ева Орлова (@eva)" in api.sent(admin)[-1]
    buttons = api.last("sendMessage")["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["ok:5", "no:5"]

    service.handle(press("ok", EVA["id"]))
    link = api.last("createChatInviteLink")
    assert link["creates_join_request"] is True and link["chat_id"] == CHAT
    assert link["expire_date"] == service.now() + 24 * HOUR
    assert "https://t.me/+link1" in api.sent(EVA["id"])[-1]
    assert "одобрена" in api.last("editMessageText")["text"]

    # The 7 days start when the person actually joins, not when the admin approves.
    clock.t += 5 * HOUR
    service.handle(join_request(EVA))
    assert api.last("approveChatJoinRequest") == {"chat_id": CHAT, "user_id": EVA["id"]}
    row = service.store.get(EVA["id"])
    assert row["status"] == "active" and row["expires_at"] == service.now() + 7 * DAY
    assert "revokeChatInviteLink" in api.methods()

    clock.t += 6 * DAY - 60
    service.tick()
    assert "banChatMember" not in api.methods()
    assert not any("Напоминание" in text for text in api.sent(EVA["id"]))
    clock.t += 120
    service.tick()
    service.tick()
    assert sum("Напоминание" in text for text in api.sent(EVA["id"])) == 1

    clock.t += DAY
    service.tick()
    service.tick()
    assert api.methods().count("banChatMember") == 1
    assert "unbanChatMember" not in api.methods()  # the ban stays: the trial is one-time
    assert service.store.get(EVA["id"])["status"] == "expired"
    assert "закончился" in api.sent(EVA["id"])[-1]
    assert "Срок вышел" in api.sent(ADMIN)[-1]


def test_trial_is_one_time(env):
    service, api, clock = env
    admit(service)
    clock.t += 8 * DAY
    service.tick()
    api.calls.clear()
    service.handle(private("/start", EVA))
    assert "уже был использован" in api.sent(EVA["id"])[-1]
    assert api.sent(ADMIN) == [] and "createChatInviteLink" not in api.methods()


def test_repeated_start_while_waiting_or_inside(env):
    service, api, _ = env
    service.handle(private("/start", EVA))
    service.handle(private("/start", EVA))
    assert "уже у администратора" in api.sent(EVA["id"])[-1]
    assert len(api.sent(ADMIN)) == 1  # the admin is not spammed with duplicates
    service.handle(press("ok", EVA["id"]))
    service.handle(join_request(EVA))
    service.handle(private("/start", EVA))
    assert "открыт до" in api.sent(EVA["id"])[-1]


def test_expired_link_is_reissued_before_joining(env):
    service, api, clock = env
    service.handle(private("/start", EVA))
    service.handle(press("ok", EVA["id"]))
    service.handle(private("/start", EVA))
    assert api.links == 1  # still valid: the same link again
    clock.t += 25 * HOUR
    service.handle(private("/start", EVA))
    assert api.links == 2 and "https://t.me/+link2" in api.sent(EVA["id"])[-1]


def test_rejection(env):
    service, api, _ = env
    service.handle(private("/start", EVA))
    service.handle(press("no", EVA["id"]))
    assert "отклонили" in api.sent(EVA["id"])[-1]
    assert "createChatInviteLink" not in api.methods()
    service.handle(join_request(EVA))
    assert "declineChatJoinRequest" in api.methods()
    service.handle(private("/start", EVA))
    assert "уже был использован" in api.sent(EVA["id"])[-1]


def test_two_admins_press_at_once(env):
    service, api, _ = env
    service.handle(private("/start", EVA))
    service.handle(press("ok", EVA["id"], ADMIN))
    service.handle(press("no", EVA["id"], ADMIN2))
    assert service.store.get(EVA["id"])["status"] == "approved"
    assert api.last("answerCallbackQuery")["text"] == "Заявка уже обработана"
    assert api.links == 1


def test_only_admins_can_decide(env):
    service, api, _ = env
    service.handle(private("/start", EVA))
    service.handle(press("ok", EVA["id"], admin_id=BOB["id"]))
    assert service.store.get(EVA["id"])["status"] == "pending"
    service.handle(private("/list", BOB))
    service.handle(private("/extend 5 3", BOB))
    assert api.sent(BOB["id"]) == []


def test_forwarded_link_does_not_let_a_stranger_in(env):
    service, api, _ = env
    service.handle(private("/start", EVA))
    service.handle(press("ok", EVA["id"]))
    service.handle(join_request(BOB))
    assert api.last("declineChatJoinRequest")["user_id"] == BOB["id"]
    assert "approveChatJoinRequest" not in api.methods()
    assert service.store.get(BOB["id"]) is None


def test_requests_through_other_links_or_chats_are_left_alone(env):
    service, api, _ = env
    service.handle(join_request(BOB, creator=ADMIN))
    service.handle(join_request(BOB, chat=-2002))
    assert api.calls == []


def test_leaving_early_uses_up_the_trial(env):
    service, api, clock = env
    admit(service)
    service.handle(member_left(EVA))
    assert service.store.get(EVA["id"])["status"] == "left"
    assert "Вышел из чата" in api.sent(ADMIN)[-1]
    clock.t += 8 * DAY
    service.tick()
    assert "banChatMember" not in api.methods()
    # A stranger leaving the chat is none of the bot's business.
    service.handle(member_left(BOB))


def test_extend_moves_the_date_and_the_reminder(env):
    service, api, clock = env
    admit(service)
    start = service.now()
    clock.t += 6 * DAY + 60
    service.tick()  # reminder sent
    service.handle(private("/extend @eva 3", ADMIN))
    assert service.store.get(EVA["id"])["expires_at"] == start + 10 * DAY
    assert "продлён" in api.sent(EVA["id"])[-1]
    clock.t += DAY
    service.tick()
    assert "banChatMember" not in api.methods()
    clock.t += 2 * DAY
    service.tick()
    assert sum("Напоминание" in text for text in api.sent(EVA["id"])) == 2
    clock.t += DAY
    service.tick()
    assert service.store.get(EVA["id"])["status"] == "expired"

    for bad in ("/extend", "/extend 5", "/extend 5 0", "/extend 5 abc", "/extend 999 3"):
        service.handle(private(bad, ADMIN))
    assert service.store.get(EVA["id"])["status"] == "expired"


def test_revoke_and_reset(env):
    service, api, _ = env
    admit(service)
    service.handle(private("/revoke 5", ADMIN))
    assert service.store.get(EVA["id"])["status"] == "revoked"
    assert api.last("banChatMember")["user_id"] == EVA["id"]
    service.handle(private("/start", EVA))
    assert "уже был использован" in api.sent(EVA["id"])[-1]

    service.handle(private("/reset 5", ADMIN))
    assert api.last("unbanChatMember") == {
        "chat_id": CHAT, "user_id": EVA["id"], "only_if_banned": True,
    }  # fmt: skip
    service.handle(private("/start", EVA))
    row = service.store.get(EVA["id"])
    assert row["status"] == "pending" and row["expires_at"] is None and row["invite_link"] is None

    # Revoking before the person joined cancels the approval and the link.
    service.handle(press("ok", EVA["id"]))
    service.handle(private("/revoke 5", ADMIN))
    service.handle(join_request(EVA))
    assert api.last("declineChatJoinRequest")["user_id"] == EVA["id"]
    # An active or pending person cannot be "reset".
    service.handle(private("/reset 5", ADMIN))
    admit(service, BOB)
    service.handle(private("/reset 6", ADMIN))
    assert service.store.get(BOB["id"])["status"] == "active"


def test_failed_removal_is_retried_and_reported_once(env):
    service, api, clock = env
    admit(service)
    clock.t += 8 * DAY
    api.fail["banChatMember"] = TgError("banChatMember", 400, "not enough rights")
    service.tick()
    service.tick()
    assert service.store.get(EVA["id"])["status"] == "active"
    assert sum("Не могу убрать" in text for text in api.sent(ADMIN)) == 1
    del api.fail["banChatMember"]
    service.tick()
    assert service.store.get(EVA["id"])["status"] == "expired"


def test_person_who_blocked_the_bot_is_reported_to_the_admin(env):
    service, api, _ = env
    service.handle(private("/start", EVA))
    real_call = api.call

    def call(method, _timeout=None, **params):
        if method == "sendMessage" and params["chat_id"] == EVA["id"]:
            api.calls.append((method, params))
            raise TgError(method, 403, "bot was blocked by the user")
        return real_call(method, _timeout, **params)

    api.call = call
    service.handle(press("ok", EVA["id"]))
    assert service.store.get(EVA["id"])["status"] == "approved"
    assert "Не смог написать" in api.last("editMessageText")["text"]


def test_lists(env):
    service, api, _ = env
    service.handle(private("/list", ADMIN))
    assert "никого" in api.sent(ADMIN)[-1]
    service.handle(private("/pending", ADMIN))
    assert "нет" in api.sent(ADMIN)[-1]
    admit(service)
    service.handle(private("/start", BOB))
    service.handle(private("/pending", ADMIN))
    assert "Боб" in api.sent(ADMIN)[-1] and "reply_markup" in api.last("sendMessage")
    service.handle(private("/list", ADMIN))
    assert "В чате: 1" in api.sent(ADMIN)[-1] and "Ева Орлова" in api.sent(ADMIN)[-1]
    service.handle(private("/whatever", ADMIN))
    assert "Команды" in api.sent(ADMIN)[-1]


def test_admin_link_is_single_use_and_outside_the_trial(env):
    service, api, clock = env
    service.handle(private("/link", ADMIN))
    link = api.last("createChatInviteLink")
    assert link["member_limit"] == 1 and "creates_join_request" not in link
    assert link["chat_id"] == CHAT and link["expire_date"] == service.now() + 24 * HOUR
    assert "https://t.me/+link1" in api.sent(ADMIN)[-1]
    assert api.sent(ADMIN2) == []

    # Whoever joins through it is a plain member: no row, nothing to remove later.
    clock.t += 30 * DAY
    service.tick()
    assert service.store.by_status("active", "approved", "pending") == []
    assert "banChatMember" not in api.methods()

    service.handle(private("/link", BOB))
    assert api.links == 1 and api.sent(BOB["id"]) == []

    api.fail["createChatInviteLink"] = TgError("createChatInviteLink", 400, "no <rights>")
    service.handle(private("/link", ADMIN))
    assert "no &lt;rights&gt;" in api.sent(ADMIN)[-1]


def test_admin_link_needs_a_bound_chat():
    api = FakeApi()
    cfg = Config(token=f"{BOT_ID}:secret", admin_ids=frozenset({ADMIN}))
    service = Service(api, Store(":memory:"), cfg, Clock())
    service.handle(private("/link", ADMIN))
    assert "не привязан" in api.sent(ADMIN)[-1] and "createChatInviteLink" not in api.methods()


def test_rate_limited_links_are_queued_and_sent_later(env):
    service, api, clock = env
    for user in (EVA, BOB):
        service.handle(private("/start", user))
    limit = TgError("createChatInviteLink", 429, "Too Many Requests: retry after 20", 20)
    api.fail["createChatInviteLink"] = limit

    service.handle(press("ok", EVA["id"]))
    assert "чуть позже" in api.sent(EVA["id"])[-1]
    assert "ограничил частоту" in api.last("editMessageText")["text"]
    # While Telegram asks to wait, the bot does not even try.
    attempts = api.methods().count("createChatInviteLink")
    service.handle(press("ok", BOB["id"]))
    service.tick()
    assert api.methods().count("createChatInviteLink") == attempts
    assert "чуть позже" in api.sent(BOB["id"])[-1]

    # Still limited after the pause: nobody is told anything twice.
    clock.t += 22
    service.tick()
    assert sum("чуть позже" in text for text in api.sent(EVA["id"])) == 1

    del api.fail["createChatInviteLink"]
    clock.t += 22
    service.tick()
    service.tick()
    for user in (EVA, BOB):
        assert sum("Ссылка в чат" in text for text in api.sent(user["id"])) == 1
        assert service.store.get(user["id"])["invite_link"]
    service.handle(join_request(EVA))
    assert service.store.get(EVA["id"])["status"] == "active"


def test_queued_link_that_cannot_be_delivered_is_reported(env):
    service, api, clock = env
    service.handle(private("/start", EVA))
    api.fail["createChatInviteLink"] = TgError("createChatInviteLink", 429, "retry after 5", 5)
    service.handle(press("ok", EVA["id"]))
    del api.fail["createChatInviteLink"]
    real_call = api.call

    def call(method, _timeout=None, **params):
        if method == "sendMessage" and params["chat_id"] == EVA["id"]:
            raise TgError(method, 403, "bot was blocked by the user")
        return real_call(method, _timeout, **params)

    api.call = call
    clock.t += 10
    service.tick()
    service.tick()
    assert sum("Не смог написать" in text for text in api.sent(ADMIN)) == 1


def test_bind_requires_rights_and_an_admin():
    api, clock = FakeApi(), Clock()
    cfg = Config(token=f"{BOT_ID}:secret", admin_ids=frozenset({ADMIN}))
    service = Service(api, Store(":memory:"), cfg, clock)

    service.handle(private("/start", EVA))
    assert "настраивается" in api.sent(EVA["id"])[-1]

    service.handle(group_msg("/bind", sender_id=EVA["id"]))
    assert service.chat_id is None and api.sent(CHAT) == []

    api.bot_member = {"status": "administrator", "can_invite_users": True}
    service.handle(group_msg("/bind@some_invite_bot", ADMIN))
    assert service.chat_id is None and "Блокировка" in api.sent(CHAT)[-1]

    api.bot_member["can_restrict_members"] = True
    service.handle(group_msg("/bind", ADMIN, chat_type="group"))
    assert service.chat_id is None and "супергрупп" in api.sent(CHAT)[-1]

    service.handle(group_msg("/bind", ADMIN))
    assert service.chat_id == CHAT


def press_remind(admin_id=ADMIN):
    return {
        "callback_query": {
            "id": "q2",
            "from": {"id": admin_id},
            "data": "remind_all",
            "message": {"message_id": 12, "chat": {"id": admin_id}},
        }
    }


def test_remind_button_writes_only_to_approved_people_who_have_not_joined(env):
    service, api, clock = env
    carl = {"id": 7, "first_name": "Карл"}
    admit(service, BOB)  # inside the chat
    service.handle(private("/start", EVA))
    service.handle(press("ok", EVA["id"]))  # approved, has a link, did not join
    service.handle(private("/start", carl))  # still waiting for a decision
    api.calls.clear()

    service.handle(private("/list", ADMIN))
    button = api.last("sendMessage")["reply_markup"]["inline_keyboard"][0][0]
    assert button == {"text": "Напомнить не вошедшим (1)", "callback_data": "remind_all"}

    clock.t += HOUR
    service.handle(press_remind())
    assert "Напоминаем" in api.sent(EVA["id"])[-1]
    # The link is the one she already has, with its real end time, not "a day".
    assert "https://t.me/+link2" in api.sent(EVA["id"])[-1] and api.links == 2
    assert "действует сутки" not in api.sent(EVA["id"])[-1]
    assert not any("Напоминаем" in text for text in api.sent(BOB["id"]) + api.sent(carl["id"]))
    assert api.sent(ADMIN)[-1] == "Напоминание отправлено: 1 из 1."
    assert api.last("editMessageReplyMarkup")["reply_markup"] == {"inline_keyboard": []}

    # The same link still lets her in.
    service.handle(join_request(EVA))
    assert service.store.get(EVA["id"])["status"] == "active"


def test_remind_has_a_cooldown_shared_by_admins(env):
    service, api, clock = env
    service.handle(private("/start", EVA))
    service.handle(press("ok", EVA["id"]))
    service.handle(private("/remind", ADMIN))
    assert sum("Напоминаем" in text for text in api.sent(EVA["id"])) == 1

    clock.t += 60
    service.handle(press_remind(ADMIN2))
    assert sum("Напоминаем" in text for text in api.sent(EVA["id"])) == 1
    assert "уже рассылали" in api.sent(ADMIN2)[-1] and "через 9 мин" in api.sent(ADMIN2)[-1]

    clock.t += 600
    service.handle(press_remind(ADMIN2))
    assert sum("Напоминаем" in text for text in api.sent(EVA["id"])) == 2


def test_remind_reissues_expired_links_and_queues_them_under_a_rate_limit(env):
    service, api, clock = env
    for user in (EVA, BOB):
        service.handle(private("/start", user))
        service.handle(press("ok", user["id"]))
    clock.t += 2 * DAY  # both links have run out

    service.handle(private("/remind", ADMIN))
    assert "https://t.me/+link3" in api.sent(EVA["id"])[-1]
    assert "https://t.me/+link4" in api.sent(BOB["id"])[-1]
    assert api.sent(ADMIN)[-1] == "Напоминание отправлено: 2 из 2."

    clock.t += 2 * DAY
    api.fail["createChatInviteLink"] = TgError("createChatInviteLink", 429, "retry after 20", 20)
    service.handle(private("/remind", ADMIN))
    assert "Напоминание отправлено: 0 из 2." in api.sent(ADMIN)[-1]
    assert "Ещё 2 получат ссылку чуть позже" in api.sent(ADMIN)[-1]
    # One attempt, then the limit is respected; the people are not told to wait again.
    assert api.methods().count("createChatInviteLink") == 5
    assert not any("чуть позже" in text for text in api.sent(EVA["id"]))

    del api.fail["createChatInviteLink"]
    clock.t += 22
    service.tick()
    for user in (EVA, BOB):
        row = service.store.get(user["id"])
        assert row["invite_link"] and row["remind"] == 0
        # Sent late, but still worded as the reminder it is.
        assert api.sent(user["id"])[-1].startswith("Напоминаем")


def test_remind_reports_people_it_could_not_write_to(env):
    service, api, _ = env
    for user in (EVA, BOB):
        service.handle(private("/start", user))
        service.handle(press("ok", user["id"]))
    real_call = api.call

    def call(method, _timeout=None, **params):
        if method == "sendMessage" and params["chat_id"] == EVA["id"]:
            raise TgError(method, 403, "bot was blocked by the user")
        return real_call(method, _timeout, **params)

    api.call = call
    service.handle(private("/remind", ADMIN))
    report = api.sent(ADMIN)[-1]
    assert report.startswith("Напоминание отправлено: 1 из 2.")
    assert "Не смог написать: 1" in report and "Ева Орлова" in report and "Боб" not in report


def test_remind_is_for_admins_and_needs_someone_to_remind(env):
    service, api, _ = env
    service.handle(private("/remind", ADMIN))
    assert api.sent(ADMIN)[-1] == "Одобренных, но не вошедших в чат, сейчас нет."

    service.handle(private("/start", EVA))
    service.handle(press("ok", EVA["id"]))
    api.calls.clear()
    service.handle(press_remind(admin_id=BOB["id"]))
    assert api.last("answerCallbackQuery")["text"] == "Недоступно"
    assert api.sent(EVA["id"]) == []
    # A person typing the command is not an admin either: it is ignored like any other.
    service.handle(private("/remind", BOB))
    assert api.sent(EVA["id"]) == []


def test_names_are_escaped(env):
    service, api, _ = env
    service.handle(private("/start", {"id": 9, "first_name": "<b>x</b>&"}))
    assert "&lt;b&gt;x&lt;/b&gt;&amp;" in api.sent(ADMIN)[-1]


def test_old_database_gets_the_remind_column(tmp_path):
    import sqlite3

    path = str(tmp_path / "bot.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE trials (tg_user_id INTEGER PRIMARY KEY, username TEXT, "
        "display_name TEXT NOT NULL, status TEXT NOT NULL, requested_at INTEGER NOT NULL, "
        "decided_at INTEGER, decided_by INTEGER, invite_link TEXT, link_expires_at INTEGER, "
        "joined_at INTEGER, expires_at INTEGER, reminded_at INTEGER, removed_at INTEGER, "
        "fail_notified INTEGER NOT NULL DEFAULT 0)"
    )
    old.execute(
        "INSERT INTO trials (tg_user_id, display_name, status, requested_at) "
        "VALUES (5, 'Eva', 'approved', 1)"
    )
    old.commit()
    old.close()
    store = Store(path)
    assert store.get(5)["remind"] == 0 and store.get(5)["status"] == "approved"
    assert store.get(5)["extra_joined_at"] is None and store.get(5)["extra_tagged_at"] is None
    Store(path)  # opening again does not try to add the column twice


EXTRA_CHAT = -1002


def extra_msg(text, sender_id=ADMIN, chat_type="supergroup"):
    return {
        "message": {
            "chat": {"id": EXTRA_CHAT, "type": chat_type, "title": "Чат 2"},
            "from": {"id": sender_id, "first_name": "Админ"},
            "text": text,
        }
    }


def member_event(chat_id, user, status):
    return {
        "chat_member": {
            "chat": {"id": chat_id},
            "new_chat_member": {"user": user, "status": status},
        }
    }


def test_extra_chat_one_time_link_and_week_tag():
    api, clock = FakeApi(), Clock()
    api.bot_member["can_manage_tags"] = True
    cfg = Config(
        token=f"{BOT_ID}:secret",
        admin_ids=frozenset({ADMIN}),
        db_path=":memory:",
        extra_chat_tag="прошла неделя",
    )
    service = Service(api, Store(":memory:"), cfg, clock)
    service.handle(group_msg("/bind", ADMIN))
    # /bind2 refuses binding the same chat as the main chat.
    service.handle(group_msg("/bind2", ADMIN))
    assert "уже привязан как основной" in api.sent(CHAT)[-1]

    service.handle(extra_msg("/bind2", ADMIN))
    assert service.extra_chat_id == EXTRA_CHAT
    assert "Второй чат привязан" in api.sent(EXTRA_CHAT)[-1]
    api.calls.clear()

    # Approval creates two personal join-request links (one per chat) and sends both.
    service.handle(private("/start", EVA))
    service.handle(press("ok", EVA["id"]))
    msg = api.sent(EVA["id"])[-1]
    assert "https://t.me/+link1" in msg
    assert "https://t.me/+link2" in msg
    assert "Основной чат" in msg and "Второй чат" in msg

    # Stranger trying Eva's 2nd-chat link is declined.
    service.handle(join_request(BOB, chat=EXTRA_CHAT))
    assert api.last("declineChatJoinRequest") == {"chat_id": EXTRA_CHAT, "user_id": BOB["id"]}

    # Eva joins both chats via her personal join-request links; 2nd link is revoked on join.
    service.handle(join_request(EVA))
    service.handle(join_request(EVA, chat=EXTRA_CHAT))
    assert api.last("revokeChatInviteLink") == {
        "chat_id": EXTRA_CHAT,
        "invite_link": "https://t.me/+link2",
    }
    clock.t += 6 * DAY
    service.tick()
    assert "setChatMemberTag" not in api.methods()

    # After 7 days: main chat bans Eva, 2nd chat sets the tag without banning.
    clock.t += 1 * DAY
    service.tick()
    tag_call = api.last("setChatMemberTag")
    assert tag_call == {
        "chat_id": EXTRA_CHAT,
        "user_id": EVA["id"],
        "tag": "прошла неделя",
    }
    bans = [p["chat_id"] for m, p in api.calls if m == "banChatMember"]
    assert bans == [CHAT]

    # Subsequent ticks do not re-tag.
    api.calls.clear()
    service.tick()
    assert "setChatMemberTag" not in api.methods()


def test_extra_chat_bind2_checks_tag_rights():
    api, clock = FakeApi(), Clock()
    cfg = Config(token=f"{BOT_ID}:secret", admin_ids=frozenset({ADMIN}), db_path=":memory:")
    service = Service(api, Store(":memory:"), cfg, clock)
    service.handle(extra_msg("/bind2", ADMIN))
    assert service.extra_chat_id is None
    assert "Управление тегами" in api.sent(EXTRA_CHAT)[-1]


def test_extra_chat_tag_skipped_if_not_participant_and_warns_once_on_rights():
    api, clock = FakeApi(), Clock()
    cfg = Config(
        token=f"{BOT_ID}:secret",
        admin_ids=frozenset({ADMIN}),
        db_path=":memory:",
        extra_chat_id=EXTRA_CHAT,
        extra_chat_tag="прошла неделя",
    )
    service = Service(api, Store(":memory:"), cfg, clock)
    service.handle(group_msg("/bind", ADMIN))
    admit(service, EVA)
    admit(service, BOB)
    clock.t += 7 * DAY

    # Not a participant -> marked skipped (0), no admin warning, no infinite retries.
    api.fail["setChatMemberTag"] = TgError(
        "setChatMemberTag", 400, "Bad Request: USER_NOT_PARTICIPANT"
    )
    service.tick()
    assert service.store.get(EVA["id"])["extra_tagged_at"] == 0
    assert service.store.get(BOB["id"])["extra_tagged_at"] == 0
    api.calls.clear()
    service.tick()
    assert "setChatMemberTag" not in api.methods()

    # If Eva joins the 2nd chat later, her join resets extra_tagged_at and tags after 7 days.
    del api.fail["setChatMemberTag"]
    service.handle(member_event(EXTRA_CHAT, EVA, "member"))
    assert service.store.get(EVA["id"])["extra_tagged_at"] is None
    clock.t += 7 * DAY
    service.tick()
    assert api.last("setChatMemberTag")["user_id"] == EVA["id"]


def test_export_txt_command_and_button():
    api, clock = FakeApi(), Clock()
    api.bot_member["can_manage_tags"] = True
    cfg = Config(
        token=f"{BOT_ID}:secret",
        admin_ids=frozenset({ADMIN}),
        db_path=":memory:",
        extra_chat_id=EXTRA_CHAT,
        extra_chat_tag="прошла неделя",
    )
    service = Service(api, Store(":memory:"), cfg, clock)
    service.handle(group_msg("/bind", ADMIN))
    # Eva is active in both chats, Bob is rejected.
    service.handle(private("/start", EVA))
    service.handle(press("ok", EVA["id"]))
    service.handle(join_request(EVA))
    service.handle(join_request(EVA, chat=EXTRA_CHAT))
    service.handle(private("/start", BOB))
    service.handle(press("no", BOB["id"]))
    api.calls.clear()

    # /list includes the export button.
    service.handle(private("/list", ADMIN))
    kb = api.last("sendMessage")["reply_markup"]["inline_keyboard"]
    assert any(btn["callback_data"] == "export_txt" for row in kb for btn in row)

    # /export uploads a UTF-8 .txt document containing both Eva and Bob.
    service.handle(private("/export", ADMIN))
    doc = api.last("sendDocument")
    assert doc["chat_id"] == ADMIN
    filename, payload, mime = doc["files"]["document"]
    assert filename.endswith(".txt") and "text/plain" in mime
    text = payload.decode("utf-8")
    assert "Активные в основном чате (1)" in text
    assert "Во втором чате (1)" in text
    assert "Остальные, взаимодействовавшие с ботом (1)" in text
    assert "@eva" in text and "Боб" in text

    # Clicking the inline button triggers export for admin and is blocked for non-admin.
    api.calls.clear()
    service.handle(
        {
            "callback_query": {
                "id": "q_exp",
                "from": {"id": ADMIN},
                "data": "export_txt",
                "message": {"message_id": 10, "chat": {"id": ADMIN}},
            }
        }
    )
    assert "sendDocument" in api.methods()

    api.calls.clear()
    service.handle(
        {
            "callback_query": {
                "id": "q_exp2",
                "from": {"id": BOB["id"]},
                "data": "export_txt",
                "message": {"message_id": 10, "chat": {"id": BOB["id"]}},
            }
        }
    )
    assert "sendDocument" not in api.methods()


def test_custom_expired_text():
    api, clock = FakeApi(), Clock()
    cfg = Config(
        token=f"{BOT_ID}:secret",
        admin_ids=frozenset({ADMIN}),
        db_path=":memory:",
        expired_text="Line 1\n\nLine 2",
    )
    service = Service(api, Store(":memory:"), cfg, clock)
    service.handle(group_msg("/bind", ADMIN))
    admit(service, EVA)
    clock.t += 7 * DAY
    service.tick()
    assert api.sent(EVA["id"])[-1] == "Line 1\n\nLine 2"


def test_empty_spin_allows_retry_until_win():
    class FixedRng:
        def __init__(self, seq):
            self.seq = iter(seq)

        def choices(self, population, weights):
            return [next(self.seq)]

        def uniform(self, a, b):
            return 0.0

    api, clock = FakeApi(), Clock()
    cfg = Config(token=f"{BOT_ID}:secret", admin_ids=frozenset({ADMIN}), db_path=":memory:")
    service = Service(api, Store(":memory:"), cfg, clock, rng=FixedRng([0, 1]))
    service.handle(group_msg("/bind", ADMIN))
    service.handle(private("/setprizes\n50 | Без приза | -\n50 | Скидка 1000 р", ADMIN))
    admit(service, EVA)
    api.calls.clear()

    # First spin lands on empty: no spin locked, user invited to retry, admins not bothered.
    service.handle(private("/HESOYAM", EVA))
    assert service.store.get_spin(EVA["id"]) is None
    assert "ещё попытка" in api.last("sendAnimation")["caption"]
    assert api.sent(ADMIN) == []

    # Second spin lands on real prize: locked in DB and admins notified.
    service.handle(private("/HESOYAM", EVA))
    assert service.store.get_spin(EVA["id"])["prize"] == "Скидка 1000 р"
    assert any("Скидка 1000 р" in m for m in api.sent(ADMIN))

    # Third spin is blocked because a real prize was already won.
    api.calls.clear()
    service.handle(private("/HESOYAM", EVA))
    assert "уже использован" in api.sent(EVA["id"])[-1]


def test_expired_followup_after_one_hour():
    api, clock = FakeApi(), Clock()
    cfg = Config(token=f"{BOT_ID}:secret", admin_ids=frozenset({ADMIN}), db_path=":memory:")
    service = Service(api, Store(":memory:"), cfg, clock)
    service.handle(group_msg("/bind", ADMIN))
    service.handle(private("/setprizes\n100 | Скидка 1000 р", ADMIN))
    admit(service, EVA)
    admit(service, BOB)
    # Bob wins a prize before expiring; Eva does not spin yet.
    service.handle(private("/HESOYAM", BOB))
    clock.t += 7 * DAY
    service.tick()  # both expire

    # Before 1 hour: no follow-up sent yet.
    api.calls.clear()
    clock.t += 59 * 60
    service.tick()
    assert api.sent(EVA["id"]) == [] and api.sent(BOB["id"]) == []

    # At 1 hour: Eva gets wheel offer, Bob gets reminder about his won discount.
    clock.t += 60
    service.tick()
    assert "/HESOYAM" in api.sent(EVA["id"])[-1]
    assert "персональную скидку" in api.sent(EVA["id"])[-1]
    assert "напомнить про твою персональную скидку" in api.sent(BOB["id"])[-1]

    # Subsequent ticks do not re-send follow-up.
    api.calls.clear()
    clock.t += HOUR
    service.tick()
    assert api.sent(EVA["id"]) == [] and api.sent(BOB["id"]) == []


def test_scheduled_batch_followup_for_historical_expired():
    api, clock = FakeApi(), Clock()
    cfg = Config(token=f"{BOT_ID}:secret", admin_ids=frozenset({ADMIN}), db_path=":memory:")
    service = Service(api, Store(":memory:"), cfg, clock)
    service.handle(group_msg("/bind", ADMIN))
    service.handle(private("/setprizes\n100 | Скидка 1000 р", ADMIN))
    admit(service, EVA)
    admit(service, BOB)
    service.handle(private("/HESOYAM", BOB))
    clock.t += 7 * DAY
    service.tick()
    # Simulate historical rows marked followup_sent_at = 0.
    service.store.set_followup_sent(EVA["id"], 0)
    service.store.set_followup_sent(BOB["id"], 0)
    target_ts = int(clock.t) + 2 * HOUR
    service.store.set_setting("batch_followup_at", str(target_ts))

    api.calls.clear()
    clock.t += HOUR
    service.tick()
    assert api.sent(EVA["id"]) == [] and api.sent(BOB["id"]) == []

    clock.t += HOUR
    service.tick()
    assert "/HESOYAM" in api.sent(EVA["id"])[-1]
    assert "напомнить про твою персональную скидку" in api.sent(BOB["id"])[-1]
    assert "Отложенная рассылка по колесу завершена: доставлено 2 из 2." in api.sent(ADMIN)[-1]
    assert service.store.get_setting("batch_followup_at") == "0"

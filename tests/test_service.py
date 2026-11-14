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


def test_names_are_escaped(env):
    service, api, _ = env
    service.handle(private("/start", {"id": 9, "first_name": "<b>x</b>&"}))
    assert "&lt;b&gt;x&lt;/b&gt;&amp;" in api.sent(ADMIN)[-1]

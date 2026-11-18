import io
import json

import pytest

from bot import wheel
from bot.config import DAY
from bot.service import parse_prizes
from bot.tg import TgError
from tests.test_service import ADMIN, ADMIN2, BOB, EVA, admit, env, private  # noqa: F401

PRIZES = "/setprizes\n30 | Стикерпак\n10 | Неделя в чате | +7\n1 | Мерч школы | x1"


class Pick:
    """Stands in for the random source: always takes the prize with the given title."""

    def __init__(self, service, title):
        self.service, self.title = service, title

    def choices(self, population, weights):
        prizes = self.service.prizes()
        return [next(i for i in population if prizes[i]["title"] == self.title)]

    def uniform(self, low, high):
        return 0.0


@pytest.fixture
def wheel_env(env, monkeypatch):  # noqa: F811
    service, api, clock = env
    monkeypatch.setattr(wheel, "render", lambda *args, **kwargs: b"GIF")
    service.handle(private(PRIZES, ADMIN))
    api.calls.clear()
    return service, api, clock


def spin(service, user, title="Стикерпак", text="/HESOYAM"):
    service.rng = Pick(service, title)
    service.handle(private(text, user))


# --- the picture --------------------------------------------------------------------------


def test_wheel_stops_with_the_winner_under_the_pointer():
    for count in (1, 2, 5, 7, 24):
        total = wheel.sectors(count)
        assert total >= wheel.MIN_SECTORS and total % count == 0
        for winner in range(count):
            for offset in (-0.35, 0.0, 0.35):
                turn = wheel.stop_angle(count, winner, offset)
                assert turn >= 360 * wheel.TURNS
                # Where the top of the picture (270 degrees) points on the unturned wheel.
                under_pointer = (270 - turn) % 360
                assert int(under_pointer // (360 / total)) == winner


def test_render_makes_an_animated_gif():
    from PIL import Image

    data = wheel.render(["Стикерпак", "Неделя в чате", "A very long prize title that is cut"], 1)
    image = Image.open(io.BytesIO(data))
    assert image.format == "GIF" and image.n_frames == wheel.FRAMES + len(wheel.BLINK_MS)
    assert image.size == (wheel.WIDTH, wheel.HEIGHT)
    assert len(data) < 4_000_000


def test_render_with_branding(tmp_path):
    from PIL import Image

    logo = tmp_path / "logo.png"
    Image.new("RGB", (300, 200), (10, 200, 30)).save(logo)
    plain = wheel.render(["A", "B"], 0)
    branded = wheel.render(["A", "B"], 0, logo=str(logo), brand="Школа")
    missing = wheel.render(["A", "B"], 0, logo=str(tmp_path / "none.png"))
    assert branded != plain and missing == plain  # an absent logo file is not an error
    hub = Image.open(io.BytesIO(branded)).convert("RGB").getpixel(wheel.CENTER)
    assert hub[1] > 150 and hub[0] < 80  # the logo's green is in the middle of the wheel


# --- prizes -------------------------------------------------------------------------------


def test_parse_prizes():
    prizes, error = parse_prizes("30 | Стикерпак\n\n10|Неделя в чате|+7\n1 | Мерч | х3 | +2")
    assert error is None
    assert prizes == [
        {"title": "Стикерпак", "weight": 30, "days": 0, "left": None},
        {"title": "Неделя в чате", "weight": 10, "days": 7, "left": None},
        {"title": "Мерч", "weight": 1, "days": 2, "left": 3},  # Cyrillic «х» works as well
    ]
    for bad in ("Стикерпак", "0 | Приз", "5 |", "5 | Приз | много", "5 | Приз | x0", ""):
        assert parse_prizes(bad)[1], bad
    assert parse_prizes("\n".join(f"1 | Приз {n}" for n in range(25)))[1]


def test_admin_sets_and_sees_prizes(wheel_env):
    service, api, _ = wheel_env
    service.handle(private("/prizes", ADMIN))
    text = api.sent(ADMIN)[-1]
    assert "1. Стикерпак — шанс 73.2%" in text
    assert "продлеваю сам на 7 дней" in text and "осталось 1" in text
    assert "Крутили: 0" in text

    service.handle(private("/setprizes", ADMIN))
    assert "Каждая строка" in api.sent(ADMIN)[-1]
    service.handle(private("/setprizes\nбез веса", ADMIN))
    assert api.sent(ADMIN)[-1].startswith("Строка 1")
    assert len(service.prizes()) == 3  # a bad list replaces nothing

    service.handle(private("/setprizes off", ADMIN))
    assert service.prizes() == []
    service.handle(private("/prizes", ADMIN))
    assert "Колесо выключено" in api.sent(ADMIN)[-1]


def test_only_admins_set_prizes(wheel_env):
    service, api, _ = wheel_env
    service.handle(private("/setprizes off", EVA))
    service.handle(private("/prizes", EVA))
    assert len(service.prizes()) == 3 and api.calls == []


# --- spinning -----------------------------------------------------------------------------


def test_spin_is_for_those_who_joined(wheel_env):
    service, api, _ = wheel_env
    spin(service, BOB)  # never asked
    service.handle(private("/start", EVA))
    spin(service, EVA)  # asked, not in the chat yet
    for user in (BOB, EVA):
        assert "только у тех, кто вошёл в чат" in api.sent(user["id"])[-1]
    assert "sendAnimation" not in api.methods() and service.store.spins() == []


def test_spin_gives_a_prize_once(wheel_env):
    service, api, _ = wheel_env
    admit(service)
    api.calls.clear()
    spin(service, EVA, text="/hesoyam")

    sent = api.last("sendAnimation")
    assert sent["chat_id"] == EVA["id"] and sent["files"]["animation"][1] == b"GIF"
    assert "<tg-spoiler>Ваш приз: <b>Стикерпак</b>" in sent["caption"]
    assert "Администратор напишет" in sent["caption"]
    for admin in (ADMIN, ADMIN2):
        assert "Приз: <b>Стикерпак</b>\nПриз нужно вручить." in api.sent(admin)[-1]

    api.calls.clear()
    spin(service, EVA, "Мерч школы")
    assert api.sent(EVA["id"]) == ["Чит-код уже использован. Ваш приз: <b>Стикерпак</b>."]
    assert "sendAnimation" not in api.methods() and api.sent(ADMIN) == []
    assert [row["prize"] for row in service.store.spins()] == ["Стикерпак"]


def test_days_prize_extends_access(wheel_env):
    service, api, _ = wheel_env
    admit(service)
    before = service.store.get(EVA["id"])["expires_at"]
    spin(service, EVA, "Неделя в чате")
    assert service.store.get(EVA["id"])["expires_at"] == before + 7 * DAY
    assert "Доступ в чат продлён до" in api.last("sendAnimation")["caption"]
    assert "Доступ продлил сам" in api.sent(ADMIN)[-1]
    assert service.store.get_spin(EVA["id"])["applied"] == 1
    service.handle(private("/prizes", ADMIN))
    assert "Неделя в чате (выдан)" in api.sent(ADMIN)[-1]


def test_days_prize_after_leaving_goes_to_admins(wheel_env):
    service, api, clock = wheel_env
    admit(service)
    clock.t += 8 * DAY
    service.tick()  # removed: the trial ran out
    spin(service, EVA, "Неделя в чате")
    assert service.store.get(EVA["id"])["status"] == "expired"
    assert "Администратор напишет" in api.last("sendAnimation")["caption"]
    assert "человек уже не в чате" in api.sent(ADMIN)[-1]
    assert service.store.get_spin(EVA["id"])["applied"] == 0


def test_limited_prize_runs_out(wheel_env):
    service, api, _ = wheel_env
    service.handle(private("/setprizes\n1 | Мерч школы | x1", ADMIN))
    admit(service, EVA)
    admit(service, BOB)
    spin(service, EVA, "Мерч школы")
    assert json.loads(service.store.get_setting("prizes"))[0]["left"] == 0
    api.calls.clear()
    spin(service, BOB, "Мерч школы")
    assert api.sent(BOB["id"]) == ["Призы закончились."]
    assert service.store.get_spin(BOB["id"]) is None  # the spin is not spent
    service.handle(private("/prizes", ADMIN))
    assert "закончился" in api.sent(ADMIN)[-1]


def test_label_and_empty_sector(wheel_env, monkeypatch):
    service, api, _ = wheel_env
    prizes, error = parse_prizes("5 | Долгое название приза | =Коротко | x2\n1 | Без приза | -")
    assert error is None
    assert prizes[0]["label"] == "Коротко" and prizes[1]["empty"] is True
    assert parse_prizes("1 | Пусто | - | +7")[1] and parse_prizes("1 | Приз | =")[1]

    seen = []
    monkeypatch.setattr(wheel, "render", lambda titles, *a, **k: seen.append(titles) or b"GIF")
    service.handle(private("/setprizes\n5 | Долгое название | =Коротко\n1 | Без приза | -", ADMIN))
    admit(service)
    api.calls.clear()
    spin(service, EVA, "Без приза")
    assert seen == [["Коротко", "Без приза"]]
    caption = api.last("sendAnimation")["caption"]
    assert "без приза" in caption and "Администратор" not in caption
    assert api.sent(ADMIN) == [] and api.sent(ADMIN2) == []  # nothing to hand over

    spin(service, EVA, "Долгое название")
    assert api.sent(EVA["id"])[-1].endswith("колесо выпало без приза.")
    service.handle(private("/prizes", ADMIN))
    text = api.sent(ADMIN)[-1]
    assert "пустой сектор" in text and "на колесе «Коротко»" in text and "(без приза)" in text


def test_no_prizes_means_no_wheel(wheel_env):
    service, api, _ = wheel_env
    service.handle(private("/setprizes off", ADMIN))
    admit(service)
    spin(service, EVA)
    assert api.sent(EVA["id"])[-1] == "Колесо пока не запущено."
    assert service.store.get_spin(EVA["id"]) is None


def test_broken_picture_still_tells_the_prize(wheel_env, monkeypatch):
    service, api, _ = wheel_env
    admit(service, EVA)
    admit(service, BOB)

    def broken(*args, **kwargs):
        raise RuntimeError("no font")

    monkeypatch.setattr(wheel, "render", broken)
    spin(service, EVA)
    assert "Ваш приз: <b>Стикерпак</b>" in api.sent(EVA["id"])[-1]
    assert "tg-spoiler" not in api.sent(EVA["id"])[-1]

    monkeypatch.setattr(wheel, "render", lambda *args, **kwargs: b"GIF")
    api.fail["sendAnimation"] = TgError("sendAnimation", 400, "bad file")
    spin(service, BOB)
    assert "Ваш приз: <b>Стикерпак</b>" in api.sent(BOB["id"])[-1]
    assert service.store.get_spin(BOB["id"])["prize"] == "Стикерпак"


def test_admin_spin_is_a_trial_run(wheel_env):
    service, api, _ = wheel_env
    spin(service, ADMIN, "Мерч школы")
    caption = api.last("sendAnimation")["caption"]
    assert "Пробный прокрут" in caption and "Мерч школы" in caption
    assert service.store.spins() == []
    assert service.prizes()[2]["left"] == 1  # nothing is taken from stock

    service.handle(private("/setprizes off", ADMIN))
    spin(service, ADMIN)
    assert "Призов нет" in api.sent(ADMIN)[-1]

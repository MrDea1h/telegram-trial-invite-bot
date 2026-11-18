# telegram-trial-invite-bot

A small Telegram bot that gives people one-time, time-limited access to a single group chat. A person asks the bot, an admin approves with one button, the bot lets the person in for a set number of days and then removes them by itself.

The bot's messages are in Russian; all of them live in `bot/service.py` and are easy to translate.

## How it works

1. A person sends the bot `/start`. Every admin gets the request with **Approve** / **Reject** buttons.
2. After approval the bot sends the person a personal invite link (join-request link, valid for 24 hours).
3. The person asks to join through the link. The bot approves the join request only for the person the link was issued to; anyone else using a forwarded link is declined.
4. The trial period (7 days by default) starts at the moment of joining. A day before the end the bot reminds the person; at the end it removes them from the chat and leaves the ban in place, so the trial cannot be repeated.
5. A second `/start` after a used trial is refused until an admin resets it.

Join requests made through links the bot did not create are left alone, so the chat can keep its own invite links and human-approved requests.

## Admin commands

Sent to the bot in a private chat, by users listed in `ADMIN_IDS`.

| Command | What it does |
|---|---|
| `/pending` | requests waiting for a decision, each with its buttons |
| `/list` | who is in the chat on a trial and until when; under it, a button that reminds everyone who was approved but has not joined |
| `/remind` | the same reminder as a command: sends the join link again (a fresh one if the old link has run out) to every approved person who has not joined; at most once in 10 minutes |
| `/extend <id> <days>` | extend someone's access |
| `/revoke <id>` | remove from the chat early, or cancel an approval |
| `/reset <id>` | allow a new request (lifts the ban in the chat) |
| `/link` | a single-use invite link (one join, valid 24 hours) with no request and no time limit |
| `/prizes` | the prize wheel: prizes with their chances and stock, and who won what |
| `/setprizes` | set the prizes of the wheel (see below); `/setprizes off` turns the wheel off |

`@username` works in place of `<id>`.

## Prize wheel

A person who has joined the chat through the bot's link can send the bot `/HESOYAM` once. The bot draws a prize, stores it, and answers with a GIF of a wheel that stops on that prize; after the stop the winning sector stays lit and the rest goes dim; the prize is also written under a spoiler in the caption. The draw happens on the server before the picture is made, so the picture decides nothing. Admins get a message with the winner and the prize. An admin's own `/HESOYAM` is a trial run: nothing is stored.

Prizes are set by an admin, one per line:

```
/setprizes
30 | Sticker pack
10 | A week in the chat | +7
1 | School merch | x3
```

The first number is the weight: the larger it is, the more often the prize comes up. `+7` makes the bot extend the winner's access by 7 days itself (if the person has already left the chat, the admins are asked to decide). `x3` limits the prize to three winners. Every other prize is handed over by an admin. With no prizes set the wheel is off.
Two more optional fields: `=Short text` is what the wheel itself shows when the title is long, and `-` marks an empty sector — the person gets nothing and admins are not pinged.

Branding is optional: put a square picture named `wheel-logo.png` next to the database (on the data volume) to show it in the wheel's hub, and set `WHEEL_BRAND` to a short word for the plaque under the wheel. The bundled font is Geologica (SIL Open Font License, see `bot/assets/Geologica-OFL.txt`).

## Setting up the chat

1. The chat must be a supergroup.
2. Add the bot as an administrator with the **Invite users via link** and **Ban users** rights.
3. An admin from `ADMIN_IDS` sends `/bind` in the chat. The bot checks its rights and remembers the chat.

One bot instance serves one chat.

## Configuration

Copy `.env.example` to `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | token from BotFather |
| `ADMIN_IDS` | — | comma-separated Telegram user ids of the people who approve requests |
| `TRIAL_DAYS` | `7` | how long access lasts, counted from joining |
| `LINK_TTL_HOURS` | `24` | how long an invite link works |
| `REMIND_HOURS` | `24` | how long before the end the person is reminded |
| `DB_PATH` | `./data/bot.db` | SQLite file (`/data/bot.db` in the container) |
| `WHEEL_BRAND` | — | a short word on the plaque under the prize wheel |
| `WHEEL_LOGO` | `wheel-logo.png` next to the database | picture for the wheel's hub |

## Running

```bash
cp .env.example .env      # fill in BOT_TOKEN and ADMIN_IDS
docker compose up -d --build
docker compose logs -f bot
```

Locally:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m bot.main
```

## Design

One process: long polling (no domain, webhook or inbound port needed) and the time-based work in the same loop. State is a SQLite file on a volume; deadlines are stored there, so a restart loses nothing. Every state change is guarded by the row's current status, which makes repeated updates and two admins pressing at once safe.

```
bot/
├─ main.py      # loop: getUpdates → handle → tick
├─ service.py   # all behaviour: requests, decisions, joining, reminders, removal, wheel, commands
├─ wheel.py     # the prize wheel picture (GIF, made with Pillow)
├─ store.py     # SQLite: `trials` (one row per person), `spins` and `settings`
├─ tg.py        # minimal Bot API client
└─ config.py    # settings from the environment
```

Request statuses: `pending` → `approved` → `active` → `expired`, plus `rejected`, `left` (left early), `revoked` (removed by an admin) and `reset` (an admin allowed a new request).

## License

[MIT](LICENSE)

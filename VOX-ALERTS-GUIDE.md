# VOX Alerts: everything about this bot

Written 2026-11-03 for Omar. It contains **no passwords, tokens or webhook links** on purpose. Keep secrets in a password manager (or in GitHub/Cloudflare secrets, where they already live).

---

## 1. What it does, in plain English

It watches **VOX Cinemas Egypt (Almaza)** for you and your friends. When something you care about changes, it sends a message on **Telegram** (to each person) and **Discord** (one shared channel).

It never needs your computer or phone to be on. Everything runs for free in the cloud, with no card.

Things it can tell people:

| Kind of message | When it is sent |
|---|---|
| **Movie alert** (per watch) | A movie someone is watching gets showtimes that match their day / time / cinema / screen. |
| **Heads-up** | The movie has new dates, but the day someone asked for isn't out yet. |
| **"What's out right now"** | When a watch is added (optional) or you press *Send now*. |
| **New dates** (news, everyone who has it on) | VOX adds new days to its calendar for movies already in cinemas. |
| **Movie opened** (news) | A movie that had no showtimes gets its first ones. (Note: this means *showtimes appeared*, not necessarily *tickets on sale*. The wording is misleading and is on the to-do list.) |
| **New in Coming Soon** (news) | VOX adds a movie to its Coming Soon list. |
| **Daily roundup** (news) | One summary a day (default 12:00 Cairo time). |
| **Replies** | Friend presses Start; /status; "I booked it" / "Stop this one" buttons; failure warnings. |

The Discord channel gets **every** movie automatically; you don't tick it per movie.

---

## 2. The parts and how they connect

```
Cloudflare Worker (timer, every 10 min)
        │  calls GitHub's "run workflow" API
        ▼
GitHub Actions: workflow vox-alerts.yml
        │  runs:  python -m alerts
        ▼
 reads config.json (what to watch, who gets what)
 reads alerts-state.json (the bot's memory)
 fetches VOX pages  ──►  decides what is new  ──►  Telegram + Discord
        │
        ▼
 commits updated alerts-state.json, movies.json, posters/ back to the repo
        ▲
GitHub Pages panel (index.html) edits config.json through the GitHub API
```

- **Repo:** your fork `OMAR6972/vox_cinema` (cloned at `/home/claude/vox_cinema`). It was originally someone else's single-purpose scraper (`moghazy17/vox_cinema`); the new `alerts/` engine sits beside it. The original `check_showtimes.py` is still used for its page-fetching code.
- **Panel:** https://omar6972.github.io/vox_cinema/ (a single `index.html` on GitHub Pages).
- **Timer:** a Cloudflare Worker you created in the Cloudflare dashboard. GitHub's own schedule was unreliable (it skipped runs for hours), so Cloudflare pokes GitHub every 10 minutes instead. It needs a GitHub token with *Actions: Read and write* only, stored as a Worker secret.
- **Cost:** free. Each run takes roughly 30–50 seconds of free Actions time.

---

## 3. The files

| File / folder | What it is |
|---|---|
| `config.json` | **What to watch and who gets it.** Settings, people, watches. The panel edits this. |
| `alerts-state.json` | **The bot's memory.** What it has seen and sent, so nothing repeats. Committed back after runs. |
| `movies.json` | List of current movies (title, poster, cinemas, screen types, bot username) for the panel to show. |
| `posters/` | Small copies of movie posters (kept in the repo; deleted when a movie leaves VOX). |
| `index.html` | The panel (light blue and white design, poster cards). |
| `alerts/__main__.py` | Entry point. Reads secrets from the environment, runs the engine, writes files. |
| `alerts/engine.py` | The brain: fetch, remember, match, send, handle Telegram taps. |
| `alerts/matcher.py` | Decides which showtimes match a watch (day, time ± flex, cinema, screen). |
| `alerts/formatter.py` | Writes the message text. |
| `alerts/vox.py` | Reads VOX's web pages. |
| `alerts/vtime.py` | Cairo time and date helpers. |
| `alerts/tg.py`, `alerts/discord.py` | Sending to Telegram and Discord. |
| `alerts/posters.py` | Downloads and cleans up poster copies. |
| `.github/workflows/vox-alerts.yml` | The job GitHub runs. Has a manual "test" option. |
| `scheduler/` | The Cloudflare Worker code (you deployed an equivalent through the dashboard). |
| `tests/test_alerts.py` | 15 automated tests for the engine. |
| `check_showtimes.py`, `watches.json`, `state.json` | The **original** scraper that came with the repo; kept for its fetch code. One of its old tests is expected to fail; leave it. |
| `CLAUDE.md`, `GLOSSARY.md`, `docs/` | Notes for developers and AI helpers. |

Secrets are **not** in the repo. They live in GitHub repo secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL`. The Cloudflare Worker holds its own GitHub token. The panel keeps its token only in your browser (you paste it once).

---

## 4. How a check works (one run)

1. **Handle Telegram taps first.** It reads new Telegram messages: someone pressed Start from an invite link, typed /status, or tapped *I booked it* / *Stop this one*. (This is why replies only come at the next run.)
2. **Crawl VOX.** It loads every date page, the Coming Soon list, and posters (about 16 page loads). It uses `curl_cffi` pretending to be Chrome, because VOX blocks normal scrapers.
3. **Remember.** For every showing (movie + cinema + date + time + screen) it records when it **first saw** it.
4. **Match each watch** against the showings.
5. **Send** only what is new to that watch, to its people (and Discord).
6. **Broadcast news** (new dates, movie opened, coming soon, roundup) to people with those switches on.
7. **Save memory** and commit it back to the repo.

Important behaviours:
- **First run = baseline.** The first time, it records everything silently so you aren't flooded.
- **Editing a watch** (cinema, screen, sold-out option) re-baselines it silently, so changing a filter doesn't re-send old times.
- **Each watch has its own "seen" notebook**, so a time is only announced once per watch.
- **"Bookable" vs not:** on VOX a showing can be listed but not on sale yet (it shows as not bookable). A full house can still have a booking link, so "bookable" doesn't mean seats are left.
- **Failures:** if VOX checks fail several times in a row you get a warning, and a "working again" message later.

### How "matching" works for a watch
- You can pick days (a date, or weekdays), a time with **± flex** (e.g. around 8:00 PM ± 1 h), cinemas, screen types (Standard, IMAX, 4DX, GOLD and so on), and whether to include sold-out/not-bookable times.
- **Modes:** *match* (only matching times), *closest* (nearest times), *all* (whole day).
- Order of preference: exact match within the flex window, then closest times, then a "not yet" heads-up about other dates.

---

## 5. The panel (what each part is for)

- **Movies tab:** poster cards. Each has an **On/Paused** switch, **Edit**, and **Send now**.
  - *Paused* means the bot ignores this watch (nothing sent, nothing deleted).
- **Editor (six steps):** days, time ± flex, cinema, screen type, who gets alerts, extras (include sold-out, only my days, and "tell me what's out now" for brand-new watches).
- **People tab:** one card per person (you, each friend, the Discord channel). Each card has the four news switches (new dates, movie opened, coming soon, daily roundup) and an **invite link** for friends.
- **Settings:** the "new" window in hours (default 6), and the roundup time (default 12:00).
- **Bottom bar:** **Save & check now** (saves and immediately runs the bot) or **Save only** (waits for the next 10-minute check).
- **Status pill:** shows whether the last run worked.
- It saves by editing `config.json` in GitHub, using a token you paste into the panel once (stored only in your browser).

---

## 6. Adding a friend

1. In People, add a person and open **Invite link**; copy it (or send via WhatsApp).
2. **Press Save first.** The link only works after it has been saved to `config.json`.
3. The friend opens the link in Telegram and taps **Start**.
4. They are connected at the **next run** (or press **Save & check now** to force it). They then get a "you're connected" message.
5. Choose which movies and which news they get.

A friend who taps Start before you saved gets "ask for an invite link". Just save and have them tap again.

---

## 7. What "new" means (your definition)

The bot records, for every showing, the time it **first saw** it. A showing counts as **new** if it was first seen within the "new" window (default **6 hours**, adjustable in Settings). Showings that were already there when tracking started have no exact first-seen time and are never marked new.

This is the rule behind the 🆕 marker. *Current limitation:* the messages don't yet explain it clearly enough (see to-do).

---

## 8. Running, testing, fixing

- **Run now:** Actions → "VOX Alerts" → Run workflow, or press *Save & check now* in the panel.
- **Test message:** the workflow's test option sends a "VOX Alerts test" message to confirm delivery.
- **Tests:** `pip install -r requirements.txt pytest` then `pytest -q`.
- **Where to look when alerts stop:** (1) is the Cloudflare timer firing (Worker logs/triggers), (2) did the latest Actions run succeed, (3) VOX may have changed its page layout, so the parser needs updating against a saved page in `tests/fixtures_alerts/`.
- **If Telegram messages stop:** the chat ID may have changed (supergroup upgrade). Re-fetch it with `get_chat_id.py` and update the secret.
- **Duplicates:** the workflow always checks out the latest `main` so queued runs don't resend. Rare duplicates can still occur if both Discord and Telegram partially fail and retry.

---

## 9. Limits to know about

- Button taps and /start are answered only when the bot next runs (up to ~10 min, or instantly with *Save & check now*). Repeated taps get one reply.
- Checking faster than every ~2 minutes risks VOX blocking the bot; each run loads about 16 pages.
- The sandbox the assistant works in cannot reach VOX, so live behaviour is verified through Actions.
- Posters come from VOX's own pages and are stored as small image files in the repo. Optionally TMDB could be used later (needs a key you would save yourself).

---

## 10. Decisions made

- Free, no card, fully automatic, no device left on.
- **No quiet hours.** Notify at any hour.
- Discord receives every movie; no per-movie ticking.
- Cloudflare timer every 10 minutes (2 minutes was suggested; not decided).
- "New" = first seen by the bot within 6 hours.
- Light blue and white panel, poster cards, people as cards.

## 11. To-do / agreed-but-not-built (talk first, then build)

1. Rework every message so it **explains itself**: why you got it and how to stop that kind of message. A stop button turns the setting off in `config.json`, so the panel shows it unchecked, and you are told.
2. Replace bare counts like "1 new · 6 first seen" with **labelled groups of times** ("🆕 New: first seen 35 min ago" / "Seen before: first seen 3 days ago") plus a one-line legend for 🆕.
3. **Fix the duplicate:** the general "New dates" message should leave out movies a person already gets personal alerts for.
4. Reword "Booking opened" to "First showtimes are out" (and optionally add a separate "now bookable" message).
5. Optional "Turn it back on" button after a stop.
6. Decide check frequency (10 min vs 2 min).
7. Optional: "show me what's out now" checkbox for existing movies; auto-add every new movie as a watch; TMDB posters.
8. A leftover branch `wip/quiet-hours-and-new-only-messages` exists on GitHub with abandoned quiet-hours work. It can be deleted.

---

## 12. Current setup snapshot (2026-11-03)

- Settings: roundup 12:00, "new" window 6 hours, admin = you.
- People: you, Osama, Essam, and the Discord channel.
- Active watches: Clayface, Resident Evil, Heart of the Beast, Avengers: Doomsday (Infinity Vision), Dune: Part Three.
- Latest code on `main`: commit `09ecf48`.

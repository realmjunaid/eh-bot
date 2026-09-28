# EH Bot — E-Hentai → Zip → GoFile → Discord

A Discord slash-command bot that downloads an E-Hentai gallery, zips it,
uploads the zip to [GoFile](https://gofile.io), and posts the download link.

## Commands

| Command | Description |
|---|---|
| `/ehd <url>` | Download gallery, upload to GoFile, reply with link |
| `/ping` | Check if the bot is alive |

Only works in channels listed in `ALLOWED_CHANNEL_IDS` (empty = everywhere),
and in **NSFW-marked channels** by default. Channel ID: right-click channel
-> Copy Channel ID (Developer Mode on).

## Setup

1. **Python 3.10+** required.
2. Install deps:
   ```powershell
   pip install -r requirements.txt
   ```
3. Copy config and fill it in:
   ```powershell
   copy .env.example .env
   # edit .env -> DISCORD_TOKEN (required), GOFILE_TOKEN + ALLOWED_CHANNEL_IDS (optional)
   ```
4. Create the Discord bot:
   - https://discord.com/developers/applications → New Application → Bot → copy token → `DISCORD_TOKEN`
   - Invite URL: `https://discord.com/oauth2/authorize?client_id=<APP_ID>&permissions=2147502080&scope=bot+applications.commands`
   - Required permissions: Send Messages, Embed Links, Use Slash Commands.
5. Run:
   ```powershell
   python bot.py
   ```

## Configuration (.env)

Basic keys (in `.env.example`):

| Key | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token (required) |
| `GOFILE_TOKEN` | GoFile account token for stable uploads (optional, guest mode otherwise). Get it at gofile.io → profile |
| `ALLOWED_CHANNEL_IDS` | Comma-separated channel IDs the bot works in (optional, empty = everywhere) |

Advanced keys (optional, sane defaults — only set if you need them):

| Key | Default | Purpose |
|---|---|---|
| `EH_IPB_MEMBER_ID` / `EH_IPB_PASS_HASH` / `EH_IGNEOUS` | — | E-Hentai login cookies to raise IP quota |
| `EH_ENABLE_EXHENTAI` | `false` | Allow `exhentai.org` URLs (needs all 3 cookies) |
| `EH_FETCH_DELAY` | `1.0` | Seconds between image-page fetches (keep ≥ 0.2 to avoid 509 bans) |
| `MAX_CONCURRENT_GALLERIES` / `MAX_CONCURRENT_IMAGES` | `2` / `5` | Concurrency caps |
| `MAX_IMAGES_PER_GALLERY` | `0` (unlimited) | Safety cap per gallery |
| `MAX_ZIP_MB` | `0` (unlimited) | Refuse zips over this size |
| `REQUIRE_NSFW_CHANNEL` | `true` | Restrict to NSFW channels |
| `CLEANUP_AFTER_UPLOAD` | `true` | Delete temp files after sending the link |

## How it works

```
Discord /ehd <gallery url>
  → collect /s/ page links from gallery (?p=0..N)
  → resolve each /s/ page to direct img#img URL (sequential + delay)
  → download images concurrently (default 5)
  → zip (Deflate)
  → POST zip to GoFile store server
  → reply with embed + download button
```

## E-Hentai notes / limits

- Plain `e-hentai.org` works without login but shares a per-IP quota. Hitting it returns **509** — the bot surfaces this as "image limit exceeded, try again later".
- Adding `EH_*` cookies (from a logged-in browser session) raises the quota.
- Keep `EH_FETCH_DELAY >= 0.2` (default `1.0`). Faster scraping gets your IP temp-banned.
- ExHentai ("sad panda" without login) requires all three cookies + `EH_ENABLE_EXHENTAI=true`.

## Project layout

```
bot.py             everything: config, E-Hentai scraper, GoFile client,
                   download→zip→upload pipeline, /ehd + /ping commands
requirements.txt   dependencies
.env.example       config template (copy to .env)
```

## Troubleshooting

- `DISCORD_TOKEN is missing` → you didn't create `.env`.
- `Invalid gallery URL` → must look like `https://e-hentai.org/g/<gid>/<token>/`.
- `509 / image limit` → E-Hentai quota exhausted; wait or add login cookies.
- `Sad panda` → ExHentai needs cookies.
- Slash commands don't appear → re-invite with `applications.commands` scope, wait ~1 min.

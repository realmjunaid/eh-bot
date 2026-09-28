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
   # edit .env -> DISCORD_TOKEN, GOFILE_TOKEN (optional)
   ```
4. Create the Discord bot:
   - https://discord.com/developers/applications → New Application → Bot → copy token → `DISCORD_TOKEN`
   - Invite URL: `https://discord.com/oauth2/authorize?client_id=<APP_ID>&permissions=274878221312&scope=bot+applications.commands`
   - Required permissions: Send Messages, Embed Links, Attach Files, Use Slash Commands.
5. Run:
   ```powershell
   python bot.py
   ```

## Configuration (.env)

| Key | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token (required) |
| `GOFILE_TOKEN` | GoFile account token for stable uploads (optional, guest mode otherwise). Get it at gofile.io → profile |
| `GOFILE_FOLDER_ID` | Upload into a specific GoFile folder (optional) |
| `EH_IPB_MEMBER_ID` / `EH_IPB_PASS_HASH` / `EH_IGNEOUS` | E-Hentai login cookies to raise IP quota (optional) |
| `EH_ENABLE_EXHENTAI` | Allow `exhentai.org` URLs (needs all 3 cookies) |
| `EH_FETCH_DELAY` | Seconds between image-page fetches (keep ≥ 0.5 to avoid 509 bans) |
| `MAX_CONCURRENT_GALLERIES` / `MAX_CONCURRENT_IMAGES` | Concurrency caps |
| `MAX_IMAGES_PER_GALLERY` | Safety cap (`0` = unlimited) |
| `MAX_ZIP_MB` | Refuse zips over this size (`0` = unlimited) |
| `REQUIRE_NSFW_CHANNEL` | Restrict to NSFW channels |
| `CLEANUP_AFTER_UPLOAD` | Delete temp files after sending the link |

## How it works

```
Discord /eh-dl <gallery url>
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
- Keep `EH_FETCH_DELAY >= 0.5` (default `1.0`). Faster scraping gets your IP temp-banned.
- ExHentai ("sad panda" without login) requires all three cookies + `EH_ENABLE_EXHENTAI=true`.

## Project layout

```
bot.py             entry point
config.py          .env loading + validation
ehentai.py         gallery / image-page scraping
gofile.py          GoFile upload client
downloader.py      pipeline: download -> zip -> upload
cogs/eh_commands.py  slash commands + progress embeds
```

## Troubleshooting

- `DISCORD_TOKEN is missing` → you didn't create `.env`.
- `Invalid gallery URL` → must look like `https://e-hentai.org/g/<gid>/<token>/`.
- `509 / image limit` → E-Hentai quota exhausted; wait or add login cookies.
- `Sad panda` → ExHentai needs cookies.
- Slash commands don't appear → re-invite with `applications.commands` scope, wait ~1 min.

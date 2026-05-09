# Free Trial Finder Bot - Code Explanation

This document explains how the bot works and how to customize it.

## 1) Project Goal

The bot searches the web for:
- free trial offers
- free plans
- student/education discounts

Then it sends clean Telegram responses using:
- `/check <platform>`
- `/scan`
- `/list`
- `/add <platform>`
- `/help`

## 2) Main Files

- `bot.py`: main bot logic (Telegram handlers + search + filtering + formatting)
- `platforms.json`: list of platforms scanned by `/scan`
- `official_domains.json`: trusted domains per platform (whitelist)
- `.env`: environment variables (mainly `TELEGRAM_BOT_TOKEN`)

## 3) Runtime Flow

### Startup
1. `main()` builds Telegram app with token from `get_bot_token()`.
2. `get_bot_token()` loads `.env` and validates token format.
3. Handlers are registered and `run_polling()` starts.

### `/check <platform>`
1. `check_platform()` runs `scan_platform()` in a background thread (`asyncio.to_thread`).
2. `scan_platform()` builds a focused query (with domain whitelist when available).
3. Search results are fetched from `DDGS().text(...)`.
4. Each result is cleaned (`canonicalize_url`), validated by whitelist, and fetched via `requests`.
5. Offer text is extracted with regex patterns (`extract_offer_info`).
6. Results are scored (`score_result`), deduplicated, sorted, and formatted.

### `/scan`
1. Reads all platforms from `platforms.json`.
2. Runs parallel scans with `ThreadPoolExecutor`.
3. Merges all results, deduplicates, sorts by score, and sends formatted output.

## 4) Accuracy Improvements in Code

- Official domain whitelist (`official_domains.json`)
- Query constrained with `site:` filters when whitelist exists
- Hard filter: if result domain is outside whitelist, it is ignored
- Tracking params removed from URLs (`utm_*`, `gclid`, `fbclid`, etc.)
- Low-trust domains penalized in scoring (reddit, youtube, x, ...)
- Duplicate results removed by `(platform, url, offer)`

## 5) Scoring Logic (High Level)

`score_result(...)` gives points for:
- official or official-ish domains
- pricing/plan hints in URL or snippet text
- explicit trial/free duration terms

It subtracts points for:
- low-trust domains
- negative hints (`review`, `coupon`, `torrent`, ...)

Final output is sorted descending by score.

## 6) Response Formatting

Formatting helpers:
- `render_help_message()`
- `render_platforms_message()`
- `render_check_results()`
- `render_scan_results()`

Long messages are split safely by `reply_long(...)`.

## 7) How to Customize

### Add platform
- Telegram: `/add NewPlatform`
- or edit `platforms.json` directly

### Add official domains for better precision
Edit `official_domains.json`:

```json
{
  "NewPlatform": ["newplatform.com", "docs.newplatform.com"]
}
```

Notes:
- Key should match platform name you use in `/check`.
- Subdomains are accepted automatically.

### Tune search behavior
In `bot.py` constants:
- `SEARCH_MAX_RESULTS`
- `MAX_PLATFORM_RESULTS`
- `MAX_SCAN_RESULTS`
- `REQUEST_TIMEOUT`
- `SCAN_WORKERS`

## 8) Local Run

1. Install dependencies.
2. Put token in `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:your_token_here
```

3. Run:

```bash
python bot.py
```

## 9) Security Notes

- Never share bot token publicly.
- Keep `.env` in `.gitignore` (already configured).
- If token is leaked, rotate it in BotFather immediately.

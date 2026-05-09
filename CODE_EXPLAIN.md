# Free Trial Finder Bot - Code Explain

## How it works now
- The bot does **not depend on web search** anymore.
- It scans official pricing pages from `official_domains.json`.
- It extracts offers using regex `PATTERNS` in `bot.py`.

## Main flow
1. User runs `/check Canva` or `/scan`.
2. `scan_platform()` loads official URL for that platform.
3. Bot fetches page HTML with `requests`.
4. `BeautifulSoup` converts HTML to text.
5. Regex patterns detect text like `free trial`, `$0`, `30 days free`, etc.
6. Bot formats and sends the result card.

## Key files
- `bot.py`: bot + scanner logic
- `official_domains.json`: platform -> official pricing URL
- `platforms.json`: platforms list for `/scan`
- `.env`: contains `TELEGRAM_BOT_TOKEN`

## Important error
If you see:
`Conflict: terminated by other getUpdates request`

That means the same token is running in another instance (local + Railway, or 2 locals).

Fix:
1. Stop all other bot instances.
2. Run only one polling instance for this token.

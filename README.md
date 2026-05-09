# Free Trial Finder Bot

Bot Telegram يفحص صفحات الـ pricing الرسمية مباشرة (بدون الاعتماد على search engine).

## Files
- `bot.py`: bot logic + scanner
- `platforms.json`: platforms list for `/scan`
- `official_domains.json`: official pricing URL per platform
- `.env`: `TELEGRAM_BOT_TOKEN`

## Setup
```bash
pip install -r requirements.txt
python bot.py
```

## Commands
- `/start`
- `/help`
- `/list`
- `/add <name>`
- `/check <name>`
- `/scan`

## Official Sources Format
`official_domains.json` لازم يكون هكّا:
```json
{
  "Canva": "https://www.canva.com/pricing/",
  "Figma": "https://www.figma.com/pricing/"
}
```

## Important
- Error `Conflict: terminated by other getUpdates request` يعني نفس bot token شغال في instance أخرى.
- لازم تشغّل bot instance وحدة فقط (مثلا local أو Railway، موش الزوز مع بعض).

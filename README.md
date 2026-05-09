# Free Trial Finder Bot

Bot Telegram يفحص صفحات الـ pricing الرسمية مباشرة (بدون الاعتماد على search engine).

## Local Only (No Railway)
If you want local mode only:
1. In Railway, stop/remove the bot service deployment.
2. Keep only one local terminal running `python bot.py`.
3. Do not run the same token on any cloud instance.

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
- This project is configured in local-only mode and will stop if it detects Railway environment.

## Network Troubleshooting
If you see `httpx.ConnectError: [Errno 11001] getaddrinfo failed`:
1. Check your internet connection.
2. Check DNS resolution for `api.telegram.org`.
3. Remove broken proxy env vars in PowerShell:
```powershell
Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:ALL_PROXY -ErrorAction SilentlyContinue
```

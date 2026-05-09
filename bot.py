import asyncio
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")
LOGGER = logging.getLogger(__name__)

ENV_FILE = ".env"
PLATFORMS_FILE = "platforms.json"
OFFICIAL_DOMAINS_FILE = "official_domains.json"

MAX_PLATFORM_RESULTS = 5
MAX_SCAN_RESULTS = 20
REQUEST_TIMEOUT = 12
MAX_PAGE_CHARS = 350000
SNIPPET_WINDOW = 90
SCAN_WORKERS = 4
MAX_TELEGRAM_MESSAGE = 3900
CARD_SEPARATOR = "------------------------------"

DEFAULT_PLATFORMS = [
    "Canva",
    "Spotify",
    "Adobe",
    "Figma",
    "Notion",
    "ChatGPT",
    "GitHub Copilot",
    "Cursor AI",
]

DEFAULT_OFFICIAL_DOMAINS = {
    "Canva": "https://www.canva.com/pricing/",
    "Spotify": "https://www.spotify.com/premium/",
    "Adobe": "https://www.adobe.com/creativecloud/plans.html",
    "Figma": "https://www.figma.com/pricing/",
    "Notion": "https://www.notion.so/pricing",
    "ChatGPT": "https://openai.com/chatgpt/pricing/",
    "GitHub Copilot": "https://github.com/features/copilot/plans",
    "Cursor AI": "https://cursor.com/pricing",
}

PATTERNS = [
    r"free trial",
    r"try.*free",
    r"start.*free",
    r"free for \d+ days",
    r"free for \d+ months",
    r"\d+\s*days?\s*free",
    r"\d+\s*months?\s*free",
    r"1\s*month\s*free",
    r"3\s*months?\s*free",
    r"30\s*days?\s*free",
    r"90\s*days?\s*free",
    r"\$0",
    r"0\s*\$",
    r"free plan",
    r"student plan",
    r"student discount",
    r"no credit card required",
    r"free version",
]

COMPILED_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in PATTERNS]

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_local_env(path=ENV_FILE):
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        return


def get_bot_token():
    load_local_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Set it first, then run the bot again.")

    if not TOKEN_PATTERN.match(token):
        raise RuntimeError("Invalid TELEGRAM_BOT_TOKEN format. Use the full BotFather token: <digits>:<secret>.")

    return token


def load_platforms():
    try:
        with open(PLATFORMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return [str(item).strip() for item in data if str(item).strip()]
    except (OSError, json.JSONDecodeError):
        pass

    save_platforms(DEFAULT_PLATFORMS)
    return list(DEFAULT_PLATFORMS)


def save_platforms(platforms):
    with open(PLATFORMS_FILE, "w", encoding="utf-8") as f:
        json.dump(platforms, f, indent=2, ensure_ascii=False)


def normalize_domain(value):
    domain = (value or "").strip().lower()
    if not domain:
        return ""

    if "://" in domain:
        domain = urlsplit(domain).netloc.lower()

    domain = domain.split("/")[0].strip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def normalize_url(value):
    raw = (value or "").strip()
    if not raw:
        return ""

    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"

    split = urlsplit(raw)
    if split.scheme not in ("http", "https") or not split.netloc:
        return ""
    return raw


def save_official_domains(domains_map):
    with open(OFFICIAL_DOMAINS_FILE, "w", encoding="utf-8") as f:
        json.dump(domains_map, f, indent=2, ensure_ascii=False)


def load_official_domains():
    normalized_defaults = {
        platform.lower(): normalize_url(url)
        for platform, url in DEFAULT_OFFICIAL_DOMAINS.items()
    }

    try:
        with open(OFFICIAL_DOMAINS_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        if not isinstance(raw_data, dict):
            raise ValueError("official domains file must be an object")

        loaded = {}
        for platform, value in raw_data.items():
            key = str(platform).strip().lower()
            if not key:
                continue

            if isinstance(value, str):
                url = normalize_url(value)
                if url:
                    loaded[key] = url
                continue

            if isinstance(value, list) and value:
                # Backward compatibility: old format may contain domains list.
                first_domain = normalize_domain(str(value[0]))
                if first_domain:
                    loaded[key] = f"https://{first_domain}"

        merged = dict(normalized_defaults)
        merged.update(loaded)
        return merged
    except (OSError, json.JSONDecodeError, ValueError):
        save_official_domains(DEFAULT_OFFICIAL_DOMAINS)
        return normalized_defaults


def normalize_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def get_platform_source_url(platform, official_domains_map):
    return official_domains_map.get(platform.lower(), "")


def extract_offer_info(text):
    for pattern in COMPILED_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue

        start = max(match.start() - SNIPPET_WINDOW, 0)
        end = min(match.end() + SNIPPET_WINDOW, len(text))
        snippet = normalize_text(text[start:end])
        return normalize_text(match.group(0)), snippet

    return None, None


def extract_offer_matches(text):
    matches = []
    seen = set()

    for pattern in COMPILED_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue

        offer = normalize_text(match.group(0))
        key = offer.lower()
        if not offer or key in seen:
            continue
        seen.add(key)

        start = max(match.start() - SNIPPET_WINDOW, 0)
        end = min(match.end() + SNIPPET_WINDOW, len(text))
        snippet = normalize_text(text[start:end])
        matches.append((offer, snippet))

    return matches


def fetch_page_text(url):
    try:
        try:
            res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.SSLError:
            res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=False)
        content_type = res.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            return "", ""

        soup = BeautifulSoup(res.text, "html.parser")
        title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        text = normalize_text(soup.get_text(" ", strip=True))
        return title, text[:MAX_PAGE_CHARS]
    except Exception:
        return "", ""


def dedupe_and_sort(results):
    deduped = []
    seen = set()

    for item in results:
        key = (
            item.get("platform", "").lower(),
            item.get("url", "").lower(),
            item.get("offer", "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    deduped.sort(key=lambda r: r.get("score", 0), reverse=True)
    return deduped


def scan_platform(platform, official_domains_map=None):
    if official_domains_map is None:
        official_domains_map = load_official_domains()

    url = get_platform_source_url(platform, official_domains_map)
    if not url:
        return []

    found = []

    title, page_text = fetch_page_text(url)
    if not page_text:
        return found

    search_text = f"{title} {page_text}"
    matched = extract_offer_matches(search_text)
    for offer, snippet in matched:
        found.append(
            {
                "platform": platform,
                "title": title or f"{platform} pricing",
                "offer": offer,
                "url": url,
                "snippet": snippet,
                "score": 100,
                "source": "official",
            }
        )

    return dedupe_and_sort(found)


def scan_all_platforms(platforms):
    if not platforms:
        return []

    all_results = []
    official_domains_map = load_official_domains()
    max_workers = min(SCAN_WORKERS, max(1, len(platforms)))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(scan_platform, platform, official_domains_map): platform
            for platform in platforms
        }

        for future in as_completed(futures):
            try:
                all_results.extend(future.result())
            except Exception:
                continue

    return dedupe_and_sort(all_results)


def format_result(result, include_platform=False):
    lines = []
    if include_platform:
        lines.append(f"📌 {result['platform']}")
        lines.append("")

    title = result.get("title") or "Offer found"
    lines.append(f"🎁 {title}")
    lines.append(result["url"])
    lines.append(f"🎯 {result['offer']}")

    if result.get("snippet"):
        lines.append(f"📝 {result['snippet']}")

    return "\n".join(lines)


def render_help_message():
    return (
        "👋 Welcome to Free Trial Finder Bot!\n"
        "I help you find platforms that offer free trials or $0 plans.\n\n"
        "🛰 /scan - Search all platforms\n"
        "🛠 /check <name> - Check one platform\n"
        "➕ /add <name> - Add a platform\n"
        "📋 /list - Show all platforms\n"
        "ℹ️ /help - Show help"
    )


def render_platforms_message(platforms):
    lines = [f"📌 Platforms ({len(platforms)}):", ""]
    lines.extend([f"• {platform}" for platform in platforms])
    return "\n".join(lines)


def render_check_results(platform, results):
    lines = [
        f"🔎 Checking {platform}...",
        f"✅ Offers found for {platform}:",
        CARD_SEPARATOR,
        "",
    ]

    for idx, result in enumerate(results[:MAX_PLATFORM_RESULTS], start=1):
        lines.append(f"{idx}) {format_result(result)}")
        lines.append("")
        lines.append(CARD_SEPARATOR)
        lines.append("")

    return "\n".join(lines).strip()


def render_scan_results(results):
    lines = ["🔥 Free / Trial Offers Found:", ""]

    for idx, result in enumerate(results[:MAX_SCAN_RESULTS], start=1):
        lines.append(f"{idx}) {format_result(result, include_platform=True)}")
        lines.append("")
        lines.append(CARD_SEPARATOR)
        lines.append("")

    return "\n".join(lines).strip()


async def reply_long(update: Update, text: str):
    for i in range(0, len(text), MAX_TELEGRAM_MESSAGE):
        await update.message.reply_text(text[i : i + MAX_TELEGRAM_MESSAGE])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    LOGGER.exception("Bot error: %s", context.error)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(render_help_message())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(render_help_message())


async def list_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platforms = load_platforms()
    await update.message.reply_text(render_platforms_message(platforms))


async def add_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Example: /add Canva")
        return

    name = normalize_text(" ".join(context.args))
    platforms = load_platforms()

    existing_lower = {p.lower() for p in platforms}
    if name.lower() in existing_lower:
        await update.message.reply_text("Already exists ✅")
        return

    platforms.append(name)
    save_platforms(platforms)
    await update.message.reply_text(f"Added ✅: {name}")


async def check_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Example: /check Canva")
        return

    platform = normalize_text(" ".join(context.args))
    await update.message.reply_text(f"🔎 Checking {platform}...")

    results = await asyncio.to_thread(scan_platform, platform)

    if not results:
        await update.message.reply_text(f"❌ No confirmed free offer found for {platform}.")
        return

    await reply_long(update, render_check_results(platform, results))


async def scan_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platforms = load_platforms()
    await update.message.reply_text("🔎 Scanning all platforms...")

    all_results = await asyncio.to_thread(scan_all_platforms, platforms)

    if not all_results:
        await update.message.reply_text("❌ No offers found.")
        return

    await reply_long(update, render_scan_results(all_results))


def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    token = get_bot_token()
    app = ApplicationBuilder().token(token).build()

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_platforms))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_platform))
    app.add_handler(CommandHandler("check", check_platform))
    app.add_handler(CommandHandler("scan", scan_all))

    app.run_polling(drop_pending_updates=True, bootstrap_retries=0)


if __name__ == "__main__":
    main()

import asyncio
import json
import logging
import os
import re
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from telegram.error import Conflict, NetworkError
from telegram.request import HTTPXRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")
LOGGER = logging.getLogger(__name__)

ENV_FILE = ".env"
PLATFORMS_FILE = "platforms.json"
OFFICIAL_DOMAINS_FILE = "official_domains.json"
LOCK_FILE = ".bot.lock"

MAX_PLATFORM_RESULTS = 3
MAX_SCAN_RESULTS = 8
REQUEST_TIMEOUT = 12
MAX_PAGE_CHARS = 350000
SNIPPET_WINDOW = 70
SCAN_WORKERS = 4
MAX_TELEGRAM_MESSAGE = 3900
CARD_SEPARATOR = "------------------------------"
MAX_MATCHES_PER_PLATFORM = 2
MAX_SCAN_RESULTS_PER_PLATFORM = 1
MAX_OFFER_TEXT_LEN = 80
MAX_SNIPPET_TEXT_LEN = 220
MAX_MATCHES_PER_PAGE = 3
CHECK_CALLBACK_PREFIX = "check::"
NETWORK_LOG_THROTTLE_SEC = 30

DEFAULT_PLATFORMS = [
    "Canva",
    "Spotify",
    "YouTube",
    "Adobe",
    "Figma",
    "Notion",
    "ChatGPT",
    "GitHub Copilot",
    "Cursor AI",
]

DEFAULT_OFFICIAL_DOMAINS = {
    "Canva": [
        "https://www.canva.com/pricing/",
        "https://www.canva.com/education/",
    ],
    "Spotify": [
        "https://www.spotify.com/premium/",
        "https://www.spotify.com/student/",
        "https://support.spotify.com/us/article/premium-student/",
    ],
    "YouTube": [
        "https://www.youtube.com/premium",
        "https://support.google.com/youtube/answer/16475192?hl=en",
        "https://support.google.com/youtube/answer/9158808?hl=en",
    ],
    "Adobe": ["https://www.adobe.com/creativecloud/plans.html"],
    "Figma": ["https://www.figma.com/pricing/"],
    "Notion": ["https://www.notion.so/pricing"],
    "ChatGPT": ["https://openai.com/chatgpt/pricing/"],
    "GitHub Copilot": ["https://github.com/features/copilot/plans"],
    "Cursor AI": ["https://cursor.com/pricing"],
}

PATTERNS = [
    r"free trial",
    r"\btry.{0,40}?free\b",
    r"\bstart.{0,40}?free\b",
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
    r"student membership",
    r"\bstudents?\b.{0,40}\bfree\b",
    r"\bfree\b.{0,40}\bstudents?\b",
    r"free for students",
    r"no credit card required",
    r"free version",
]

COMPILED_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in PATTERNS]

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}

LAST_NETWORK_ERROR_LOG_AT = 0.0
REQUESTS_SESSION = requests.Session()
REQUESTS_SESSION.trust_env = False


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


def pid_exists(pid):
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5

            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return ctypes.windll.kernel32.GetLastError() == ERROR_ACCESS_DENIED
        except Exception:
            return False

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def release_instance_lock(lock_path=LOCK_FILE):
    with suppress(OSError, ValueError):
        with open(lock_path, "r", encoding="utf-8") as lock_file:
            lock_pid = int((lock_file.read() or "0").strip() or "0")
        if lock_pid and lock_pid != os.getpid():
            return
        os.remove(lock_path)


def acquire_instance_lock(lock_path=LOCK_FILE):
    current_pid = os.getpid()

    if os.path.exists(lock_path):
        stale_pid = 0
        with suppress(OSError, ValueError):
            with open(lock_path, "r", encoding="utf-8") as lock_file:
                stale_pid = int((lock_file.read() or "0").strip() or "0")

        if stale_pid and pid_exists(stale_pid):
            raise RuntimeError(f"Another local bot instance is already running (PID {stale_pid}).")

        with suppress(OSError):
            os.remove(lock_path)

    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
        lock_file.write(str(current_pid))


def ensure_local_only_mode():
    if os.getenv("RAILWAY_SERVICE_ID"):
        raise RuntimeError(
            "Local-only mode is enabled. This bot should not run on Railway. "
            "Stop the Railway service and run it only on your local machine."
        )


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


def normalize_source_urls(value):
    raw_items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    normalized = []
    seen = set()

    for item in raw_items:
        text = str(item).strip()
        if not text:
            continue

        url = normalize_url(text)
        if not url:
            # Backward compatibility with old files that may store plain domains.
            domain = normalize_domain(text)
            if domain:
                url = f"https://{domain}"

        if not url:
            continue

        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(url)

    return normalized


def load_official_domains():
    normalized_defaults = {
        platform.lower(): normalize_source_urls(urls)
        for platform, urls in DEFAULT_OFFICIAL_DOMAINS.items()
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

            urls = normalize_source_urls(value)
            if urls:
                loaded[key] = urls

        merged = dict(normalized_defaults)
        merged.update(loaded)
        return merged
    except (OSError, json.JSONDecodeError, ValueError):
        save_official_domains(DEFAULT_OFFICIAL_DOMAINS)
        return normalized_defaults


def normalize_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def truncate_text(text, max_len):
    cleaned = normalize_text(text)
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip(" ,.;:") + "..."


def get_platform_source_urls(platform, official_domains_map):
    return official_domains_map.get(platform.lower(), [])


def extract_offer_info(text):
    for pattern in COMPILED_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue

        offer = truncate_text(match.group(0), MAX_OFFER_TEXT_LEN)
        start = max(match.start() - SNIPPET_WINDOW, 0)
        end = min(match.end() + SNIPPET_WINDOW, len(text))
        snippet = truncate_text(text[start:end], MAX_SNIPPET_TEXT_LEN)
        return offer, snippet

    return None, None


def extract_offer_matches(text):
    matches = []
    seen = set()

    for pattern in COMPILED_PATTERNS:
        for match in pattern.finditer(text):
            offer = truncate_text(match.group(0), MAX_OFFER_TEXT_LEN)
            key = offer.lower()
            if not offer or key in seen:
                continue
            seen.add(key)

            start = max(match.start() - SNIPPET_WINDOW, 0)
            end = min(match.end() + SNIPPET_WINDOW, len(text))
            snippet = truncate_text(text[start:end], MAX_SNIPPET_TEXT_LEN)
            matches.append((offer, snippet))

            if len(matches) >= MAX_MATCHES_PER_PAGE:
                return matches

    return matches


def fetch_page_text(url):
    try:
        try:
            res = REQUESTS_SESSION.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.SSLError:
            res = REQUESTS_SESSION.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=False)
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

    urls = get_platform_source_urls(platform, official_domains_map)
    if not urls:
        return []

    found = []
    for url_idx, url in enumerate(urls):
        title, page_text = fetch_page_text(url)
        if not page_text:
            continue

        search_text = f"{title} {page_text}"
        matched = extract_offer_matches(search_text)
        for match_idx, (offer, snippet) in enumerate(matched):
            found.append(
                {
                    "platform": platform,
                    "title": title or f"{platform} pricing",
                    "offer": offer,
                    "url": url,
                    "snippet": snippet,
                    "score": 120 - (url_idx * 5) - match_idx,
                    "source": "official",
                }
            )

    sorted_found = dedupe_and_sort(found)
    return sorted_found[:MAX_MATCHES_PER_PLATFORM]


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

    deduped = dedupe_and_sort(all_results)
    limited = []
    per_platform = {}
    for result in deduped:
        platform_key = result.get("platform", "").lower()
        count = per_platform.get(platform_key, 0)
        if count >= MAX_SCAN_RESULTS_PER_PLATFORM:
            continue
        per_platform[platform_key] = count + 1
        limited.append(result)

    return limited


def format_result(result, include_platform=False):
    lines = []
    if include_platform:
        lines.append(f"Platform: {result['platform']}")
        lines.append("")

    title = result.get("title") or "Offer found"
    lines.append(f"Title: {title}")
    lines.append(result["url"])
    lines.append(f"Offer: {result['offer']}")

    if result.get("snippet"):
        lines.append(f"Note: {result['snippet']}")

    return "\n".join(lines)


def render_help_message():
    return (
        "Welcome to Free Trial Finder Bot.\n"
        "I help you find platforms that offer free trials or $0 plans.\n\n"
        "/scan - Search all platforms\n"
        "/check <name> - Check one platform\n"
        "/add <name> - Add a platform\n"
        "/list - Show all platforms\n"
        "/help - Show help"
    )


def render_platforms_message(platforms):
    lines = [f"Platforms ({len(platforms)}):", ""]
    lines.extend([f"- {platform}" for platform in platforms])
    return "\n".join(lines)


def build_check_keyboard(platforms, columns=2):
    rows = []
    current_row = []

    for platform in platforms:
        current_row.append(
            InlineKeyboardButton(
                platform,
                callback_data=f"{CHECK_CALLBACK_PREFIX}{platform}",
            )
        )
        if len(current_row) == columns:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    return InlineKeyboardMarkup(rows)


def render_check_results(platform, results):
    lines = [
        f"Checking {platform}...",
        f"Offers found for {platform}:",
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
    lines = ["Free / Trial Offers Found:", ""]

    for idx, result in enumerate(results[:MAX_SCAN_RESULTS], start=1):
        lines.append(f"{idx}) {format_result(result, include_platform=True)}")
        lines.append("")
        lines.append(CARD_SEPARATOR)
        lines.append("")

    return "\n".join(lines).strip()


async def reply_long(update: Update, text: str):
    for i in range(0, len(text), MAX_TELEGRAM_MESSAGE):
        await update.message.reply_text(text[i : i + MAX_TELEGRAM_MESSAGE])


async def reply_long_message(message, text: str):
    for i in range(0, len(text), MAX_TELEGRAM_MESSAGE):
        await message.reply_text(text[i : i + MAX_TELEGRAM_MESSAGE])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    global LAST_NETWORK_ERROR_LOG_AT
    error = context.error
    if isinstance(error, Conflict):
        LOGGER.error("Telegram conflict (another instance is polling). Stopping this instance.")
        context.application.stop_running()
        return

    if isinstance(error, NetworkError):
        message = str(error)
        now = monotonic()
        should_log = now - LAST_NETWORK_ERROR_LOG_AT >= NETWORK_LOG_THROTTLE_SEC
        if should_log:
            if "getaddrinfo failed" in message.lower():
                LOGGER.warning(
                    "Network/DNS issue while contacting Telegram API. "
                    "Check internet and disable broken proxy env vars "
                    "(HTTP_PROXY/HTTPS_PROXY/ALL_PROXY). Details: %s",
                    message,
                )
            else:
                LOGGER.warning("Temporary Telegram network issue: %s", message)
            LAST_NETWORK_ERROR_LOG_AT = now
        return

    LOGGER.error("Bot error: %s", error, exc_info=error)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platforms = load_platforms()
    intro = render_help_message()

    if not platforms:
        await update.message.reply_text(f"{intro}\n\nNo platforms found. Add one with /add <name>.")
        return

    await update.message.reply_text(
        f"{intro}\n\nSelect a platform to check:",
        reply_markup=build_check_keyboard(platforms),
    )


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
        await update.message.reply_text("Already exists")
        return

    platforms.append(name)
    save_platforms(platforms)
    await update.message.reply_text(f"Added: {name}")


async def check_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        platforms = load_platforms()
        if not platforms:
            await update.message.reply_text("No platforms found. Add one with /add <name>.")
            return

        await update.message.reply_text(
            "Select a platform to check:",
            reply_markup=build_check_keyboard(platforms),
        )
        return

    platform = normalize_text(" ".join(context.args))
    await update.message.reply_text(f"Checking {platform}...")

    results = await asyncio.to_thread(scan_platform, platform)

    if not results:
        await update.message.reply_text(f"No confirmed free offer found for {platform}.")
        return

    await reply_long(update, render_check_results(platform, results))


async def check_platform_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    if not query.data.startswith(CHECK_CALLBACK_PREFIX):
        return

    platform = normalize_text(query.data[len(CHECK_CALLBACK_PREFIX) :])
    platforms = load_platforms()
    known = {item.lower() for item in platforms}
    if platform.lower() not in known:
        await query.answer("Platform is no longer available.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_text(f"Checking {platform}...")

    results = await asyncio.to_thread(scan_platform, platform)
    if not results:
        await query.message.reply_text(f"No confirmed free offer found for {platform}.")
        return

    await reply_long_message(query.message, render_check_results(platform, results))


async def scan_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platforms = load_platforms()
    await update.message.reply_text("Scanning all platforms...")

    all_results = await asyncio.to_thread(scan_all_platforms, platforms)

    if not all_results:
        await update.message.reply_text("No offers found.")
        return

    await reply_long(update, render_scan_results(all_results))


def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    ensure_local_only_mode()
    acquire_instance_lock()
    token = get_bot_token()
    common_httpx_kwargs = {"trust_env": False}
    request = HTTPXRequest(
        proxy=None,
        connect_timeout=15.0,
        read_timeout=20.0,
        write_timeout=20.0,
        httpx_kwargs=common_httpx_kwargs,
    )
    get_updates_request = HTTPXRequest(
        proxy=None,
        connect_timeout=15.0,
        read_timeout=20.0,
        write_timeout=20.0,
        httpx_kwargs=common_httpx_kwargs,
    )
    app = (
        ApplicationBuilder()
        .token(token)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )

    try:
        app.add_error_handler(error_handler)
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("list", list_platforms))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("add", add_platform))
        app.add_handler(CommandHandler("check", check_platform))
        app.add_handler(CallbackQueryHandler(check_platform_callback, pattern=rf"^{CHECK_CALLBACK_PREFIX}"))
        app.add_handler(CommandHandler("scan", scan_all))

        app.run_polling(drop_pending_updates=True, bootstrap_retries=0)
    finally:
        release_instance_lock()


if __name__ == "__main__":
    main()

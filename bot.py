import asyncio
import json
import os
import re
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

try:
    from ddgs import DDGS

    USING_LEGACY_DDGS = False
except ImportError:
    from duckduckgo_search import DDGS

    USING_LEGACY_DDGS = True

TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")

ENV_FILE = ".env"
PLATFORMS_FILE = "platforms.json"
OFFICIAL_DOMAINS_FILE = "official_domains.json"

SEARCH_MAX_RESULTS = 8
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
    "Canva": ["canva.com"],
    "Spotify": ["spotify.com"],
    "Adobe": ["adobe.com"],
    "Figma": ["figma.com"],
    "Notion": ["notion.so", "notion.com"],
    "ChatGPT": ["openai.com", "chatgpt.com"],
    "GitHub Copilot": ["github.com"],
    "Cursor AI": ["cursor.com", "cursor.sh"],
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

URL_HINTS = (
    "pricing",
    "plans",
    "plan",
    "billing",
    "subscription",
    "trial",
    "student",
    "education",
    "free",
)

NEGATIVE_HINTS = (
    "review",
    "coupon",
    "promo code",
    "reddit",
    "forum",
    "torrent",
    "crack",
    "mod apk",
)

LOW_TRUST_DOMAINS = {
    "reddit.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "quora.com",
    "pinterest.com",
    "medium.com",
    "linkedin.com",
    "wikipedia.org",
}

TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "ref",
    "ref_src",
    "source",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
}


def build_ddgs_client():
    def _create_client():
        try:
            return DDGS(verify=False)
        except TypeError:
            return DDGS()

    if not USING_LEGACY_DDGS:
        return _create_client()

    original_warn = warnings.warn

    def _patched_warn(message, *args, **kwargs):
        if isinstance(message, str) and "renamed to `ddgs`" in message:
            return None
        return original_warn(message, *args, **kwargs)

    warnings.warn = _patched_warn
    try:
        return _create_client()
    finally:
        warnings.warn = original_warn


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


def save_official_domains(domains_map):
    with open(OFFICIAL_DOMAINS_FILE, "w", encoding="utf-8") as f:
        json.dump(domains_map, f, indent=2, ensure_ascii=False)


def load_official_domains():
    normalized_defaults = {
        platform.lower(): [normalize_domain(item) for item in domains]
        for platform, domains in DEFAULT_OFFICIAL_DOMAINS.items()
    }

    try:
        with open(OFFICIAL_DOMAINS_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        if not isinstance(raw_data, dict):
            raise ValueError("official domains file must be an object")

        loaded = {}
        for platform, domains in raw_data.items():
            if isinstance(domains, str):
                domains = [domains]
            if not isinstance(domains, list):
                continue

            normalized_list = []
            for domain in domains:
                item = normalize_domain(str(domain))
                if item and item not in normalized_list:
                    normalized_list.append(item)

            if normalized_list:
                loaded[str(platform).strip().lower()] = normalized_list

        merged = dict(normalized_defaults)
        merged.update(loaded)
        return merged
    except (OSError, json.JSONDecodeError, ValueError):
        save_official_domains(DEFAULT_OFFICIAL_DOMAINS)
        return normalized_defaults


def normalize_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def canonicalize_url(url):
    try:
        split = urlsplit((url or "").strip())
        if split.scheme not in ("http", "https") or not split.netloc:
            return ""

        query_pairs = parse_qsl(split.query, keep_blank_values=True)
        filtered_query = [
            (k, v)
            for k, v in query_pairs
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING_QUERY_KEYS
        ]

        query = urlencode(filtered_query, doseq=True)
        path = split.path.rstrip("/") or split.path
        return urlunsplit((split.scheme.lower(), split.netloc.lower(), path, query, ""))
    except Exception:
        return ""


def get_domain(url):
    netloc = urlsplit(url).netloc.lower()
    if netloc.startswith("www."):
        return netloc[4:]
    return netloc


def get_platform_whitelist(platform, official_domains_map):
    return official_domains_map.get(platform.lower(), [])


def domain_in_whitelist(domain, whitelist):
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in whitelist)


def build_search_query(platform, whitelist):
    base = f'{platform} official pricing free trial "$0" "months free"'
    if not whitelist:
        return base

    site_filter = " OR ".join([f"site:{domain}" for domain in whitelist])
    return f"{base} ({site_filter})"


def is_low_trust_domain(domain):
    return any(domain == item or domain.endswith(f".{item}") for item in LOW_TRUST_DOMAINS)


def platform_tokens(platform):
    return [token for token in re.findall(r"[a-z0-9]+", platform.lower()) if len(token) > 2]


def looks_official_for_platform(platform, domain):
    return any(token in domain for token in platform_tokens(platform))


def score_result(platform, url, title, body, offer, whitelist):
    score = 0
    domain = get_domain(url)
    path = urlsplit(url).path.lower()
    search_text = f"{title} {body}".lower()
    source = "community"

    if whitelist and domain_in_whitelist(domain, whitelist):
        score += 8
        source = "official"
    elif looks_official_for_platform(platform, domain):
        score += 4
        source = "official-ish"

    if any(hint in path for hint in URL_HINTS):
        score += 2

    if any(hint in search_text for hint in URL_HINTS):
        score += 1

    if any(hint in search_text for hint in NEGATIVE_HINTS):
        score -= 2

    if is_low_trust_domain(domain):
        score -= 3

    offer_text = offer.lower()
    if any(term in offer_text for term in ("trial", "day", "week", "month", "year")):
        score += 2
    elif "free" in offer_text or "student" in offer_text:
        score += 1

    return score, source


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


def fetch_page_text(url):
    try:
        res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        content_type = res.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            return ""

        soup = BeautifulSoup(res.text, "html.parser")
        text = normalize_text(soup.get_text(" ", strip=True))
        return text[:MAX_PAGE_CHARS]
    except Exception:
        return ""


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

    whitelist = get_platform_whitelist(platform, official_domains_map)
    query = build_search_query(platform, whitelist)
    found = []

    try:
        with build_ddgs_client() as ddgs:
            results = ddgs.text(
                query,
                region="us-en",
                safesearch="moderate",
                max_results=SEARCH_MAX_RESULTS,
            )
            results = list(results)
    except Exception:
        return found

    for item in results:
        url = canonicalize_url(item.get("href", ""))
        title = normalize_text(item.get("title", ""))
        body = normalize_text(item.get("body", ""))
        search_text = f"{title} {body}"

        if not url:
            continue

        domain = get_domain(url)
        if whitelist and not domain_in_whitelist(domain, whitelist):
            continue

        offer, snippet = extract_offer_info(search_text)
        if not offer:
            page_text = fetch_page_text(url)
            candidate_text = page_text if page_text else body
            if not candidate_text:
                continue

            offer, snippet = extract_offer_info(candidate_text)
            if not offer:
                continue

        score, source = score_result(platform, url, title, body, offer, whitelist)
        found.append(
            {
                "platform": platform,
                "title": title,
                "offer": offer,
                "url": url,
                "snippet": snippet,
                "score": score,
                "source": source,
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
    token = get_bot_token()
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_platforms))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_platform))
    app.add_handler(CommandHandler("check", check_platform))
    app.add_handler(CommandHandler("scan", scan_all))

    app.run_polling()


if __name__ == "__main__":
    main()

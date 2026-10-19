#!/usr/bin/env python3
"""
live_tiktok_scraper.py

Fast targeted TikTok lead scraper for beauty/jewelry/GRWM nano- & micro-influencers (10 - 5,000 followers).
- Strict non-shipping / excluded region filtering (Primary criteria).
- Follower range: 10 to 5,000 (JSON rehydration + DOM fallback; unreadable profiles are retried later, not blacklisted).
- Strict email extraction: TLD allowlist, social-handle rejection ("insta@test.test"),
  label-prefix stripping ("pr-test@x.com" -> "test@x.com"), obfuscation support ("name (at) gmail (dot) com").
- Linktree / bio-link fallback resolution (non-blocking, runs in a worker thread).
- Local lead backup file to prevent data loss if the n8n webhook drops.
- Resource route blocking (images/videos/fonts) for faster scraping and lower RAM/CPU.
- Instant dispatch to n8n upon meeting criteria with explicit skip logging.
"""

import asyncio
import json
import os
import random
import re
import signal
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote, quote_plus

import requests
from playwright.async_api import async_playwright

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------- CONFIG ----------------
N8N_PRODUCTION_WEBHOOK = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/tiktok-leads")
SEEN_FILE = os.path.expanduser("~/tiktok-scraper/seen_creators.txt")
BACKUP_LEADS_FILE = os.path.expanduser("~/tiktok-scraper/leads_backup.jsonl")
USER_DATA_DIR = os.path.expanduser("~/tiktok-scraper/browser_profile")

# Follower boundaries
MIN_FOLLOWERS = 10
MAX_FOLLOWERS = 5000

# Batch controls
BATCH_LEAD_LIMIT = 50
DELAY_BETWEEN_EMAILS = 40.0  # Lowered temporarily for faster initial leads
BATCH_REST_DURATION = 10 * 60.0
DELAY_BETWEEN_SEARCHES = (2.0, 3.5)

# Email behavior
STRIP_LABEL_PREFIX = True   # "pr-test@x.com" -> "test@x.com". Set False to REJECT such emails instead.
BIO_LINK_TIMEOUT = 10       # Increased for slower link-in-bio services
BIO_LINK_MAX_BYTES = 400_000

# ---------------- REGION FILTERING (PRIMARY CRITERIA) ----------------
EXCLUDED_REGION_TERMS = [
    # Excluded South America
    "brazil", "brasil", "rio", "são paulo", ".com.br", "🇧🇷",
    "venezuela", "bolivia", "argentina", "buenos aires", "🇦🇷", ".ar",

    # Africa (All non-shipping destinations)
    "africa", "nigeria", "lagos", "kenya", "nairobi", "south africa", "ghana",
    "egypt", "morocco", "algeria", "tunisia", "🇿🇦", "🇳🇬", "🇰🇪", "🇲🇦", "🇪🇬",

    # War, Conflict Zones, & Sanctions
    "russia", "moscow", ".ru", "🇷🇺",
    "ukraine", "kyiv", ".ua", "🇺🇦",
    "israel", "tel aviv", "🇮🇱",
    "palestine", "gaza", "🇵🇸",
    "syria", "yemen", "sudan", "lebanon", "iraq",

    # All "Stan" Countries & South Asia
    "pakistan", "islamabad", "karachi", "lahore", "🇵🇰", ".pk",
    "kazakhstan", ".kz", "uzbekistan", ".uz", "turkmenistan", ".tm",
    "tajikistan", ".tj", "kyrgyzstan", ".kg", "afghanistan", ".af",
    "india", "delhi", "mumbai", "bangalore", ".in", "🇮🇳", "bangladesh"
]

# Precompiled. Words/emoji: not glued to other word chars (the old \b never matched emoji flags).
# TLD terms (".in", ".ar", ...): must END the token, so ".in" no longer matches ".info" / ".instagram".
_REGION_WORD_PATTERNS = [
    re.compile(rf"(?<!\w){re.escape(t)}(?!\w)")
    for t in EXCLUDED_REGION_TERMS if not t.startswith(".")
]
_REGION_TLD_PATTERNS = [
    re.compile(rf"{re.escape(t)}(?![a-z0-9])")
    for t in EXCLUDED_REGION_TERMS if t.startswith(".")
]

# ---------------- SEARCH MATRIX ----------------
LOCATIONS = [
    # North America
    "us", "usa", "united states", "canada", "mexico",
    "new york", "los angeles", "chicago", "toronto", "vancouver", "miami",

    # South America (Permitted)
    "chile", "colombia", "peru", "uruguay", "ecuador",
    "santiago", "bogota", "lima",

    # Middle East
    "uae", "dubai", "ksa", "saudi arabia", "riyadh", "jordan", "amman",
    "kuwait", "bahrain", "oman", "qatar", "doha",

    # Europe / UK / Oceania
    "paris", "lyon", "marseille", "berlin", "munich", "hamburg", "milan", "rome",
    "madrid", "barcelona", "amsterdam", "rotterdam", "brussels", "stockholm",
    "copenhagen", "oslo", "vienna", "zurich", "prague", "warsaw", "lisbon",
    "australia", "sydney", "melbourne", "new zealand", "auckland",
    "london", "manchester", "uk", "europe", "schengen", "worldwide"
]

NICHES = [
    # PR, Gifting & Brand Deals
    "first pr package unboxing",
    "pr unboxing small creator",
    "open for gifting grwm",
    "pr haul small creator",
    "small influencer pr unboxing",
    "brands that send pr to small creators",
    "micro influencer pr packages",
    "pr package haul unboxing",
    "gifted product review grwm",
    "reach out to brands with me",
    "pitching to brands small creator",
    "first brand deal grwm",
    "pr list small creator",
    "brands i work with micro creator",
    "brand package unboxing",
    "gifting collab haul",

    # Small & Nano Community Tags
    "small creator grwm",
    "nano beauty creator",
    "micro beauty creator",
    "small beauty creator",
    "under 1k beauty creator",
    "under 5k grwm",
    "under 2k makeup creator",
    "aspiring beauty creator grwm",
    "growing creator grwm",
    "support small beauty creators",
    "day in the life small creator",
    "small creator diary grwm",
    "content creator starter kit grwm",

    # Routine & Aesthetic
    "everyday grwm routine",
    "affordable grwm skincare routine",
    "clean girl grwm micro creator",
    "everyday makeup routine",
    "glowy base makeup tutorial",
    "morning skincare routine grwm",
    "night skincare routine aesthetic",
    "no makeup makeup routine",
    "get unready with me skincare",
    "skincare shelfie products review",
    "lip combo of the day grwm",
    "current skincare favorites",
    "glass skin routine grwm",

    # Casual Hauls & Reviews
    "ulta haul small creator",
    "sephora haul under 5k",
    "drugstore makeup finds haul",
    "skincare empties review",
    "honest beauty product review",
    "products i regret buying beauty",
    "skincare holy grails routine",
    "trying new beauty products grwm",
    "what is in my makeup bag",
    "affordable beauty dupes grwm",

    # Student / Relatable Lifestyle
    "college morning grwm routine",
    "get ready with me for uni",
    "high school grwm makeup",
    "quick 10 minute makeup grwm",
    "realistic morning routine grwm",

    # Jewelry PR & Gifting (Nano & Micro Focus)
    "jewelry pr package unboxing",
    "jewelry pr unboxing small creator",
    "jewelry brands that send pr to small creators",
    "small creator jewelry pr haul",
    "jewelry pr list small creator",
    "open for jewelry gifting grwm",
    "micro influencer jewelry pr",
    "first jewelry brand deal grwm",
    "pitching to jewelry brands small creator",
    "gifted jewelry review grwm",

    # Jewelry Stacking, Styling & Everyday Wear
    "everyday jewelry routine grwm",
    "jewelry stack of the day",
    "clean girl jewelry aesthetic",
    "how i style my gold jewelry",
    "ear stack styling aesthetic",
    "ring stack of the day",
    "waterproof jewelry review grwm",
    "affordable tarnish free jewelry",
    "everyday gold jewelry pieces",
    "minimalist jewelry styling grwm",
    "grwm putting on my jewelry",
    "silver and gold jewelry styling",

    # Jewelry Hauls & Collections (Nano Creators)
    "affordable jewelry haul under 5k",
    "jewelry haul small creator",
    "everyday jewelry collection tour",
    "unboxing jewelry package grwm",
    "favorite jewelry pieces aesthetic",
    "jewelry collection small creator",
    "top jewelry holy grails grwm"
]

YEAR_MODIFIERS = ["2026", "mini", "haul", "pr", "routine", "unboxing", "#smallcreator", "#microinfluencer", "#prunboxing", "#grwm"]

# ---------------- COMMERCIAL & AGENT EXCLUSIONS ----------------
MANAGEMENT_KEYWORDS = [
    "agency", "mgmt", "management", "talent", "represented",
    "press", "model agency", "talent group", "manager", "inquiries:"
]

SHOP_AND_BRAND_KEYWORDS = [
    "shop", "store", "boutique", "official", "handmade",
    "orders", "worldwide shipping", "free shipping",
    "order via", "link in bio to buy", "etsy", "shopify", "ceo", "founder",
    "wholesale", "b2b", "dropship", "catalog", "clothing brand", "cosmetics brand",
    "skincare brand", "jewelry brand", "brand owner", "our brand", "my brand",
    "dm to order", "dm for orders", "shop here", "shop our", "new collection", "restock",
    "magazine", "editorial", "publication", "media group", "journal",
    "gazette", "news", "daily news", "curated by", "feature page",
    "salon", "studio", "clinic", "spa", "lash tech", "nail tech",
    "appointments", "book now", "booking link", "dm to book", "hair salon"
]

UGC_KEYWORDS = ["ugc", "user generated content", "content creator for brands", "ugc creator"]
EXCLUDE_KEYWORDS = list(dict.fromkeys(SHOP_AND_BRAND_KEYWORDS + MANAGEMENT_KEYWORDS + UGC_KEYWORDS))

# Whole-word match on bio + username + email (lookarounds also work for "inquiries:").
_EXCLUDE_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(k) for k in EXCLUDE_KEYWORDS) + r")(?!\w)"
)
# Substring match on the username only for longer keywords (avoids "spa" in "spaghetti", "ceo" in "ceolia").
_USERNAME_SUBSTRINGS = [k for k in EXCLUDE_KEYWORDS if len(k) >= 4 and " " not in k and ":" not in k]

EXCLUDE_EMAIL_PREFIXES = (
    "support@", "orders@", "order@", "sales@", "service@", "help@",
    "wholesale@", "billing@", "press@", "media@", "admin@", "shop@", "info@"
)

AGENCY_DOMAIN_FRAGMENTS = ("talent", "agency", "mgmt", "management", "media", "influencer", "ugc")

THEME_PAGE_FRAGMENTS = [
    "makeup", "makup", "grwm", "cleangirl", "everyday", "inspo",
    "aesthetic", "outfit", "daily", "night", "vlog", "diary",
    "trends", "hacks", "tips", "finds", "routine", "club", "lifestyle"
]

IGNORED_EMAIL_DOMAINS = [
    "linktr.ee", "linktree.com", "beacons.ai", "sentry.io",
    "w3.org", "tiktok.com", "google.com", "schema.org", "example.com"
]

# ---------------- EMAIL EXTRACTION ----------------
EMAIL_CANDIDATE = re.compile(
    r'(?<![A-Za-z0-9._%+])'                                   # don't start mid-token
    r'([A-Za-z0-9][A-Za-z0-9._%+\-]*)'                        # local part (must start alnum)
    r'@'
    r'([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.([A-Za-z]{2,}))'  # domain + tld
)

PROVIDERS = r"(?:gmail|googlemail|outlook|hotmail|yahoo|ymail|icloud|live|aol|proton|protonmail|gmx|zoho)"

VALID_TLDS = {
    "com", "net", "org", "edu", "gov", "co", "io", "me", "info", "biz", "app",
    "xyz", "online", "studio", "shop", "store", "tv", "ai", "us", "uk", "ca",
    "au", "nz", "de", "fr", "es", "it", "nl", "be", "se", "no", "dk", "fi",
    "at", "ch", "cz", "pl", "pt", "ie", "mx", "cl", "pe", "uy", "ec", "ae",
    "sa", "jo", "kw", "bh", "om", "qa", "gr", "ro", "hu", "sk", "si", "hr",
    "lt", "lv", "ee", "is", "lu", "mt", "cy",
}

SOCIAL_LABELS = {
    "ig", "insta", "instagram", "snap", "snapchat", "sc", "tt", "tiktok",
    "twitter", "x", "yt", "youtube", "fb", "facebook", "pinterest", "pin",
    "handle", "user", "username", "follow", "dm",
}

# "pr-", "email:", "collab_" ... label glued to the front of the local part
LABEL_PREFIX = re.compile(
    r'^(?:pr|email|e-?mail|mail|contact|collabs?|biz|business|inquiries|inq|bookings?|for)[\-_:|]+',
    re.IGNORECASE,
)

_OBF_AT_BRACKET = re.compile(r'\s*[\(\[\{<]\s*at\s*[\)\]\}>]\s*', re.IGNORECASE)
_OBF_DOT_BRACKET = re.compile(r'\s*[\(\[\{<]\s*dot\s*[\)\]\}>]\s*', re.IGNORECASE)
_OBF_AT_WORD = re.compile(rf'\s+at\s+(?={PROVIDERS}\b)', re.IGNORECASE)
_OBF_AT_SPACED = re.compile(rf'\s*@\s*(?={PROVIDERS}\b)', re.IGNORECASE)
_OBF_DOT_PROVIDER = re.compile(rf'(@{PROVIDERS})\s+dot\s+', re.IGNORECASE)


def _normalize_obfuscation(text: str) -> str:
    t = _OBF_AT_BRACKET.sub('@', text)
    t = _OBF_DOT_BRACKET.sub('.', t)
    t = _OBF_AT_WORD.sub('@', t)        # bare " at gmail" only when a real provider follows
    t = _OBF_AT_SPACED.sub('@', t)      # "name @ gmail.com" only when a provider follows
    t = _OBF_DOT_PROVIDER.sub(r'\1.', t)
    return t


def extract_email(text: str) -> Optional[str]:
    if not text:
        return None

    normalized = _normalize_obfuscation(text)

    for m in EMAIL_CANDIDATE.finditer(normalized):
        local, domain, tld = m.group(1).lower(), m.group(2).lower(), m.group(3).lower()

        local = local.strip("._%+-")
        if STRIP_LABEL_PREFIX:
            stripped = LABEL_PREFIX.sub("", local)
            if len(stripped) >= 3:
                local = stripped.strip("._%+-")
        elif LABEL_PREFIX.match(local):
            continue

        if len(local) < 2:
            continue
        if local in SOCIAL_LABELS:                      # "insta@..." = Instagram handle
            continue
        if tld not in VALID_TLDS:                       # ".test", ".png", ".jpg", ...
            continue
        if any(ignored in domain for ignored in IGNORED_EMAIL_DOMAINS):
            continue

        return f"{local}@{domain}"

    return None


# ---------------- STATE & SIGNALS ----------------
_stop = False


def _handle_stop(signum, frame):
    global _stop
    if not _stop:
        print("\n[!] Stop signal received — finishing current profile, then exiting cleanly.")
    _stop = True


signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)

HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})


async def cancellable_sleep(duration_sec: float):
    slept = 0.0
    while slept < duration_sec and not _stop:
        chunk = min(1.0, duration_sec - slept)
        await asyncio.sleep(chunk)
        slept += chunk


CAPTCHA_SELECTOR = "#captcha_container, div[class*='captcha'], div[class*='verify-bar-close']"


async def check_for_captcha(page):
    try:
        captcha_el = await page.query_selector(CAPTCHA_SELECTOR)
        if captcha_el and await captcha_el.is_visible():
            print("\n" + "!" * 55)
            print("⚠️  CAPTCHA TRIGGERED! Solve it in the browser window.")
            print("!" * 55 + "\n\a", flush=True)
            while not _stop:
                await asyncio.sleep(2)
                el = await page.query_selector(CAPTCHA_SELECTOR)
                if not el or not await el.is_visible():
                    print("[+] Solved! Resuming scraper...\n")
                    await asyncio.sleep(1)
                    break
    except Exception:
        pass


# ---------------- PERSISTENCE ----------------
class SeenStore:
    """Keeps the seen set in memory and appends to disk through one open file handle."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.users: set = set()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.users = {line.strip().lower() for line in f if line.strip()}
        self._fh = open(path, "a", encoding="utf-8")

    def __contains__(self, username: str) -> bool:
        return username in self.users

    def __len__(self) -> int:
        return len(self.users)

    def add(self, username: str):
        username = username.strip().lower()
        if username in self.users:
            return
        self.users.add(username)
        self._fh.write(username + "\n")
        self._fh.flush()

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def save_lead_backup(lead_data: dict):
    try:
        os.makedirs(os.path.dirname(BACKUP_LEADS_FILE), exist_ok=True)
        with open(BACKUP_LEADS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(lead_data, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"      [!] Backup write error: {e}")


# ---------------- HELPERS ----------------
def generate_search_query() -> str:
    parts = []
    if random.random() < 0.6:                 # 60% chance to target a location
        parts.append(random.choice(LOCATIONS))
    parts.append(random.choice(NICHES))
    if random.random() < 0.3:                 # 30% chance to add a modifier
        parts.append(random.choice(YEAR_MODIFIERS))
    return " ".join(parts).strip()


def parse_follower_count(text: str) -> int:
    if not text:
        return 0
    clean = str(text).strip().lower().replace(",", "").replace(" ", "")
    multiplier = 1

    if clean.endswith("k"):
        multiplier, clean = 1000, clean[:-1]
    elif clean.endswith("m"):
        multiplier, clean = 1_000_000, clean[:-1]
    elif clean.endswith("b"):
        multiplier, clean = 1_000_000_000, clean[:-1]

    num_match = re.search(r"[\d.]+", clean)
    if not num_match:
        return 0
    try:
        return int(float(num_match.group(0)) * multiplier)
    except (ValueError, TypeError):
        return 0


def _fetch_email_from_bio_link(link_url: str) -> Optional[str]:
    """Blocking helper (run via asyncio.to_thread)."""
    if not link_url:
        return None
    try:
        if link_url.startswith("mailto:"):
            return extract_email(link_url[len("mailto:"):])
        if not link_url.startswith("http"):
            return None

        # Unwrap TikTok redirect URL
        if "tiktok.com/link/v2" in link_url:
            target = parse_qs(urlparse(link_url).query).get("target")
            if target:
                link_url = unquote(target[0])

        netloc = urlparse(link_url).netloc.lower()
        blocked = ("youtube.com", "instagram.com", "twitter.com", "x.com", "facebook.com", "tiktok.com")
        if any(b in netloc for b in blocked):
            return None

        resp = HTTP.get(link_url, timeout=BIO_LINK_TIMEOUT, allow_redirects=True, stream=True)
        if resp.status_code != 200:
            return None
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            return None

        raw = next(resp.iter_content(BIO_LINK_MAX_BYTES), b"")
        html = raw.decode(resp.encoding or "utf-8", errors="ignore")

        # mailto: links first (most reliable), then general text scan
        for m in re.findall(r'mailto:([^"\'?\s<>]+)', html, re.IGNORECASE):
            found = extract_email(unquote(m))
            if found:
                return found
        return extract_email(html)
    except Exception:
        return None


async def extract_email_from_bio_link(link_url: str) -> Optional[str]:
    return await asyncio.to_thread(_fetch_email_from_bio_link, link_url)


def is_in_excluded_region(text: str) -> bool:
    t = text.lower()
    return (any(p.search(t) for p in _REGION_WORD_PATTERNS)
            or any(p.search(t) for p in _REGION_TLD_PATTERNS))


def is_commercial_talent_or_excluded(username: str, bio: str, email: str) -> bool:
    u = username.lower()
    combined = f"{u} {bio.lower()} {email.lower()}"

    if _EXCLUDE_PATTERN.search(combined):
        return True
    if any(kw in u for kw in _USERNAME_SUBSTRINGS):
        return True
    if email.startswith(EXCLUDE_EMAIL_PREFIXES):
        return True

    domain = email.split("@")[-1].lower()
    return any(ag in domain for ag in AGENCY_DOMAIN_FRAGMENTS)


def is_theme_page_username(username: str) -> bool:
    clean_name = username.replace(".", "").replace("_", "").lower()
    return any(fragment in clean_name for fragment in THEME_PAGE_FRAGMENTS)


async def extract_profile_data(page) -> tuple:
    """
    Returns (followers | None, bio, bio_link). followers is None when it could not be read at all.
    1. Server-rendered rehydration JSON (__UNIVERSAL_DATA_FOR_REHYDRATION__ or SIGI_STATE).
    2. DOM fallback with explicit wait for client-side hydration.
    """
    followers: Optional[int] = None
    bio = ""
    bio_link = ""

    try:
        json_data = await page.evaluate("""() => {
            const el = document.querySelector('#__UNIVERSAL_DATA_FOR_REHYDRATION__') || document.querySelector('#SIGI_STATE');
            return el ? el.textContent : null;
        }""")
        if json_data:
            parsed = json.loads(json_data)

            scope = parsed.get("__DEFAULT_SCOPE__", {})
            user_detail = scope.get("webapp.user-detail", {}).get("userInfo", {})
            if user_detail:
                stats = user_detail.get("stats", {})
                user_info = user_detail.get("user", {})
                if stats.get("followerCount") is not None:
                    return (int(stats["followerCount"]),
                            user_info.get("signature", "") or "",
                            (user_info.get("bioLink") or {}).get("link", "") or "")

            stats_module = parsed.get("UserModule", {}).get("stats", {})
            user_module = parsed.get("UserModule", {}).get("users", {})
            if stats_module:
                key = next(iter(stats_module))
                if stats_module[key].get("followerCount") is not None:
                    u = user_module.get(key, {})
                    return (int(stats_module[key]["followerCount"]),
                            u.get("signature", "") or "",
                            (u.get("bioLink") or {}).get("link", "") or "")
    except Exception:
        pass

    # DOM fallback
    try:
        follower_el = await page.wait_for_selector(
            "strong[data-e2e='followers-count'], [data-e2e='followers-count']",
            timeout=3500
        )
        if follower_el:
            raw = (await follower_el.get_attribute("title")) or (await follower_el.inner_text())
            followers = parse_follower_count(raw)
    except Exception:
        try:
            follower_el = await page.query_selector("strong[title]")
            if follower_el:
                followers = parse_follower_count(await follower_el.get_attribute("title"))
        except Exception:
            pass

    try:
        bio_el = await page.query_selector("h2[data-e2e='user-bio'], [data-e2e='user-bio']")
        if bio_el:
            bio = await bio_el.inner_text()
    except Exception:
        pass

    try:
        link_el = await page.query_selector("a[data-e2e='user-link']")
        if link_el:
            bio_link = await link_el.get_attribute("href") or ""
    except Exception:
        pass

    return followers, bio, bio_link


def _post_to_n8n(payload: dict):
    return HTTP.post(N8N_PRODUCTION_WEBHOOK, json=payload, timeout=20)


# ---------------- PROFILE PIPELINE ----------------
async def process_profile(page, profile_url: str, seen: SeenStore) -> bool:
    username = profile_url.split("/@")[-1].strip().lower()

    if username in seen:
        return False

    def skip(reason: str):
        print(f"  Skip @{username}: {reason}")
        seen.add(username)
        return False

    if is_theme_page_username(username):
        return skip("Generic theme-page handle")

    await asyncio.sleep(random.uniform(1.0, 2.0))

    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=25000)
        await check_for_captcha(page)

        followers, bio_text, bio_link = await extract_profile_data(page)

        # Unreadable profile: don't blacklist, it may work on a later pass
        if followers is None:
            print(f"  Skip @{username}: could not read profile data (will retry later)")
            return False

        if followers < MIN_FOLLOWERS:
            return skip(f"{followers} followers (< {MIN_FOLLOWERS})")
        if followers > MAX_FOLLOWERS:
            return skip(f"{followers} followers (> {MAX_FOLLOWERS})")

        if is_in_excluded_region(bio_text):
            return skip("Excluded region detected in bio")

        # Email from bio first, then from the bio link (Linktree, Beacons, ...)
        email = extract_email(bio_text)
        email_source = "bio"
        if not email and bio_link:
            email = await extract_email_from_bio_link(bio_link)
            email_source = "bio_link"

        if not email:
            return skip("No valid email found in bio or link")

        if is_in_excluded_region(email):
            return skip("Excluded region domain detected in email")

        if is_commercial_talent_or_excluded(username, bio_text, email):
            return skip("Commercial entity, shop, or agency")

        # All criteria satisfied -> save and dispatch
        seen.add(username)
        print(f"  ✅ Qualified Lead: @{username} ({followers} followers) -> {email} [{email_source}]")

        lead_record = {
            "username": username,
            "profile_url": profile_url,
            "followers_count": followers,
            "bio": bio_text,
            "email": email,
            "email_source": email_source,
            "found_at": datetime.now(timezone.utc).isoformat(),
        }

        save_lead_backup(lead_record)  # local copy so the lead is NEVER lost

        try:
            res = await asyncio.to_thread(_post_to_n8n, {"leads": [lead_record]})
            print(f"      -> forwarded to n8n: {res.status_code}")
        except requests.RequestException as e:
            print(f"      -> n8n webhook failed (safely stored in local backup): {e}")

        return True

    except Exception as e:
        print(f"  Error on @{username}: {e}")
        return False


# ---------------- MAIN RUNNER ----------------
async def run_scraper():
    seen = SeenStore(SEEN_FILE)
    print(f"[+] Loaded {len(seen)} previously tracked creators.")

    batch_count = 0
    total_lifetime_leads = 0

    try:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                USER_DATA_DIR,
                headless=False,
                # channel="chrome", # Removed to force Playwright's bundled Chromium on Debian
                ignore_default_args=["--enable-automation", "--no-sandbox"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--test-type=false",
                ],
            )

            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )

            page = context.pages[0] if context.pages else await context.new_page()

            # Abort images, videos, and fonts to speed up loads & cut CPU/RAM
            BLOCKED_TYPES = {"image", "media", "font"}

            async def block_heavy_resources(route):
                if route.request.resource_type in BLOCKED_TYPES:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", block_heavy_resources)

            while not _stop:
                term = generate_search_query()
                print(f"\n🔍 Searching Videos for: '{term}'")
                search_url = f"https://www.tiktok.com/search/video?q={quote_plus(term)}"

                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                    await check_for_captcha(page)
                    await asyncio.sleep(random.uniform(*DELAY_BETWEEN_SEARCHES))

                    for _ in range(random.randint(3, 5)):
                        if page.is_closed() or _stop:
                            break
                        await page.evaluate(f"window.scrollBy(0, {random.randint(600, 1100)})")
                        await asyncio.sleep(random.uniform(0.9, 1.6))

                    user_links = await page.eval_on_selector_all(
                        "a[href*='/@']", "els => els.map(el => el.href)"
                    )
                    profile_urls = list(dict.fromkeys(
                        u for u in user_links
                        if re.match(r"https://www\.tiktok\.com/@[^/?]+$", u)
                    ))

                    for profile_url in profile_urls:
                        if _stop:
                            break

                        if await process_profile(page, profile_url, seen):
                            batch_count += 1
                            total_lifetime_leads += 1
                            print(f"      [Progress: {batch_count}/{BATCH_LEAD_LIMIT} in batch | Total: {total_lifetime_leads}]")

                            if batch_count >= BATCH_LEAD_LIMIT:
                                print(f"\n[🛑] Hit batch quota of {BATCH_LEAD_LIMIT} leads.")
                                print(f"[i] Resting for {int(BATCH_REST_DURATION / 60)} minutes...")
                                await cancellable_sleep(BATCH_REST_DURATION)
                                batch_count = 0
                                print("[▶️] Cooldown complete. Resuming...\n")
                                break

                            print(f"      [⏳] Pacing: cooling down for {int(DELAY_BETWEEN_EMAILS)}s before next lead...")
                            await cancellable_sleep(DELAY_BETWEEN_EMAILS)

                except Exception as search_err:
                    print(f"Search error on '{term}': {search_err}")
                    await asyncio.sleep(2)
                    continue

            await context.close()
    finally:
        seen.close()

    print(f"\n[+] Stopped cleanly. Total leads sent: {total_lifetime_leads}. Seen list preserved.")


if __name__ == "__main__":
    asyncio.run(run_scraper())

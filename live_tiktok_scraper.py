#!/usr/bin/env python3
"""
live_tiktok_scraper.py

Fast targeted TikTok lead scraper for beauty/jewelry/GRWM nano- & micro-influencers (10 - 5,000 followers).
- Strict non-shipping / excluded region filtering (Primary criteria).
- Follower range: 10 to 5,000 (Uses JSON rehydration + fallback to prevent 0-follower bugs).
- Disguised/obfuscated email parsing (e.g. "name (at) gmail (dot) com").
- Linktree / Bio-link fallback resolution to extract external emails.
- Local lead backup file to prevent data loss if n8n webhook drops.
- Resource route blocking (aborts videos/images) for 3x-5x faster scraping and lower RAM/CPU.
- Suppresses `--no-sandbox` and test-type flags to remove Chrome warning banners and evade bot checks.
- Instant dispatch to n8n upon meeting criteria with explicit skip logging.
"""

import asyncio
import json
import os
import random
import re
import signal
import sys
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote
from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------
N8N_PRODUCTION_WEBHOOK = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/tiktok-leads")
SEEN_FILE = os.getenv("SEEN_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_creators.txt"))
BACKUP_LEADS_FILE = os.getenv("BACKUP_LEADS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads_backup.jsonl"))
USER_DATA_DIR = os.getenv("USER_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profile"))

# Follower boundaries
MIN_FOLLOWERS = 10
MAX_FOLLOWERS = 5000

# Batch controls
BATCH_LEAD_LIMIT = 50
DELAY_BETWEEN_EMAILS = 30.0
BATCH_REST_DURATION = 10 * 60.0
DELAY_BETWEEN_SEARCHES = (2.0, 3.5)

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

YEAR_MODIFIERS = ["2026", "mini", "haul", "pr", "routine", "unboxing"]

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
    "gazette", "news", "press", "daily news", "curated by", "feature page",
    "salon", "studio", "clinic", "spa", "lash tech", "nail tech", 
    "appointments", "book now", "booking link", "dm to book", "hair salon"
]

UGC_KEYWORDS = ["ugc", "user generated content", "content creator for brands", "ugc creator"]
EXCLUDE_KEYWORDS = SHOP_AND_BRAND_KEYWORDS + MANAGEMENT_KEYWORDS + UGC_KEYWORDS

EXCLUDE_EMAIL_PREFIXES = [
    "support@", "orders@", "order@", "sales@", "service@", "help@", 
    "wholesale@", "billing@", "press@", "media@", "admin@", "shop@", "info@"
]

THEME_PAGE_FRAGMENTS = [
    "makeup", "makup", "grwm", "cleangirl", "everyday", "inspo", 
    "aesthetic", "outfit", "daily", "night", "vlog", "diary", 
    "trends", "hacks", "tips", "finds", "routine", "club", "lifestyle"
]

IGNORED_EMAIL_DOMAINS = [
    "linktr.ee", "linktree.com", "beacons.ai", "sentry.io", 
    "w3.org", "tiktok.com", "google.com", "schema.org", "example.com"
]

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

# ---------------- STATE & SIGNALS ----------------
_stop = False

def _handle_stop(signum, frame):
    global _stop
    if not _stop:
        print("\n[!] Stop signal received — finishing current profile, then exiting cleanly.")
    _stop = True

signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)

async def cancellable_sleep(duration_sec: float):
    slept = 0.0
    while slept < duration_sec and not _stop:
        chunk = min(1.0, duration_sec - slept)
        await asyncio.sleep(chunk)
        slept += chunk

async def check_for_captcha(page):
    try:
        captcha_el = await page.query_selector("#captcha_container, div[class*='captcha'], div[class*='verify-bar-close']")
        if captcha_el and await captcha_el.is_visible():
            print("\n" + "!" * 55)
            print("⚠️  CAPTCHA TRIGGERED! Solve it in the browser window.")
            print("!" * 55 + "\n\a", flush=True)
            while not _stop:
                await asyncio.sleep(2)
                el = await page.query_selector("#captcha_container, div[class*='captcha'], div[class*='verify-bar-close']")
                if not el or not await el.is_visible():
                    print("[+] Solved! Resuming scraper...\n")
                    await asyncio.sleep(1)
                    break
    except Exception:
        pass

def generate_search_query() -> str:
    niche = random.choice(NICHES)
    parts = []
    # 60% chance to target a location
    if random.random() < 0.6:
        parts.append(random.choice(LOCATIONS))
    parts.append(niche)
    # 30% chance to add year / style modifier
    if random.random() < 0.3:
        parts.append(random.choice(YEAR_MODIFIERS))
    return " ".join(parts).strip()

def load_seen_users() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(line.strip().lower() for line in f if line.strip())

def record_seen_user(username: str):
    os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(f"{username.strip().lower()}\n")

def save_lead_backup(lead_data: dict):
    try:
        os.makedirs(os.path.dirname(BACKUP_LEADS_FILE), exist_ok=True)
        with open(BACKUP_LEADS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(lead_data) + "\n")
    except Exception as e:
        print(f"      [!] Backup write error: {e}")

def parse_follower_count(text: str) -> int:
    if not text:
        return 0
    clean = str(text).strip().lower().replace(",", "").replace(" ", "")
    multiplier = 1
    
    if clean.endswith("k"):
        multiplier = 1000
        clean = clean[:-1]
    elif clean.endswith("m"):
        multiplier = 1_000_000
        clean = clean[:-1]
    elif clean.endswith("b"):
        multiplier = 1_000_000_000
        clean = clean[:-1]

    num_match = re.search(r"[\d.]+", clean)
    if not num_match:
        return 0
    try:
        return int(float(num_match.group(0)) * multiplier)
    except (ValueError, TypeError):
        return 0

def extract_email(text: str):
    if not text:
        return None
    # Normalize common obfuscations: "name (at) domain (dot) com", "name [at] domain.com"
    normalized = re.sub(r'[\(\[\{]\s*(at|@)\s*[\)\]\}]|\s+at\s+', '@', text, flags=re.IGNORECASE)
    normalized = re.sub(r'[\(\[\{]\s*(dot|\.)\s*[\)\]\}]|\s+dot\s+', '.', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'\s*@\s*', '@', normalized)
    normalized = re.sub(r'(\w+@\w+)\s+\.(\w+)', r'\1.\2', normalized)

    match = EMAIL_REGEX.search(normalized)
    if match:
        email = match.group(0).lower()
        domain = email.split("@")[-1]
        if not any(ignored in domain for ignored in IGNORED_EMAIL_DOMAINS):
            return email
    return None

def extract_email_from_bio_link(link_url: str):
    if not link_url:
        return None
    try:
        # Direct mailto: link
        if link_url.startswith("mailto:"):
            return extract_email(link_url.replace("mailto:", ""))
        
        # Only inspect HTTP/HTTPS links
        if not link_url.startswith("http"):
            return None

        # Clean TikTok redirect URL if present
        if "tiktok.com/link/v2" in link_url:
            parsed = parse_qs(urlparse(link_url).query)
            target = parsed.get("target")
            if target:
                link_url = unquote(target[0])

        parsed_url = urlparse(link_url)
        # Avoid heavy non-creator platforms
        blocked = ["youtube.com", "instagram.com", "twitter.com", "x.com", "facebook.com", "tiktok.com"]
        if any(b in parsed_url.netloc.lower() for b in blocked):
            return None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(link_url, headers=headers, timeout=4, allow_redirects=True)
        if resp.status_code == 200:
            # Check for mailto: in the HTML first
            mailto_matches = re.findall(r'mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', resp.text, re.IGNORECASE)
            for m in mailto_matches:
                found = extract_email(m)
                if found:
                    return found
            # Fallback to general email search in page text
            found = extract_email(resp.text)
            if found:
                return found
    except Exception:
        pass
    return None

def is_in_excluded_region(text: str) -> bool:
    t_lower = text.lower()
    for term in EXCLUDED_REGION_TERMS:
        if term.startswith("."):
            if term in t_lower:
                return True
        else:
            if re.search(rf"\b{re.escape(term)}\b", t_lower):
                return True
    return False

def is_commercial_talent_or_excluded(username: str, bio: str, email: str) -> bool:
    combined = f"{username.lower()} {bio.lower()} {email.lower()}"
    
    if any(re.search(rf"\b{re.escape(kw)}\b", combined) or kw in username.lower() for kw in EXCLUDE_KEYWORDS):
        return True
        
    if any(email.startswith(prefix) for prefix in EXCLUDE_EMAIL_PREFIXES):
        return True

    agency_domains = ["talent", "agency", "mgmt", "management", "media", "influencer", "ugc"]
    domain = email.split("@")[-1].lower()
    if any(ag in domain for ag in agency_domains):
        return True
    return False

def is_theme_page_username(username: str) -> bool:
    clean_name = username.replace(".", "").replace("_", "").lower()
    return any(fragment in clean_name for fragment in THEME_PAGE_FRAGMENTS)

async def extract_profile_data(page) -> tuple[int, str, str]:
    """
    Extracts followers, bio, and external bio link reliably:
    1. Reads server-rendered rehydration JSON script (__UNIVERSAL_DATA_FOR_REHYDRATION__ or SIGI_STATE).
    2. Falls back to DOM queries with explicit wait to handle client-side hydration.
    """
    followers = 0
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
                followers = stats.get("followerCount")
                bio = user_info.get("signature", "")
                bio_link = user_info.get("bioLink", {}).get("link", "")
                if followers is not None:
                    return int(followers), bio, bio_link

            stats_module = parsed.get("UserModule", {}).get("stats", {})
            user_module = parsed.get("UserModule", {}).get("users", {})
            if stats_module:
                first_key = next(iter(stats_module))
                followers = stats_module[first_key].get("followerCount")
                bio = user_module.get(first_key, {}).get("signature", "")
                bio_link = user_module.get(first_key, {}).get("bioLink", {}).get("link", "")
                if followers is not None:
                    return int(followers), bio, bio_link
    except Exception:
        pass

    try:
        follower_el = await page.wait_for_selector(
            "strong[data-e2e='followers-count'], [data-e2e='followers-count']", 
            timeout=3500
        )
        if follower_el:
            raw_title = await follower_el.get_attribute("title")
            raw_text = await follower_el.inner_text()
            follower_str = raw_title if raw_title else raw_text
            followers = parse_follower_count(follower_str)
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

# ---------------- PROFILE PIPELINE ----------------
async def process_profile(page, profile_url: str, seen_users: set) -> bool:
    username = profile_url.split("/@")[-1].strip().lower()
    
    if username in seen_users:
        return False

    if is_theme_page_username(username):
        print(f"  Skip @{username}: Generic theme-page handle")
        seen_users.add(username)
        record_seen_user(username)
        return False

    await asyncio.sleep(random.uniform(1.0, 2.0))

    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=25000)
        await check_for_captcha(page)

        followers, bio_text, bio_link = await extract_profile_data(page)

        if followers < MIN_FOLLOWERS:
            print(f"  Skip @{username}: {followers} followers (< {MIN_FOLLOWERS})")
            seen_users.add(username)
            record_seen_user(username)
            return False
        if followers > MAX_FOLLOWERS:
            print(f"  Skip @{username}: {followers} followers (> {MAX_FOLLOWERS})")
            seen_users.add(username)
            record_seen_user(username)
            return False

        # Strict Region Checks
        if is_in_excluded_region(bio_text):
            print(f"  Skip @{username}: Excluded region detected in bio")
            seen_users.add(username)
            record_seen_user(username)
            return False

        # Extract email from bio text first
        email = extract_email(bio_text)
        email_source = "bio"

        # If not in bio text, attempt extraction from bio link (Linktree, Beacons, etc.)
        if not email and bio_link:
            email = extract_email_from_bio_link(bio_link)
            if email:
                email_source = "bio_link"

        if not email:
            print(f"  Skip @{username}: No email found in bio or link")
            seen_users.add(username)
            record_seen_user(username)
            return False

        if is_in_excluded_region(email):
            print(f"  Skip @{username}: Excluded region domain detected in email")
            seen_users.add(username)
            record_seen_user(username)
            return False

        if is_commercial_talent_or_excluded(username, bio_text, email):
            print(f"  Skip @{username}: Commercial entity, shop, or agency")
            seen_users.add(username)
            record_seen_user(username)
            return False

        # All criteria satisfied -> Save and dispatch
        seen_users.add(username)
        record_seen_user(username)
        print(f"  ✅ Qualified Lead: @{username} ({followers} followers) -> {email} [{email_source}]")

        lead_record = {
            "username": username,
            "profile_url": profile_url,
            "followers": followers,
            "followers_count": followers,
            "bio": bio_text,
            "email": email,
            "email_source": email_source,
            "found_at": datetime.now(timezone.utc).isoformat(),
        }

        # Backup locally so lead is NEVER lost
        save_lead_backup(lead_record)

        payload = {"leads": [lead_record]}
        try:
            res = requests.post(N8N_PRODUCTION_WEBHOOK, json=payload, timeout=20)
            print(f"      -> forwarded to n8n: {res.status_code}")
        except requests.RequestException as e:
            print(f"      -> n8n webhook failed (safely stored in local backup): {e}")

        return True

    except Exception as e:
        print(f"  Error on @{username}: {e}")
        return False

# ---------------- MAIN RUNNER ----------------
async def run_scraper():
    seen_users = load_seen_users()
    print(f"[+] Loaded {len(seen_users)} previously tracked creators.")

    batch_count = 0
    total_lifetime_leads = 0

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            channel="chrome",
            ignore_default_args=[
                "--enable-automation",
                "--no-sandbox"
            ],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--test-type=false"
            ]
        )

        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        page = context.pages[0] if context.pages else await context.new_page()

        # Route blocking: Abort images, videos, and fonts to accelerate page loads & reduce CPU/RAM
        async def block_heavy_resources(route):
            if route.request.resource_type in ["image", "media", "font"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_heavy_resources)

        while not _stop:
            term = generate_search_query()
            print(f"\n🔍 Searching Videos for: '{term}'")
            search_url = f"https://www.tiktok.com/search/video?q={term}"

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
                    "a[href*='/@']",
                    "elements => elements.map(el => el.href)"
                )
                profile_urls = list(dict.fromkeys(
                    u for u in user_links
                    if re.match(r"https://www\.tiktok\.com/@[^/?]+$", u)
                ))

                for profile_url in profile_urls:
                    if _stop:
                        break

                    lead_sent = await process_profile(page, profile_url, seen_users)
                    if lead_sent:
                        batch_count += 1
                        total_lifetime_leads += 1
                        print(f"      [Progress: {batch_count}/{BATCH_LEAD_LIMIT} leads in current batch | Total: {total_lifetime_leads}]")

                        if batch_count >= BATCH_LEAD_LIMIT:
                            print(f"\n[🛑] Hit batch quota of {BATCH_LEAD_LIMIT} leads.")
                            print(f"[i] Resting for {int(BATCH_REST_DURATION / 60)} minutes...")
                            await cancellable_sleep(BATCH_REST_DURATION)
                            batch_count = 0
                            print("[▶️] Cooldown complete. Resuming...\n")
                            break
                        else:
                            print(f"      [⏳] Pacing: cooling down for {int(DELAY_BETWEEN_EMAILS)}s before next lead...")
                            await cancellable_sleep(DELAY_BETWEEN_EMAILS)

            except Exception as search_err:
                print(f"Search error on '{term}': {search_err}")
                await asyncio.sleep(2)
                continue

        await context.close()
        print(f"\n[+] Stopped cleanly. Total leads sent: {total_lifetime_leads}. Seen list preserved.")

if __name__ == "__main__":
    asyncio.run(run_scraper())

#!/usr/bin/env python3
"""Durable, concurrent TikTok lead scraper. Independent of the original script.

Run --help for controls. Default state lives beside this file in scraper_v2_state.
The original browser profile, seen list, script and backups are never modified.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import html
import ipaddress
import json
import logging
import math
import os
import random
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright

LOG = logging.getLogger("tiktok-v2")
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DEFAULT_WEBHOOK = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/tiktok-leads")


class HighlightFormatter(logging.Formatter):
    COLORS = {"qualified": "\033[1;36m", "sent": "\033[1;32m",
              "retry": "\033[1;33m", "held": "\033[1;33m", "mode": "\033[1;36m"}

    def format(self, record):
        message = super().format(record)
        color = self.COLORS.get(getattr(record, "lead_event", ""))
        if color and sys.stderr.isatty() and "NO_COLOR" not in os.environ:
            return color + message + "\033[0m"
        return message


def lead_notice(event, title, lines, level=logging.INFO):
    border = "=" * 72
    LOG.log(level, "\n%s\n  %s\n%s\n%s\n%s", border, title, border,
            "\n".join("  " + str(line) for line in lines), border,
            extra={"lead_event": event})


def progress_text(store):
    summary = store.summary()
    delivery = summary["deliveries"]
    qualified = sum(count for status, count in delivery.items() if status != "external")
    retrying = store.conn.execute(
        "SELECT COUNT(*) FROM leads WHERE delivery_status='pending' AND attempts>0"
    ).fetchone()[0]
    return (
        f"QUALIFIED: {qualified} | N8N ACCEPTED: {delivery.get('sent', 0)} | "
        f"QUEUED: {delivery.get('pending', 0)} | SENDING: {delivery.get('inflight', 0)} | "
        f"RETRYING: {retrying} | DRY-RUN HELD: {delivery.get('held', 0)} | "
        f"PROFILES WAITING: {summary['backlog']} | LEGACY IMPORTED: {delivery.get('external', 0)}"
    )

# Shipping exclusions retained as data; no import of the running script.
EXCLUDED_REGION_TERMS = ['brazil', 'brasil', 'rio', 'são paulo', '.com.br', '🇧🇷', 'venezuela', 'bolivia', 'argentina',
 'buenos aires', '🇦🇷', '.ar', 'africa', 'nigeria', 'lagos', 'kenya', 'nairobi',
 'south africa', 'ghana', 'egypt', 'morocco', 'algeria', 'tunisia', '🇿🇦', '🇳🇬', '🇰🇪', '🇲🇦',
 '🇪🇬', 'russia', 'moscow', '.ru', '🇷🇺', 'ukraine', 'kyiv', '.ua', '🇺🇦', 'israel', 'tel aviv',
 '🇮🇱', 'palestine', 'gaza', '🇵🇸', 'syria', 'yemen', 'sudan', 'lebanon', 'iraq', 'pakistan',
 'islamabad', 'karachi', 'lahore', '🇵🇰', '.pk', 'kazakhstan', '.kz', 'uzbekistan', '.uz',
 'turkmenistan', '.tm', 'tajikistan', '.tj', 'kyrgyzstan', '.kg', 'afghanistan', '.af',
 'india', 'delhi', 'mumbai', 'bangalore', '.in', '🇮🇳', 'bangladesh']

LOCATIONS = ['us', 'usa', 'united states', 'canada', 'mexico', 'new york', 'los angeles', 'chicago',
 'toronto', 'vancouver', 'miami', 'chile', 'colombia', 'peru', 'uruguay', 'ecuador',
 'santiago', 'bogota', 'lima', 'uae', 'dubai', 'ksa', 'saudi arabia', 'riyadh', 'jordan',
 'amman', 'kuwait', 'bahrain', 'oman', 'qatar', 'doha', 'paris', 'lyon', 'marseille',
 'berlin', 'munich', 'hamburg', 'milan', 'rome', 'madrid', 'barcelona', 'amsterdam',
 'rotterdam', 'brussels', 'stockholm', 'copenhagen', 'oslo', 'vienna', 'zurich', 'prague',
 'warsaw', 'lisbon', 'australia', 'sydney', 'melbourne', 'new zealand', 'auckland', 'london',
 'manchester', 'uk', 'europe', 'schengen', 'worldwide']

# Every query carries a small-account signal. Generic beauty/PR searches and
# "micro influencer" terms often fill the queue with established creators.
NICHES = [
    'small creator grwm', 'nano beauty creator', 'small beauty creator',
    'under 1k beauty creator', 'under 2k makeup creator', 'under 5k grwm',
    'makeup creator under 5k', 'skincare creator under 5k',
    'road to 1k beauty creator', 'road to 5k grwm',
    'my first pr package small creator', 'pr unboxing small creator',
    'pr haul small creator', 'small influencer pr unboxing',
    'first gifted collaboration small creator', 'small creator unboxing gifted package',
    'small creator beauty haul', 'small creator skincare routine',
    'small creator makeup routine', 'small creator lip combo',
    'small creator jewelry pr haul', 'jewelry pr unboxing small creator',
    'jewelry haul small creator', 'jewelry collection small creator',
    'jewelry creator under 5k', 'nano jewelry creator',
    'small creator jewelry styling', 'small creator necklace stack',
    'small creator accessories haul', 'small fashion creator grwm',
    'fashion creator under 5k', 'nano fashion creator',
    'lifestyle creator under 5k', 'daily vlog small creator',
    'day in the life small creator', 'small creator diary grwm',
    'small creator pr friendly', 'open to gifted collabs small creator',
    'nano influencer brand collab', 'small creator business email in bio',
]

SEARCH_MODIFIERS = ['unboxing', 'routine', 'review', 'gifted', 'collab', '#grwm']

HASHTAG_SEARCHES = [
    '#smallcreator #grwm', '#nanoinfluencer beauty', '#smallcreatortiktok makeup',
    '#smallinfluencer skincare', '#smallcreator #prunboxing',
    '#nanocreator jewelry', '#smallcreator #jewelrytok',
    '#under1k beauty', '#under5k grwm', '#roadto1k makeup',
    '#smallcreator #fashiontok', '#nanoinfluencer lifestyle',
]



def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical_profile_url(value):
    """Accept creator and video links, discarding tracking and video suffixes."""
    parsed = urlparse(urljoin("https://www.tiktok.com", value))
    if parsed.scheme not in ("http", "https") or parsed.hostname not in {
        "www.tiktok.com", "tiktok.com", "m.tiktok.com"
    }:
        return None
    match = re.match(r"^/@([A-Za-z0-9._]{1,128})(?:/|$)", unquote(parsed.path))
    return f"https://www.tiktok.com/@{match[1].lower()}" if match else None


EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9][A-Za-z0-9._%+\-]*)"
    r"@([A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+)"
    r"(?![A-Za-z0-9_\-])"
)
IGNORED_DOMAINS = {
    "linktr.ee", "linktree.com", "beacons.ai", "sentry.io", "w3.org",
    "tiktok.com", "google.com", "schema.org", "example.com", "example.org",
}
SOCIAL_LABELS = {"ig", "insta", "instagram", "snap", "snapchat", "tt", "tiktok",
                 "twitter", "youtube", "facebook", "username", "handle"}
FILE_SUFFIXES = {"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "woff", "mp4", "test"}
SOCIAL_HOSTS = {"youtube.com", "youtu.be", "instagram.com", "twitter.com", "x.com",
                "facebook.com", "tiktok.com", "pinterest.com"}
BIO_HOSTS = {"linktr.ee", "linktree.com", "beacons.ai", "bio.site", "hoo.be", "campsite.bio"}


def domain_matches(host, domains):
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def extract_emails(value):
    """Normalize obfuscation, never rewrite a valid email's local part."""
    value = html.unescape(value or "").replace("\\u0040", "@").replace("\\u002e", ".")
    value = re.sub(r"\s*[\[({<]\s*at\s*[\])}>]\s*", "@", value, flags=re.I)
    value = re.sub(r"\s*[\[({<]\s*dot\s*[\])}>]\s*", ".", value, flags=re.I)
    providers = r"(?:gmail|googlemail|outlook|hotmail|yahoo|icloud|protonmail|proton|live|aol|gmx|zoho)"
    value = re.sub(rf"\s+(?:at|@)\s+(?={providers}\b)", "@", value, flags=re.I)
    value = re.sub(rf"(@{providers})\s+dot\s+", r"\1.", value, flags=re.I)
    found = {}
    for match in EMAIL_PATTERN.finditer(value):
        local, domain = match[1], match[2].lower().rstrip(".")
        suffix = domain.rsplit(".", 1)[-1]
        if (len(local) > 64 or local.endswith(".") or ".." in local
                or local.lower() in SOCIAL_LABELS or not re.fullmatch(r"[a-z]{2,63}", suffix)
                or suffix in FILE_SUFFIXES or domain_matches(domain, IGNORED_DOMAINS)):
            continue
        address = f"{local}@{domain}"
        if len(address) <= 254:
            found.setdefault(address.lower(), address)
    return list(found.values())


def parse_follower_count(value):
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if math.isfinite(value) and value >= 0 else None
    clean = re.sub(r"[\s\u00a0\u202f]", "", str(value).lower())
    match = re.fullmatch(r"([0-9][0-9.,]*)([kmb]?)", clean)
    if not match:
        return None
    number, suffix = match.groups()
    if suffix and "," in number and "." not in number:
        number = number.replace(",", ".") if len(number.rsplit(",", 1)[-1]) != 3 else number.replace(",", "")
    else:
        number = number.replace(",", "")
    try:
        return int(float(number) * {"": 1, "k": 1000, "m": 1_000_000, "b": 1_000_000_000}[suffix])
    except (ValueError, OverflowError):
        return None


def excluded_region(bio, email=""):
    # Rio is a person's name too. Keep the unambiguous city name.
    terms = [term for term in EXCLUDED_REGION_TERMS if not term.startswith(".") and term != "rio"]
    terms.append("rio de janeiro")
    lower = bio.lower()
    for term in terms:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", lower):
            return term
    domains = [address.split("@", 1)[1] for address in extract_emails(bio + " " + email)]
    domains.extend((urlparse(url).hostname or "").lower()
                   for url in re.findall(r"https?://[^\s<>]+", bio, flags=re.I))
    for term in EXCLUDED_REGION_TERMS:
        if term.startswith(".") and any(domain.endswith(term) for domain in domains):
            return term
    return None


def exclusion_reason(username, bio, email="", include_ugc=False):
    """Preserve business exclusions using context rather than creator handle words."""
    lower = bio.lower()
    if re.search(r"\b(?:fan\s*page|repost\s*(?:page|account)|not my (?:videos|content))\b", lower):
        return "explicit repost/fan page"
    if not include_ugc and re.search(r"\b(?:ugc|user generated content|content creator for brands)\b", lower):
        return "UGC creator (use --include-ugc to include)"
    if re.search(r"\b(?:represented by|managed by|talent agency)\b|\b(?:management|mgmt)\s*:", lower):
        return "agency representation"
    if re.search(r"\b(?:dm (?:to|for) order(?:s)?|wholesale|shop our|order via|our (?:jewelry|cosmetics|clothing) brand)\b", lower):
        return "explicit shop/brand sales"
    business = bool(re.search(r"\b(?:boutique|shopify|etsy|brand owner|our store|our shop)\b", lower))
    sales = bool(re.search(r"\b(?:orders|shipping|restock|catalog|new collection)\b", lower))
    if business and sales:
        return "multiple shop/brand signals"
    if re.search(r"\b(?:appointments|dm to book|book now)\b", lower) and re.search(
        r"\b(?:salon|clinic|lash tech|nail tech|spa)\b", lower
    ):
        return "appointment business"
    if email:
        local, domain = email.lower().split("@", 1)
        if local in {"support", "orders", "order", "sales", "service", "help", "wholesale", "billing"}:
            return "business service address"
        if re.search(r"(?:^|[.\-])(?:agency|mgmt|management|talent)(?:[.\-]|$)", domain):
            return "agency email domain"
    return None


class Store:
    """Only accessed on the event-loop thread. Claims and lead/outbox writes are atomic."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                username TEXT PRIMARY KEY, url TEXT NOT NULL, source_query TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', next_due REAL, attempts INTEGER DEFAULT 0,
                data TEXT, reason TEXT, discovered REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,next_due);
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, email_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, delivery_status TEXT NOT NULL, attempts INTEGER DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS delivery_due ON leads(delivery_status,next_attempt);
            CREATE TABLE IF NOT EXISTS queries (
                term TEXT PRIMARY KEY, last_run REAL DEFAULT 0, runs INTEGER DEFAULT 0,
                discovered INTEGER DEFAULT 0, checked INTEGER DEFAULT 0, leads INTEGER DEFAULT 0,
                seconds REAL DEFAULT 0, errors INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1, range_checked INTEGER DEFAULT 0, in_range INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cache (url TEXT PRIMARY KEY, expires REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        with self.conn:
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(queries)")}
            for name, default in (("active", 1), ("range_checked", 0), ("in_range", 0)):
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE queries ADD COLUMN {name} INTEGER DEFAULT {default}")
            self.conn.execute("UPDATE jobs SET status='pending' WHERE status='in_profile'")
            self.conn.execute("UPDATE jobs SET status='enrichment_pending' WHERE status='in_email'")
            self.conn.execute("UPDATE leads SET delivery_status='pending' WHERE delivery_status='inflight'")

    def close(self):
        self.conn.close()

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def add_queries(self, terms):
        with self.conn:
            self.conn.executemany("INSERT OR IGNORE INTO queries(term) VALUES (?)", ((term,) for term in terms))

    def configure_discovery(self, terms, min_followers, max_followers):
        """Retire old broad searches while retaining their queued jobs and history."""
        limits = [min_followers, max_followers]
        reseed = self.get_meta("discovery_follower_range") != limits
        with self.conn:
            self.conn.execute("UPDATE queries SET active=0")
            self.conn.executemany("""INSERT INTO queries(term,active) VALUES (?,1)
                ON CONFLICT(term) DO UPDATE SET active=1""", ((term,) for term in terms))
            if reseed:
                # Bootstrap the new counters from readable saved profiles on upgrade.
                # Changing --min/--max-followers also discards scores for the old range.
                counts = {}
                for row in self.conn.execute("SELECT source_query,data FROM jobs WHERE data IS NOT NULL"):
                    try:
                        count = parse_follower_count(json.loads(row["data"]).get("followers_count"))
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if count is not None:
                        values = counts.setdefault(row["source_query"], [0, 0])
                        values[0] += 1
                        values[1] += min_followers <= count <= max_followers
                self.conn.execute("UPDATE queries SET range_checked=0,in_range=0")
                self.conn.executemany("UPDATE queries SET range_checked=?,in_range=? WHERE term=?",
                                      ((checked, matched, term) for term, (checked, matched) in counts.items()))
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  ("discovery_follower_range", json.dumps(limits)))

    def choose_query(self):
        rows = self.conn.execute("SELECT * FROM queries WHERE active=1 AND last_run<?",
                                 (time.time() - 1800,)).fetchall()
        if not rows:
            return None
        unexplored = [row for row in rows if row["runs"] == 0]
        explored = [row for row in rows if row["runs"] > 0]
        if unexplored and (not explored or random.random() < 0.25):
            return random.choice(unexplored)["term"]
        # Mostly reward actual follower-range fit, with a smaller bonus for email
        # leads. Raw discovery volume must not reward a search full of big accounts.
        def score(row):
            return (0.8 * (row["in_range"] + 1) / (row["range_checked"] + 2)
                    + 0.2 * (row["leads"] + 0.25) / (row["checked"] + 2))
        candidates = explored or rows
        best = max(score(row) for row in candidates)
        return random.choice([row for row in candidates if score(row) >= best - 0.02])["term"]

    def search_finished(self, term, discovered, seconds, error=False):
        with self.conn:
            self.conn.execute("""UPDATE queries SET last_run=?,runs=runs+1,
                discovered=discovered+?,seconds=seconds+?,errors=errors+? WHERE term=?""",
                (time.time(), discovered, seconds, int(error), term))

    def profile_finished(self, term, seconds, followers=None, min_followers=10, max_followers=5000):
        with self.conn:
            self.conn.execute("""UPDATE queries SET checked=checked+1,seconds=seconds+?,
                range_checked=range_checked+?,in_range=in_range+? WHERE term=?""",
                (seconds, int(followers is not None),
                 int(followers is not None and min_followers <= followers <= max_followers), term))

    def add_candidate(self, url, term):
        url = canonical_profile_url(url)
        if not url:
            return False
        username = url.split("/@", 1)[1]
        now = time.time()
        with self.conn:
            if self.conn.execute("SELECT 1 FROM leads WHERE username=?", (username,)).fetchone():
                return False
            cursor = self.conn.execute("""INSERT OR IGNORE INTO jobs
                (username,url,source_query,next_due,discovered,updated) VALUES (?,?,?,?,?,?)""",
                (username, url, term, now, now, now))
            return cursor.rowcount == 1

    def claim_job(self, enrichment=False):
        states = ("enrichment_pending", "enrichment_error") if enrichment else (
            "pending", "temporary_error", "no_email", "out_of_range", "excluded"
        )
        placeholders = ",".join("?" for _ in states)
        with self.conn:
            row = self.conn.execute(f"""SELECT * FROM jobs WHERE status IN ({placeholders})
                AND next_due<=? ORDER BY attempts,discovered LIMIT 1""", (*states, time.time())).fetchone()
            if row:
                self.conn.execute("UPDATE jobs SET status=?,attempts=attempts+1,updated=? WHERE username=?",
                                  ("in_email" if enrichment else "in_profile", time.time(), row["username"]))
                return dict(row)
        return None

    def finish_job(self, username, status, reason="", delay=None, data=None):
        with self.conn:
            self.conn.execute("""UPDATE jobs SET status=?,reason=?,next_due=?,updated=?,
                data=COALESCE(?,data) WHERE username=?""", (
                status, reason, time.time() + delay if delay is not None else None, time.time(),
                json.dumps(data, ensure_ascii=False) if data is not None else None, username))

    def duplicate(self, username, email):
        return self.conn.execute("SELECT 1 FROM leads WHERE username=? OR email_key=?",
                                 (username.lower(), email.lower())).fetchone() is not None

    def save_lead(self, record, dry_run=False):
        """Lead persistence and candidate completion commit together before dispatch."""
        with self.conn:
            cursor = self.conn.execute("""INSERT OR IGNORE INTO leads
                (username,email_key,payload,delivery_status,created) VALUES (?,?,?,?,?)""", (
                record["username"], record["email"].lower(), json.dumps(record, ensure_ascii=False),
                "held" if dry_run else "pending", time.time()))
            inserted = cursor.rowcount == 1
            self.conn.execute("UPDATE jobs SET status=?,next_due=NULL,updated=? WHERE username=?",
                              ("qualified" if inserted else "duplicate", time.time(), record["username"]))
            if inserted:
                self.conn.execute("UPDATE queries SET leads=leads+1 WHERE term=?", (record["source_query"],))
        return inserted

    def import_external(self, records):
        added = 0
        with self.conn:
            for record in records:
                username = str(record.get("username", "")).strip().lstrip("@").lower()
                email = str(record.get("email", "")).strip()
                if not username or not extract_emails(email):
                    continue
                record = {**record, "username": username}
                cursor = self.conn.execute("""INSERT OR IGNORE INTO leads
                    (username,email_key,payload,delivery_status,created) VALUES (?,?,?,?,?)""",
                    (username, email.lower(), json.dumps(record, ensure_ascii=False), "external", time.time()))
                added += cursor.rowcount
                # The old backup means the old scraper owns delivery, even if its webhook failed.
                self.conn.execute("""UPDATE leads SET delivery_status='external'
                    WHERE (username=? OR email_key=?) AND delivery_status IN ('pending','held')""",
                    (username, email.lower()))
                self.conn.execute("UPDATE jobs SET status='duplicate',next_due=NULL WHERE username=? AND status NOT IN ('in_profile','in_email')",
                                  (username,))
        return added

    def claim_delivery(self):
        with self.conn:
            row = self.conn.execute("""SELECT * FROM leads WHERE delivery_status='pending'
                AND next_attempt<=? ORDER BY id LIMIT 1""", (time.time(),)).fetchone()
            if row:
                self.conn.execute("UPDATE leads SET delivery_status='inflight',attempts=attempts+1 WHERE id=?", (row["id"],))
                return dict(row)
        return None

    def delivery_result(self, lead_id, success, error="", delay=0):
        with self.conn:
            self.conn.execute("UPDATE leads SET delivery_status=?,last_error=?,next_attempt=? WHERE id=?",
                              ("sent" if success else "pending", error[:500], time.time() + delay, lead_id))

    def backlog(self):
        return self.conn.execute("""SELECT COUNT(*) FROM jobs WHERE
            (status IN ('pending','temporary_error','no_email','out_of_range','excluded') AND next_due<=?)
            OR status IN ('in_profile','enrichment_pending','in_email')
            OR (status='enrichment_error' AND next_due<=?)""", (time.time(), time.time())).fetchone()[0]

    def cache_get(self, url):
        row = self.conn.execute("SELECT data FROM cache WHERE url=? AND expires>?", (url, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, url, result):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                              (url, time.time() + (60 if result.get("error") else 86400), json.dumps(result)))

    def summary(self):
        jobs = dict(self.conn.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
        leads = dict(self.conn.execute("SELECT delivery_status,COUNT(*) FROM leads GROUP BY delivery_status"))
        return {"profiles": jobs, "deliveries": leads, "backlog": self.backlog()}

    def export(self, path):
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            for row in self.conn.execute("SELECT payload FROM leads WHERE delivery_status!='external' ORDER BY id"):
                handle.write(row[0] + "\n")
        temp.replace(path)


class LegacyImporter:
    """Tail only JSONL backups; old seen files have no trustworthy rejection reasons."""

    def __init__(self, store, paths):
        self.store, self.paths = store, list(dict.fromkeys(Path(p).resolve() for p in paths))
        self.last_poll = 0

    def poll(self, force=False):
        if not force and time.monotonic() - self.last_poll < 5:
            return
        self.last_poll = time.monotonic()
        for path in self.paths:
            try:
                stat = path.stat()
                key = "legacy:" + str(path)
                previous = self.store.get_meta(key, {})
                identity = [stat.st_dev, stat.st_ino]
                offset = previous.get("offset", 0) if previous.get("identity") == identity else 0
                if offset > stat.st_size:
                    offset = 0
                if previous.get("identity") == identity and offset == stat.st_size:
                    continue
                records = []
                with path.open("rb") as handle:
                    handle.seek(offset)
                    for _ in range(10000):
                        line = handle.readline()
                        if not line.endswith(b"\n"):
                            break  # Concurrent writer may be midway through this record.
                        offset = handle.tell()
                        try:
                            record = json.loads(line)
                            if isinstance(record, dict):
                                records.append(record)
                        except (ValueError, UnicodeError):
                            LOG.warning("Ignoring malformed complete legacy record in %s", path.name)
                added = self.store.import_external(records)
                self.store.set_meta(key, {"identity": identity, "offset": offset})
                if added:
                    LOG.info("Imported %s existing leads from %s for deduplication", added, path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                LOG.warning("Cannot read legacy backup %s: %s", path, type(exc).__name__)


class StopRequested(Exception):
    pass


class RateLimited(Exception):
    pass


class Control:
    def __init__(self, interval):
        self.stop = asyncio.Event()
        self.ready = asyncio.Event()
        self.ready.set()
        self.captcha_lock = asyncio.Lock()
        self.captcha_pages = set()
        self.rate_lock = asyncio.Lock()
        self.interval = interval
        self.next_navigation = 0
        self.cooldown_until = 0
        self.batch_until = 0
        self.penalties = 0

    async def sleep(self, seconds):
        if self.stop.is_set():
            raise StopRequested()
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.01, seconds))
        except asyncio.TimeoutError:
            return
        raise StopRequested()

    async def wait_ready(self):
        while not self.ready.is_set() or time.monotonic() < self.batch_until:
            await self.sleep(0.25)
        if self.stop.is_set():
            raise StopRequested()

    async def navigation_slot(self):
        while True:
            await self.wait_ready()
            async with self.rate_lock:
                wait = max(self.next_navigation, self.cooldown_until) - time.monotonic()
                if wait <= 0 and self.ready.is_set():
                    self.next_navigation = time.monotonic() + self.interval * (1 + min(self.penalties, 4) * 0.5)
                    return
            await self.sleep(min(max(wait, 0.1), 1))

    def penalize(self, retry_after=None):
        self.penalties += 1
        delay = min(180, 15 * 2 ** min(self.penalties - 1, 4))
        if retry_after is not None:
            delay = max(delay, retry_after)
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
        LOG.warning("Slowing all browser workers; shared cooldown %.0fs", delay)


CAPTCHA_SELECTOR = "#captcha_container, div[class*='captcha'], div[id*='captcha'], iframe[src*='captcha'], div[class*='verify-bar-close']"


async def captcha_visible(page):
    if page.is_closed():
        return False
    for element in await page.query_selector_all(CAPTCHA_SELECTOR):
        try:
            if await element.is_visible():
                return True
        except Exception:
            continue
    return False


async def handle_captcha(page, control):
    if not await captcha_visible(page):
        return False
    control.captcha_pages.add(page)
    control.ready.clear()
    async with control.captcha_lock:
        try:
            if await captcha_visible(page):
                LOG.warning("CAPTCHA: all new work paused. Solve it manually in the browser tab: %s", page.url)
                await page.bring_to_front()
                while await captcha_visible(page):
                    await control.sleep(2)
                await control.sleep(4)
                control.penalize()
        finally:
            control.captcha_pages.discard(page)
            if not control.captcha_pages:
                control.ready.set()
    return True


async def navigate(page, url, control):
    await control.navigation_slot()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except Exception:
        # Verification can stall navigation before DOMContentLoaded is reported.
        # Solve it and retry this same task instead of losing the query/profile.
        if not await handle_captcha(page, control):
            raise
        await control.navigation_slot()
        response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    if response and response.status in (429, 503):
        header = response.headers.get("retry-after", "")
        control.penalize(float(header) if header.isdigit() else None)
        raise RateLimited(f"TikTok returned HTTP {response.status}")
    if response and response.status >= 400:
        raise RuntimeError(f"TikTok returned HTTP {response.status}")
    solved = await handle_captcha(page, control)
    if solved:
        await control.navigation_slot()
        response = await page.reload(wait_until="domcontentloaded", timeout=25000)
        await handle_captcha(page, control)
        if response and response.status >= 400:
            raise RuntimeError(f"TikTok returned HTTP {response.status} after verification")


async def extract_profile(page, username):
    await page.wait_for_function("""() => document.querySelector('[data-e2e="followers-count"]') ||
        document.querySelector('#__UNIVERSAL_DATA_FOR_REHYDRATION__') || document.querySelector('#SIGI_STATE')""",
        timeout=6500)
    snapshot = await page.evaluate("""() => ({
        states: ['__UNIVERSAL_DATA_FOR_REHYDRATION__','SIGI_STATE'].map(id => document.getElementById(id)?.textContent),
        followers: document.querySelector('[data-e2e="followers-count"]')?.getAttribute('title') ||
                   document.querySelector('[data-e2e="followers-count"]')?.textContent,
        bio: document.querySelector('[data-e2e="user-bio"]')?.textContent || '',
        link: document.querySelector('a[data-e2e="user-link"]')?.href || ''
    })""")
    result = {"followers_count": parse_follower_count(snapshot["followers"]),
              "bio": snapshot["bio"], "bio_link": snapshot["link"]}
    for raw in snapshot["states"]:
        try:
            state = json.loads(raw) if raw else {}
            detail = state.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo", {})
            pairs = [(detail.get("user", {}), detail.get("stats", {}))]
            module = state.get("UserModule", {})
            pairs += [(user, module.get("stats", {}).get(key, {})) for key, user in module.get("users", {}).items()]
            for user, stats in pairs:
                if not user or str(user.get("uniqueId", "")).lower() != username:
                    continue
                count = parse_follower_count(stats.get("followerCount"))
                if count is not None:
                    result["followers_count"] = count
                result["bio"] = user.get("signature") or result["bio"]
                link = user.get("bioLink") or {}
                result["bio_link"] = (link.get("link") if isinstance(link, dict) else link) or result["bio_link"]
        except (ValueError, TypeError, AttributeError):
            continue
    if result["followers_count"] is None:
        # A hydration script may appear before the visible follower counter.
        counter = await page.wait_for_selector('[data-e2e="followers-count"]', timeout=3500)
        result["followers_count"] = parse_follower_count((await counter.get_attribute("title")) or await counter.inner_text())
    if not result["bio"] or not result["bio_link"]:
        late = await page.evaluate("""() => ({
            bio: document.querySelector('[data-e2e="user-bio"]')?.textContent || '',
            link: document.querySelector('a[data-e2e="user-link"]')?.href || ''
        })""")
        result["bio"] = result["bio"] or late["bio"]
        result["bio_link"] = result["bio_link"] or late["link"]
    return result


class ContactHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.text = []
        self.links = []
        self.anchor = None
        self.structured_emails = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "a" and attrs.get("href"):
            self.anchor = [attrs["href"], attrs.get("aria-label", "")]
        if tag == "meta" and attrs.get("name", "").lower() in {"email", "contact:email"}:
            self.structured_emails.extend(extract_emails(attrs.get("content", "")))

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "a" and self.anchor:
            self.links.append(tuple(self.anchor))
            self.anchor = None

    def handle_data(self, data):
        if self.hidden:
            # Only explicit email fields, rather than arbitrary analytics/script addresses.
            for value in re.findall(r'"(?:email|contactEmail|businessEmail)"\s*:\s*"([^"\n]+)"', data, flags=re.I):
                self.structured_emails.extend(extract_emails(value))
            return
        self.text.append(data)
        if self.anchor:
            self.anchor[1] += " " + data


_THREAD_HTTP = threading.local()


def http_session():
    if not hasattr(_THREAD_HTTP, "session"):
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
        _THREAD_HTTP.session = session
    return _THREAD_HTTP.session


def unwrap_bio_url(url):
    parsed = urlparse(url)
    if domain_matches(parsed.hostname or "", {"tiktok.com"}) and parsed.path.startswith("/link/"):
        params = parse_qs(parsed.query)
        return next((params[key][0] for key in ("target", "url", "redirect_url") if params.get(key)), url)
    return url


def validate_public_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("invalid public website URL")
    if parsed.port not in (None, 80, 443) or domain_matches(host, SOCIAL_HOSTS):
        raise ValueError("unsupported website")
    addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("website is not on a public network")
    return url


def fetch_public_page(url):
    """Bounded HTML reads, explicit response closure, and checked redirect destinations."""
    try:
        url = unwrap_bio_url(url)
        for _ in range(5):
            validate_public_url(url)
            with http_session().get(url, timeout=(4, 8), stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    target = response.headers.get("Location")
                    if not target:
                        raise ValueError("empty redirect")
                    url = urljoin(url, target)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if "html" not in content_type and "text" not in content_type:
                    return {"emails": [], "links": [], "url": url}
                data = bytearray()
                started = time.monotonic()
                for chunk in response.iter_content(16384):
                    data.extend(chunk[:400_000 - len(data)])
                    if len(data) >= 400_000 or time.monotonic() - started > 15:
                        break
                parser = ContactHTML()
                parser.feed(data.decode(response.encoding or "utf-8", errors="replace"))
                parser.close()
                emails = []
                for href, _ in parser.links:
                    if href.lower().startswith("mailto:"):
                        emails.extend(extract_emails(unquote(href[7:].split("?", 1)[0])))
                emails += parser.structured_emails + extract_emails("\n".join(parser.text))
                return {"emails": list(dict.fromkeys(emails)), "links": parser.links, "url": url}
        raise ValueError("too many redirects")
    except ValueError as exc:
        return {"emails": [], "links": [], "url": url, "unsupported": str(exc)}
    except (requests.RequestException, OSError) as exc:
        # Persist error classes, not arbitrary response bodies or contact details.
        return {"emails": [], "links": [], "url": url, "error": type(exc).__name__}


class ContactCrawler:
    def __init__(self, store, control, max_pages):
        self.store, self.control, self.max_pages = store, control, max_pages
        self.locks = {}

    async def fetch(self, url):
        await self.control.wait_ready()
        cached = self.store.cache_get(url)
        if cached is not None:
            return cached
        lock = self.locks.setdefault(url, asyncio.Lock())
        try:
            async with lock:
                cached = self.store.cache_get(url)
                if cached is None:
                    cached = await asyncio.to_thread(fetch_public_page, url)
                    self.store.cache_put(url, cached)
                return cached
        finally:
            if self.locks.get(url) is lock:
                self.locks.pop(url, None)

    async def collect(self, url):
        if url.lower().startswith("mailto:"):
            return [(email, "bio_link") for email in extract_emails(unquote(url[7:].split("?", 1)[0]))], False
        root = unwrap_bio_url(url)
        parsed_root = urlparse(root)
        if parsed_root.scheme in {"http", "https"}:
            root = parsed_root._replace(fragment="").geturl()
        pending, visited, found, errors = [root], set(), [], False
        while pending and len(visited) < self.max_pages:
            await self.control.wait_ready()
            target = pending.pop(0)
            if target in visited:
                continue
            visited.add(target)
            result = await self.fetch(target)
            errors |= bool(result.get("error"))
            found.extend((email, "bio_link" if len(visited) == 1 else "contact_page") for email in result["emails"])
            # A creator's own contact/about page or an explicitly linked website from a bio service.
            root_host = urlparse(result["url"]).hostname or ""
            ranked = []
            for href, label in result["links"]:
                if href.lower().startswith("mailto:"):
                    continue
                link = urljoin(result["url"], href).split("#", 1)[0]
                parsed = urlparse(link)
                if parsed.scheme not in {"http", "https"} or domain_matches(parsed.hostname or "", SOCIAL_HOSTS):
                    continue
                relevant = re.search(r"contact|work with me|business|collab|about|media.?kit", label + " " + parsed.path, re.I)
                own = parsed.hostname == root_host
                outbound = domain_matches(root_host, BIO_HOSTS) and re.search(r"website|contact|business|portfolio", label, re.I)
                if (own and relevant) or outbound:
                    ranked.append((0 if re.search("contact|business", label + parsed.path, re.I) else 1, link))
            pending.extend(link for _, link in sorted(ranked) if link not in visited and link not in pending)
        return list(dict.fromkeys(found)), errors


def build_search_terms():
    terms = []
    for index, niche in enumerate(NICHES):
        terms.extend((niche, f"{LOCATIONS[index % len(LOCATIONS)]} {niche}"))
        if index % 2 == 0:
            terms.append(f"{niche} {SEARCH_MODIFIERS[index % len(SEARCH_MODIFIERS)]}")
    terms.extend(HASHTAG_SEARCHES)
    terms.extend(f"{LOCATIONS[index % len(LOCATIONS)]} {tag}" for index, tag in enumerate(HASHTAG_SEARCHES) if index % 2 == 0)
    return list(dict.fromkeys(terms))


async def discovery_worker(page, store, control, args, once=False):
    while not control.stop.is_set():
        if store.backlog() >= args.max_backlog:
            await control.sleep(2)
            if once:
                return
            continue
        term = store.choose_query()
        if not term:
            await control.sleep(10)
            if once:
                return
            continue
        started, discovered, error = time.monotonic(), 0, False
        try:
            LOG.info("Searching: %s", term)
            await navigate(page, f"https://www.tiktok.com/search/video?q={quote_plus(term)}", control)
            await page.wait_for_selector("a[href*='/@']", timeout=10000)
            observed, stagnant = set(), 0
            for scroll_index in range(args.max_scrolls):
                await control.wait_ready()
                await handle_captcha(page, control)
                links = await page.eval_on_selector_all("a[href*='/@']", "els => els.map(el => el.href)")
                added, newly_observed = 0, 0
                for link in links:
                    normalized = canonical_profile_url(link)
                    if normalized and normalized not in observed:
                        observed.add(normalized)
                        newly_observed += 1
                        added += store.add_candidate(normalized, term)
                discovered += added
                # Already-known top results should not stop us reaching the less
                # prominent creators further down the same video search.
                stagnant = stagnant + 1 if newly_observed == 0 else 0
                if (discovered >= args.profiles_per_search or stagnant >= args.stagnant_scrolls
                        or store.backlog() >= args.max_backlog or scroll_index == args.max_scrolls - 1):
                    break
                # Honor the same global navigation/request pacing when asking for more results.
                await control.navigation_slot()
                await page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 1400))")
                await control.sleep(args.scroll_wait)
            LOG.info("Search collected %s new accounts (%s unique accounts observed)", discovered, len(observed))
        except StopRequested:
            raise
        except Exception as exc:
            error = True
            await handle_captcha(page, control)
            if not isinstance(exc, RateLimited):
                control.penalize()
            LOG.warning("Search interrupted: %s; captured profiles remain queued", type(exc).__name__)
        finally:
            store.search_finished(term, discovered, time.monotonic() - started, error)
        if once:
            return


class Pipeline:
    def __init__(self, store, control, args, importer):
        self.store, self.control, self.args, self.importer = store, control, args, importer
        self.crawler = ContactCrawler(store, control, args.contact_pages)
        self.session_leads = 0

    def accept(self, job, data, candidates):
        self.importer.poll(force=True)
        for email, source in candidates:
            if excluded_region(data["bio"], email) or exclusion_reason(
                job["username"], data["bio"], email, self.args.include_ugc
            ):
                continue
            if self.store.duplicate(job["username"], email):
                self.store.finish_job(job["username"], "duplicate", "username or email already recorded")
                return True
            record = {
                "username": job["username"], "profile_url": job["url"],
                "followers_count": data["followers_count"], "bio": data["bio"],
                "email": email, "email_source": source, "source_query": job["source_query"],
                "bio_link": data.get("bio_link", ""), "found_at": utc_now(),
            }
            if self.store.save_lead(record, self.args.dry_run):
                self.session_leads += 1
                lead_notice("held" if self.args.dry_run else "qualified",
                            f"QUALIFIED LEAD #{self.session_leads} THIS RUN", [
                                f"Creator:    @{job['username']}",
                                f"Email:      {email}",
                                f"Followers:  {data['followers_count']:,}",
                                f"Profile:    {job['url']}",
                                f"Found in:   {source}",
                                "Delivery:   DRY RUN - HELD, NOT SENT" if self.args.dry_run else
                                "Delivery:   SAVED + QUEUED FOR N8N (awaiting acceptance)",
                            ])
                if self.args.batch_rest and self.session_leads % self.args.batch_size == 0:
                    self.control.batch_until = max(self.control.batch_until, time.monotonic() + self.args.batch_rest)
                    LOG.info("Optional batch rest %.0fs; all remaining profiles stay queued", self.args.batch_rest)
            return True
        return False

    async def profile_worker(self, page, number, once=False):
        processed = False
        while not self.control.stop.is_set() and not (once and processed):
            await self.control.wait_ready()
            job = self.store.claim_job()
            if not job:
                if once:
                    return False
                await self.control.sleep(1)
                continue
            processed = True
            started = time.monotonic()
            count = None
            try:
                await navigate(page, job["url"], self.control)
                if canonical_profile_url(page.url) != job["url"]:
                    raise ValueError("navigation did not reach the requested creator")
                data = await extract_profile(page, job["username"])
                count = data["followers_count"]
                if count is None:
                    raise ValueError("unreadable follower count")
                if not self.args.min_followers <= count <= self.args.max_followers:
                    self.store.finish_job(job["username"], "out_of_range", f"followers={count}", 7 * 86400, data)
                    continue
                reason = excluded_region(data["bio"]) or exclusion_reason(
                    job["username"], data["bio"], include_ugc=self.args.include_ugc
                )
                if reason:
                    self.store.finish_job(job["username"], "excluded", reason, 30 * 86400, data)
                    continue
                emails = [(email, "bio") for email in extract_emails(data["bio"])]
                if self.accept(job, data, emails):
                    continue
                if data.get("bio_link"):
                    self.store.finish_job(job["username"], "enrichment_pending", delay=0, data=data)
                else:
                    self.store.finish_job(job["username"], "no_email", "no qualifying email or public bio link", self.args.recheck_days * 86400, data)
            except StopRequested:
                self.store.finish_job(job["username"], "pending", "stopped before completion", 0)
                raise
            except Exception as exc:
                if page.is_closed():
                    self.store.finish_job(job["username"], "pending", "browser page closed", 0)
                    raise RuntimeError("Profile browser page closed") from exc
                await handle_captcha(page, self.control)
                if not isinstance(exc, RateLimited):
                    self.control.penalize()
                self.store.finish_job(job["username"], "temporary_error", type(exc).__name__, min(3600, 60 * 2 ** min(job["attempts"], 5)))
                LOG.warning("Profile worker %s: @%s will retry (%s)", number, job["username"], type(exc).__name__)
            finally:
                self.store.profile_finished(job["source_query"], time.monotonic() - started,
                                            count, self.args.min_followers, self.args.max_followers)
        return processed

    async def email_worker(self, number):
        while not self.control.stop.is_set():
            await self.control.wait_ready()
            job = self.store.claim_job(enrichment=True)
            if not job:
                await self.control.sleep(1)
                continue
            started = time.monotonic()
            try:
                data = json.loads(job["data"])
                emails, errors = await self.crawler.collect(data["bio_link"])
                if not self.accept(job, data, emails):
                    if errors:
                        self.store.finish_job(job["username"], "enrichment_error", "website unavailable; retry enrichment", 600)
                    else:
                        self.store.finish_job(job["username"], "no_email", "no qualifying public email", self.args.recheck_days * 86400)
            except StopRequested:
                self.store.finish_job(job["username"], "enrichment_pending", "stopped before enrichment completed", 0)
                raise
            except Exception as exc:
                self.store.finish_job(job["username"], "enrichment_error", type(exc).__name__, 600)
                LOG.warning("Email worker %s: @%s will retry (%s)", number, job["username"], type(exc).__name__)
            finally:
                with self.store.conn:
                    self.store.conn.execute("UPDATE queries SET seconds=seconds+? WHERE term=?",
                                            (time.monotonic() - started, job["source_query"]))


def post_webhook(url, row):
    payload = json.loads(row["payload"])
    # The receiver can enforce this key to make retries safe after ambiguous timeouts.
    key = hashlib.sha256((row["username"] + ":" + row["email_key"]).encode()).hexdigest()
    try:
        with http_session().post(url, json={"leads": [payload]}, headers={"Idempotency-Key": key}, timeout=(4, 20), allow_redirects=False) as response:
            if 200 <= response.status_code < 300:
                return True, f"HTTP {response.status_code}", 0
            retry_after = response.headers.get("Retry-After", "")
            return False, f"HTTP {response.status_code}", float(retry_after) if retry_after.isdigit() else 0
    except requests.RequestException as exc:
        return False, type(exc).__name__, 0


async def delivery_worker(store, control, args, importer):
    while not control.stop.is_set():
        await control.wait_ready()
        if args.dry_run:
            await control.sleep(2)
            continue
        importer.poll(force=True)
        row = store.claim_delivery()
        if not row:
            await control.sleep(1)
            continue
        record = json.loads(row["payload"])
        LOG.info("[N8N SENDING] @%s | %s | attempt %s", row["username"], record["email"], row["attempts"] + 1)
        success, detail, server_delay = await asyncio.to_thread(post_webhook, args.webhook, row)
        delay = max(server_delay, min(3600, 5 * 2 ** min(row["attempts"], 9))) + random.uniform(0, 2)
        store.delivery_result(row["id"], success, "" if success else detail, delay)
        if success:
            lead_notice("sent", f"N8N ACCEPTED - {detail}", [
                f"Creator:    @{row['username']}",
                f"Email:      {record['email']}",
                f"Lead ID:    {row['id']} | attempt {row['attempts'] + 1}",
                "Delivery:   SUCCESS - webhook accepted this lead",
                "Workflow:   downstream steps are tracked inside n8n",
                progress_text(store),
            ])
        else:
            lead_notice("retry", "N8N DELIVERY FAILED - SAVED FOR RETRY", [
                f"Creator:    @{row['username']}",
                f"Email:      {record['email']}",
                f"Lead ID:    {row['id']} | attempt {row['attempts'] + 1}",
                f"Error:      {detail}",
                f"Next retry: in {delay:.0f} seconds; lead remains saved",
            ], logging.WARNING)


async def monitor(store, control, importer, state_dir):
    while not control.stop.is_set():
        importer.poll()
        store.export(state_dir / "leads_backup.jsonl")
        LOG.info("[TOTALS] %s", progress_text(store))
        # Decay pacing penalties after sustained healthy operation, never below the configured interval.
        if control.cooldown_until < time.monotonic() - 60:
            control.penalties = max(0, control.penalties - 1)
        await control.sleep(30)


@contextmanager
def state_lock(state_dir):
    with (state_dir / "run.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another v2 process is already using this state directory") from exc
        yield


async def single_tab_worker(page, pipeline, store, control, args):
    """Drain saved profiles, then discover more, using one page sequentially."""
    while not control.stop.is_set():
        await control.wait_ready()
        if not await pipeline.profile_worker(page, 1, once=True):
            await discovery_worker(page, store, control, args, once=True)


async def prepare_browser_pages(context, args):
    restored = list(context.pages)
    main_page = restored[0] if restored else await context.new_page()
    # Chromium can restore the tabs from an earlier multi-tab session.
    for page in restored[1:]:
        await page.close()
    profile_pages = [await context.new_page() for _ in range(args.profile_workers)] if args.multi_tab else []
    return main_page, profile_pages


async def run(args):
    state_dir = args.state_dir.resolve()
    if args.stats:
        path = state_dir / "leads.sqlite3"
        if not path.exists():
            print(json.dumps({"profiles": {}, "deliveries": {}, "backlog": 0}, indent=2))
            return
        readonly = Store.__new__(Store)
        readonly.conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            print(json.dumps(readonly.summary(), indent=2))
        finally:
            readonly.close()
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    with state_lock(state_dir):
        store = Store(state_dir / "leads.sqlite3")
        try:
            control = Control(args.navigation_interval)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, control.stop.set)
            legacy_paths = [] if args.no_legacy_import else [
                BASE_DIR / "leads_backup.jsonl", Path.home() / "tiktok-scraper" / "leads_backup.jsonl"
            ]
            importer = LegacyImporter(store, legacy_paths + args.legacy_backup)
            importer.poll(force=True)
            terms = build_search_terms()
            store.configure_discovery(terms, args.min_followers, args.max_followers)
            LOG.info("Nano discovery: %s targeted video searches | follower range %s–%s",
                     len(terms), args.min_followers, args.max_followers)
            if args.release_held and not args.dry_run:
                with store.conn:
                    store.conn.execute("UPDATE leads SET delivery_status='pending' WHERE delivery_status='held'")
            LOG.info("State: %s | browser: %s | email workers: %s | dry run: %s",
                     state_dir, f"{args.profile_workers + 1} tabs" if args.multi_tab else "one shared tab",
                     args.email_workers, args.dry_run)
            parsed_webhook = urlparse(args.webhook)
            visible_webhook = parsed_webhook._replace(
                netloc=parsed_webhook.netloc.rsplit("@", 1)[-1], query="", fragment=""
            ).geturl()
            lead_notice("held" if args.dry_run else "mode",
                        "DRY RUN - N8N DELIVERY DISABLED" if args.dry_run else "LIVE MODE - N8N DELIVERY ENABLED", [
                            f"Webhook: {visible_webhook}",
                            "Qualified leads will be held locally." if args.dry_run else
                            "Qualified leads are saved, then sent automatically to this webhook.",
                            "Only HTTP 2xx counts as accepted; failures stay queued for retry.",
                            progress_text(store),
                        ])
            if not args.dry_run and store.summary()["deliveries"].get("held", 0):
                LOG.warning("Previously held dry-run leads remain unsent; use --release-held to queue them")
            LOG.info("Fresh separate browser profile: log into TikTok there if prompted")
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(state_dir / "browser_profile"), headless=False, args=["--disable-dev-shm-usage"]
                )
                tasks = []
                try:
                    if not args.no_resource_blocking:
                        async def route_resources(route):
                            request = route.request
                            # Keep images available for human verification; scripts/styles remain enabled.
                            if request.resource_type in {"media", "font"}:
                                await route.abort()
                            else:
                                await route.continue_()
                        await context.route("**/*", route_resources)
                    search_page, pages = await prepare_browser_pages(context, args)
                    pipeline = Pipeline(store, control, args, importer)
                    if args.multi_tab:
                        tasks.append(asyncio.create_task(discovery_worker(search_page, store, control, args), name="discovery"))
                        tasks.extend(asyncio.create_task(pipeline.profile_worker(page, index + 1), name=f"profile-{index + 1}")
                                     for index, page in enumerate(pages))
                    else:
                        tasks.append(asyncio.create_task(
                            single_tab_worker(search_page, pipeline, store, control, args), name="browser"))
                    tasks.extend(asyncio.create_task(pipeline.email_worker(index + 1), name=f"email-{index + 1}")
                                 for index in range(args.email_workers))
                    tasks.append(asyncio.create_task(delivery_worker(store, control, args, importer), name="delivery"))
                    tasks.append(asyncio.create_task(monitor(store, control, importer, state_dir), name="monitor"))
                    stopping = asyncio.create_task(control.stop.wait(), name="stop")
                    done, _ = await asyncio.wait([*tasks, stopping], return_when=asyncio.FIRST_COMPLETED)
                    failure = None
                    for task in done:
                        if task is not stopping:
                            error = task.exception()
                            if error and not isinstance(error, StopRequested):
                                LOG.error("Worker %s failed: %s", task.get_name(), error)
                                failure = error
                    control.stop.set()
                    LOG.info("Stopping: finishing in-flight requests; queued work stays saved")
                    await asyncio.gather(*tasks, return_exceptions=True)
                    stopping.cancel()
                    await asyncio.gather(stopping, return_exceptions=True)
                    if failure:
                        raise RuntimeError("A scraper worker failed; saved work will resume next run") from failure
                finally:
                    control.stop.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await context.close()
        finally:
            try:
                store.export(state_dir / "leads_backup.jsonl")
                LOG.info("[FINAL TOTALS] %s", progress_text(store))
            finally:
                store.close()


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--state-dir", type=Path, default=Path(os.getenv("SCRAPER_STATE_DIR", str(BASE_DIR / "scraper_v2_state"))).expanduser())
    parser.add_argument("--webhook", default=DEFAULT_WEBHOOK)
    parser.add_argument("--multi-tab", action="store_true", help="Use separate search/profile tabs; may require solving CAPTCHAs in each tab")
    parser.add_argument("--profile-workers", type=int, default=2, help="Profile tabs when --multi-tab is enabled; default mode shares one tab")
    parser.add_argument("--email-workers", type=int, default=3)
    parser.add_argument("--navigation-interval", type=float, default=2.0, help="Minimum shared interval between browser navigations/scrolls")
    parser.add_argument("--min-followers", type=int, default=10)
    parser.add_argument("--max-followers", type=int, default=5000)
    parser.add_argument("--profiles-per-search", type=int, default=120)
    parser.add_argument("--max-scrolls", type=int, default=28)
    parser.add_argument("--stagnant-scrolls", type=int, default=5,
                        help="Stop after this many screens with no newly observed creators, including known accounts")
    parser.add_argument("--scroll-wait", type=float, default=1.0)
    parser.add_argument("--max-backlog", type=int, default=300)
    parser.add_argument("--contact-pages", type=int, default=3, help="Maximum pages per public bio website")
    parser.add_argument("--recheck-days", type=float, default=7)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--batch-rest", type=float, default=0, help="Optional shared rest in seconds after each batch; candidates remain queued")
    parser.add_argument("--include-ugc", action="store_true", help="Include UGC creators, excluded by default to match original preference")
    parser.add_argument("--dry-run", action="store_true", help="Collect leads into held state; never POST to n8n")
    parser.add_argument("--release-held", action="store_true", help="Queue leads previously held by --dry-run for delivery")
    parser.add_argument("--stats", action="store_true", help="Show saved counts and exit without opening a browser")
    parser.add_argument("--no-legacy-import", action="store_true", help="Skip automatic read-only import of original lead backups")
    parser.add_argument("--legacy-backup", action="append", type=Path, default=[], help="Additional existing lead JSONL file to read for deduplication")
    parser.add_argument("--no-resource-blocking", action="store_true", help="Allow all resources to compare browser caching/performance")
    args = parser.parse_args(argv)
    for name in ("profile_workers", "email_workers", "profiles_per_search", "max_scrolls", "stagnant_scrolls", "max_backlog", "contact_pages", "batch_size"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if not 0 <= args.min_followers <= args.max_followers:
        parser.error("Follower range must be nonnegative and ordered")
    for name in ("navigation_interval", "scroll_wait", "recheck_days"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")
    if not math.isfinite(args.batch_rest) or args.batch_rest < 0:
        parser.error("--batch-rest must be finite and nonnegative")
    if args.dry_run and args.release_held:
        parser.error("--release-held cannot be used with --dry-run")
    if not args.stats and args.state_dir.resolve() in {BASE_DIR, (Path.home() / "tiktok-scraper").resolve()}:
        parser.error("Choose a separate --state-dir; this path belongs to the original scraper")
    parsed = urlparse(args.webhook)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        parser.error("--webhook must be an HTTP(S) URL")
    return args


def main():
    handler = logging.StreamHandler()
    handler.setFormatter(HighlightFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        asyncio.run(run(arguments()))
    except KeyboardInterrupt:
        pass
    except Exception:
        LOG.exception("Scraper stopped; its saved state will resume on next start")
        raise SystemExit(1)



if __name__ == "__main__":
    main()

# Scraper v2 usage

The improved scraper is `live_tiktok_scraper_v2.py`. It is standalone and
does not import or modify `live_tiktok_scraper.py`.

Install the dependencies from `requirements.txt`, run `playwright install chromium`,
and copy `.env.example` to `.env`. Set `N8N_WEBHOOK_URL` to your n8n production
webhook URL. `--webhook` overrides that setting; `SCRAPER_STATE_DIR` or `--state-dir`
can change the local state directory. The default URL is local and contains no
production identifier. This scraper uses Linux/macOS file locking.

Start it from a terminal:

```sh
cd creator-outreach-matrix
python3 live_tiktok_scraper_v2.py
```

It opens a separate Chromium window using v2's saved login profile. Log into TikTok
there if prompted. By default, one TikTok tab alternates between searching and
checking queued profiles, so three tabs do not each present a separate CAPTCHA.
Extra tabs restored from an earlier v2 session are closed at startup. CAPTCHA
challenges pause all new work for manual solving. TikTok can still challenge the
single tab again later; solving once does not guarantee a CAPTCHA-free session.
Press Ctrl+C once to stop cleanly; queued profiles and undelivered leads resume
when you run the same command again.

The terminal highlights each qualified lead in a bordered cyan block showing
the creator, email, followers, profile URL, and delivery state. A separate green
`N8N ACCEPTED - HTTP 200` block (or the actual successful status code) confirms
webhook acceptance. Yellow failure blocks show the error and retry delay.
Dry-run leads explicitly say `HELD, NOT SENT`. Colors appear in interactive
terminals; the same labels and borders remain visible in redirected output.

Every 30 seconds, readable totals separate new qualified leads, n8n acceptances,
queued/sending/retrying deliveries, held leads, waiting profiles, and legacy
imports. At startup, a banner shows whether live delivery is enabled and the
configured webhook. Webhook acceptance does not establish that every downstream
n8n workflow step completed; check the workflow's execution history for that.

After updating this file while v2 is running, restart only the v2 process to load
the new output. Its saved work resumes. The original scraper can stay running.

All new writable state is in `scraper_v2_state/`:

- `leads.sqlite3`: durable profile queue, retry dates, query statistics, email cache,
  deduplicated leads, and delivery outbox. This is the primary record.
- `leads_backup.jsonl`: exported new leads, refreshed every 30 seconds and at exit.
- `browser_profile/`: this version's own TikTok session.
- `run.lock`: prevents two v2 processes from using the same state directory.

The default range remains 10–5,000 followers, with the original shipping-region
exclusions. A person's name “Rio” alone no longer establishes a Brazilian location;
“Rio de Janeiro” still does. Creator handles containing makeup, GRWM, lifestyle,
diary, and similar words are allowed. Shops, agencies, explicit repost accounts,
and UGC creators still have contextual exclusion checks. `--include-ugc` includes
UGC creators when wanted. Country detection remains based on public profile
evidence; the absence of an excluded country does not verify a shipping address.

There is one shared TikTok tab and three public-website email workers by default.
Saved profiles are checked first; when none are due, the same tab searches for
more. Website enrichment and n8n delivery retain their own background workers.
Sequential browser work may reduce throughput compared with multiple tabs.
To explicitly restore the previous search tab plus two profile tabs, use
`--multi-tab`; `--profile-workers` controls profile tabs only in that mode.
Navigations and search scrolling share a minimum two-second interval; throttling,
CAPTCHAs, and failures slow the browser workers further. Email extraction and n8n
delivery use independent workers. Qualified leads no longer impose a 15-second
pause or a mandatory 10-minute rest. Optional batch rests keep every candidate:

```sh
python3 live_tiktok_scraper_v2.py --batch-size 50 --batch-rest 600
```

Addresses such as `pr-sarah@gmail.com` and `collab_emily@gmail.com` are preserved.
The public bio website and up to two linked contact/about pages are checked by
default. Reads are bounded, responses are closed, website results are cached,
and DNS/private-network checks restrict enrichment to public websites. Missing
emails are rechecked after seven days; follower-range misses after seven days;
exclusions after 30 days. Temporary profile errors retry with backoff, and website
failures retry enrichment without requiring another TikTok visit. The scraper
does not verify mailbox deliverability.

Discovery now focuses on nano creators. Every video search includes a small-account
signal such as `small creator`, `nano`, `under 1k`, `under 5k`, or `road to 1k`,
alongside beauty, jewelry, fashion, or lifestyle terms. Generic searches such as
`#grwm`, `#jewelrytok`, and `everyday makeup routine` are retired on startup, including
those saved in an existing database. Their history and queued profiles are retained.
Search wording is a discovery hint; each creator's actual follower count must still
pass the configured range before email enrichment or delivery.

Query selection primarily rewards the fraction of readable profiles in the follower
range, with a smaller bonus for qualified email leads and 25% exploration when both
tried and untried searches are available. A search full of big profiles no longer
gets a bonus for finding many new accounts. Range statistics are seeded from saved
profile data on upgrade, persist across restarts, and are recalculated from saved
profiles if the follower limits change.

Searches collect up to 120 new accounts over at most 28 result screens by default.
They stop after five screens with no newly observed creators. Known accounts that
appear for the first time in that search count as progress, so old top results do
not prevent reaching less prominent creators further down. Saved candidates do
not count toward the 120-new-account target. Found accounts are saved as soon as
each results screen is read; profile, tracked, and video URLs are normalized to
one creator identity. `--stagnant-scrolls` controls the early-stop threshold.

Existing JSONL lead backups in the repository folder and `~/tiktok-scraper/` are read
without modification and monitored for additions. Their usernames and email
addresses prevent repeat delivery by v2. Old `seen_creators.txt` entries are not
imported because they do not say whether a creator was wrongly rejected. Legacy
backup entries remain owned by the old scraper; v2 does not resend them because
their delivery status is unknown. Two independently running versions can still
discover and send the same new lead at nearly the same time. Their rate limiters
also operate independently. For strict deduplication across senders, n8n must
enforce uniqueness by normalized email/username.

n8n receives the existing `{"leads": [...]}` payload at
`http://localhost:5678/webhook/tiktok-leads`. Only HTTP 2xx marks a delivery accepted.
Failures remain in the database and retry with backoff. A stable `Idempotency-Key`
header lets n8n deduplicate retries if its workflow supports that header. Delivery
is at least once: if n8n accepts a request and its response is lost, a retry may
repeat it unless the receiver deduplicates. Reconnect credentials and configure the sanitized n8n workflows as described in README.md.

Collect without sending anything to n8n:

```sh
python3 live_tiktok_scraper_v2.py --dry-run
```

Dry-run leads stay held even on an ordinary restart. To deliver those held leads
as well as collect new leads, explicitly release them:

```sh
python3 live_tiktok_scraper_v2.py --release-held
```

Inspect saved counts while it runs, or see all configuration options:

```sh
python3 live_tiktok_scraper_v2.py --stats
python3 live_tiktok_scraper_v2.py --help
```

Useful tuning options include `--profile-workers`, `--email-workers`,
`--navigation-interval`, `--contact-pages`, `--max-backlog`, and `--recheck-days`.
`--no-resource-blocking` allows comparing normal browser caching against blocking
videos and fonts. Playwright routing disables HTTP cache; measure both settings
before assuming resource blocking is faster. Images remain enabled so human
verification challenges can render. See the
[Playwright routing documentation](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route)
and [Requests streaming documentation](https://requests.readthedocs.io/en/stable/user/advanced/#body-content-workflow).

Run the offline regression suite:

```sh
python3 -m unittest discover -s tests -p '*_test.py' -v
```

The tests use temporary databases, synthetic profiles, and mocked HTTP responses.
They do not contact TikTok or your real webhook. Live Chromium/TikTok behavior and
actual leads per hour need validation in your normal desktop terminal.

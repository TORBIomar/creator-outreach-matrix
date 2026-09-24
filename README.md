# Creator Outreach Matrix

An automated end-to-end influencer outreach pipeline designed for beauty, jewelry, GRWM, and lifestyle nano- & micro-influencers (10 – 5,000 followers). The system combines an evasive Playwright scraper with n8n workflow automations for transactional email outreach, AI intent classification of creator replies, and real-time Telegram notifications.

---

## 🏗️ System Architecture

```text
┌─────────────────────────┐
│  TikTok Search Matrix   │  Playwright (Anti-detection, resource blocking,
│  & DOM / JSON Extractor │  DOM hydration parsing, Linktree fallback)
└───────────┬─────────────┘
            │ HTTP POST (Leads batch)
            ▼
┌─────────────────────────┐
│     n8n Webhook         │  Splits leads, validates emails, applies custom HTML
│   (workflows/Tiktok)    │  collaboration pitch, sends via Zoho Mail API
└───────────┬─────────────┘
            │
      ┌─────┴─────────────────────────┐
      ▼                               ▼
┌──────────────┐              ┌──────────────┐
│  Zoho Mail   │              │ Telegram Bot │
│   Outreach   │              │ Alert (Sent) │
└──────┬───────┘              └──────────────┘
       │
       │ (Creator replies to email)
       ▼
┌─────────────────────────┐
│  Zoho Poller / IMAP     │  Detects inbound replies from creators,
│  (workflows/Zoho,       │  filters spam & automated bounce-backs
│   workflows/Message)    │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  AI Intent Analysis     │  Evaluates response sentiment (interested / question /
│   (LM Studio / OpenAI)  │  declined) and drafts personalized response
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│  Telegram Notification  │  Real-time alert with reply preview & drafted response
└─────────────────────────┘
```

---

## 📁 Repository Structure

```text
├── live_tiktok_scraper.py       # Main production Playwright scraper
├── scrape_tiktok.py             # Test script with mock lead payload
├── workflows/                   # n8n Automation Workflows (Sanitized JSONs)
│   ├── Tiktok.json              # Webhook receiver -> Zoho Mail sender -> Telegram alert
│   ├── Zoho.json                # Inbound Zoho mail checker -> LLM Intent analysis -> Telegram
│   └── Message.json             # IMAP mail trigger -> Filter -> Telegram alert
├── templates/                   # Outreach and follow-up templates
│   ├── initial_outreach_pitch.html     # HTML pitch template sent to discovered creators
│   ├── collaboration_onboarding.txt   # Step-by-step catalog redemption instructions
│   └── shipping_and_commission_faq.txt # Transparent FAQ addressing shipping & commissions
├── requirements.txt             # Python dependencies
├── .env.example                 # Environment variables template
└── .gitignore                   # Excludes browser cache, session cookies, and scraped leads
```

---

## ⚡ Scraper Features

- **Granular Follower Targeting**: Filters creators between 10 and 5,000 followers via server-side hydration JSON (`__UNIVERSAL_DATA_FOR_REHYDRATION__` / `SIGI_STATE`) with DOM query fallbacks.
- **Strict Region Filtering**: Excludes non-shipping destinations, sanctioned areas, and high shipping-cost regions before scraping.
- **Commercial & Agency Filtering**: Detects and skips agencies, management firms, salon booking links, and UGC-only handles.
- **Obfuscated Email Parsing**: Resolves disguised email patterns (e.g., `name (at) gmail (dot) com`, `name [at] domain.com`).
- **Bio-Link Resolution**: Follows Linktree, Beacons, and custom website URLs in bio to uncover hidden contact emails.
- **Resource Route Blocking**: Aborts heavy media (images, videos, fonts) during navigation for 3x–5x faster execution and low RAM/CPU usage.
- **Anti-Bot & Human Emulation**: Suppresses automation indicators, removes `--no-sandbox` warning banners, randomizes search combinations, and applies natural cooldown periods.
- **Data Persistence**: Automatically stores seen handles in `seen_creators.txt` and backs up verified leads locally in `leads_backup.jsonl`.

---

## 🚀 Getting Started

### 1. Prerequisites

- Python 3.10+
- Google Chrome installed locally
- [n8n](https://n8n.io/) instance running (local or cloud)
- [Optional] LM Studio / Ollama or OpenAI-compatible server for inbound intent parsing

### 2. Installation

Clone this repository and set up a virtual environment:

```bash
git clone https://github.com/TORBIomar/creator-outreach-matrix.git
cd creator-outreach-matrix

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3. Configuration

Copy `.env.example` to `.env` and adjust the variables:

```bash
cp .env.example .env
```

| Variable | Description | Default |
| :--- | :--- | :--- |
| `N8N_WEBHOOK_URL` | Webhook URL where scraped leads are posted | `http://localhost:5678/webhook/tiktok-leads` |
| `SEEN_FILE` | Path to file tracking previously processed creators | `seen_creators.txt` |
| `BACKUP_LEADS_FILE` | Path to fallback jsonl file for leads | `leads_backup.jsonl` |
| `USER_DATA_DIR` | Path to persistent browser profile directory | `browser_profile` |

---

## 🔄 n8n Workflows Setup

Import the JSON files from the `workflows/` directory into your n8n workspace:

1. **`workflows/Tiktok.json`**:
   - Set up the **Zoho Mail OAuth2** credentials.
   - Configure your **Zoho Account ID** in the HTTP Request node URL.
   - Set your **Telegram Bot Token** and `chatId`.
   - Update brand variables (brand name, links, address) inside the `Code in JavaScript` node.

2. **`workflows/Zoho.json`**:
   - Connects to Zoho Mail API to poll incoming creator responses.
   - Forwards the email body to a local LLM endpoint (default: `http://localhost:1234/v1/chat/completions`) for classification.
   - Sends Telegram notifications with summary and intent.

3. **`workflows/Message.json`**:
   - Alternative IMAP trigger for instant inbox notifications on incoming creator replies.

---

## 🏃 Running the Scraper

Test the webhook connection with mock data:
```bash
python scrape_tiktok.py
```

Launch the live scraper:
```bash
python live_tiktok_scraper.py
```

To stop execution cleanly, press `Ctrl+C`. The scraper completes the active creator profile, preserves state in `seen_creators.txt`, and gracefully exits.

---

## 🛡️ Privacy & Sensitive Data

All personal identifiable information (PII), proprietary brand names, physical addresses, API keys, credential IDs, and Telegram chat IDs have been replaced with clear placeholders (`YOUR_TELEGRAM_CHAT_ID`, `YOUR_ZOHO_ACCOUNT_ID`, `outreach@yourbrand.com`, etc.) before publishing.

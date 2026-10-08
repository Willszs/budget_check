# 💰 BudgetCheck - Smart Telegram Financial & Workload Pacing Bot

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram-Bot%20API-0088cc.svg?logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?logo=docker&logoColor=white)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

**BudgetCheck** is an intelligent, multi-currency financial and milestone management bot designed for freelancers, remote workers, students, and independent creators on Telegram.

Unlike standard expense trackers, BudgetCheck functions as an **autonomous workload pacing and deadline protection engine**. By analyzing your **hourly rate**, **current liquidity**, **upcoming milestones (deadlines)**, and **custom leave / weekend schedules**, it dynamically calculates your exact required daily workload to ensure 100% milestone coverage while strictly enforcing healthy work-hour boundaries (5.0h ~ 8.0h / day).

---

## 🌟 Key Features

### 1. 🛡️ Dynamic Rolling Gap & Workload Pacing Engine
- **Decisive Bottleneck Detection**: Automatically identifies the most time-sensitive financial bottleneck across all active milestones.
- **Healthy Work-Hour Guardrails (5.0h ~ 8.0h / day)**:
  - **Floor Protection (5.0h/day)**: Advises maintaining at least 5.0 hours/day during non-urgent phases to build buffer capital.
  - **Overload Alerts (>8.0h/day)**: Triggers visual alerts if weekday workloads exceed the 8-hour safety ceiling, with automatic suggestions to distribute hours over weekend relief days.
- **Interval Transition Analysis**: Merges same-day deadlines, projects intensity phase-by-phase (Sprint Phase vs. Steady Phase), and forecasts residual capital upon milestone completion.

### 2. 💱 Real-Time Multi-Currency Engine (EUR / CNY / USD)
- Live exchange rate updates via open financial APIs, backed by a 1-hour smart cache and offline fallback resilience.
- Unified dual-currency dashboard displays for **EUR (€)** and **CNY (¥)**, with **USD ($)** hourly rate conversions.
- Natural multi-currency input recognition: `+300eur`, `+2000cny`, `$1200`, `25usd`, `150rmb`.

### 3. 📅 Native Visual Inline Calendar
- Interactive month-view calendar rendered directly within the Telegram chat interface.
- One-tap date selection with smooth month navigation (◀️ / ▶️) — no manual date typing required.
- **Schedule Management**: Tap any weekday to mark as an **Off Day (🏖️ Leave)** or any weekend day to toggle as a **Workday (💼 Overtime)**, instantly recalibrating pacing models.

### 4. ⚡ Instant Natural Language Bookkeeping (NLP Shortcuts)
Skip nested menus and interact directly through one-line chat messages:
- **Income**: `+300eur`, `+2000cny`, `income 500`, `earned 1500`
- **Expense**: `-50eur`, `-200cny`, `spent 100`
- **Update Balance**: `balance 5000`, `total 65000cny`, `savings 3000`
- **Update Hourly Rate**: `rate 20`, `rate 25usd`, `rate 150cny`
- **Complete Milestone**: `done Tuition`, `complete Rent`, `finish Project A` (automatically deducts goal funds)
- **Add Milestone with Date**: `Laptop 1200eur 2026-12-01`
- **Schedule Leaves / Overtime**: `leave 2026-10-25`, `overtime 2026-10-18`, `leave 2026-10-15~2026-10-18 Vacation`

### 5. ⏰ Automated 09:00 Daily Standup & Check-in
- Proactive daily check-in prompt delivered at 09:00 local time.
- Verifies yesterday's cash balance with 1-click interactive buttons for instant adjustments.

### 6. 🔒 Privacy by Design & Zero-Downtime Hot Restore
- **Zero Data Leakage**: Open-source repository contains strictly generic code; `.gitignore` safeguards all `.env`, database, and log files.
- **One-Command Backup (`/backup`)**: Export and download your encrypted/SQLite database in real-time.
- **File Drop Hot-Restore**: Drag-and-drop your `.db` file into the chat to hot-reload all milestones, balances, and schedules in seconds.
- **Headless Seed Injection**: Deploy anywhere with pre-loaded state via the `SEED_DATA` environment variable.

---

## 📱 Telegram Dashboard Preview

```text
📊 【Financial & Goal Multi-Currency Dashboard】
💱 Live Rates: 1 EUR ≈ 7.5510 CNY | 1 EUR ≈ 1.1252 USD
─────────────────
💰 Current Liquidity: €350.00 (¥2,642.85)
⏱️ Hourly Rate: €17.77 / ¥134.22 / $20.00 /hr

📅 【Active Milestones】:
1. 📌 Language School (2026-10-30): €1,324.33 (¥10,000.00) (74.5h)
2. 📌 Rent Quarterly (2026-10-30): €650.00 (¥4,908.14) (36.6h)
3. 📌 Credit Card Due (2026-11-07): €1,121.71 (¥8,470.00) (63.1h)
   (...and 10 more future milestones in [📋 Manage Milestones])
─────────────────
🛡️ 【Core Goal: 100% Coverage · Work Hours Capped at 5.0h ~ 8.0h】:
🎯 Current Decisive Bottleneck: 【Credit Card Due】
   └ Deadline: 2026-11-07 (19 working days excluding leaves/weekends)
   └ Net Financial Gap: €3,275.77 (¥24,735.30) (184.3h)
   👉 Recommended Workload: 7.9 hours / day (Window: 5.0h ~ 8.0h)
   💡 Note: Pacing eases down to 5.0h floor once this bottleneck is cleared.

⚡ 【Sprint Phase】 7.9 hrs/day needed. Stay focused!

🛡️ 【Interval Protection & Phase Breakdown】:
• 🔥 Sprint [Today ➔ 10-30] (19 workdays | School + Rent): 7.9h/day, Surplus: €1,046.29
• 🔥 Sprint [10-30 ➔ 11-07] (5 workdays | Credit Card): 7.9h/day, 100% Cleared
• 🟢 Steady [11-07 ➔ 11-15] (7 workdays | Hardware Upgrade): Drops to 5.8h/day, Surplus: €975.61
• 🟢 Steady [11-15 ➔ 11-30] (11 workdays | Travel Fund): 5.8h/day, Surplus: €1,871.76

📅 Calendar: 🏖️ 7 leaves excluded | 💼 10 weekend shifts enabled
─────────────────
Select an action:
[💰 Update Balance]   [➕ Add Income]
[⏱️ Change Rate]      [➕ Add Milestone]
[📋 Milestones List]  [🏖️ Calendar & Leaves]
[🔄 Refresh Board]
```

---

## 🚀 Deployment Guide

### Option A: Free Cloud Deployment on Render (Recommended · 24/7 Online)

Render provides a free Web Service container tier with zero credit card requirements:

1. **Fork or Clone this repository** to your GitHub account (Public or Private).
2. Sign in to [Render.com](https://render.com) using your GitHub account.
3. Click **New +** ➔ **Web Service**.
4. Select your `budget_check` repository and click **Connect**.
5. Configure the deployment settings:
   - **Name**: `budgetcheck-bot`
   - **Language**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
   - **Instance Type**: `Free`
6. Under **Environment Variables**, add the following keys:
   | Key | Description | Example |
   | :--- | :--- | :--- |
   | `BOT_TOKEN` | Telegram Bot API Token (from @BotFather) | `123456:ABC-DEF...` |
   | `OWNER_CHAT_ID` | Your numeric Telegram User ID (from @userinfobot) | `123456789` |
   | `TIMEZONE` | Timezone identifier | `Europe/Berlin` or `Asia/Shanghai` |
   | `PORT` | Health check port (auto-injected by Render, or `8080`) | `8080` |
   | `SEED_DATA` | *(Optional)* Compressed seed string for pre-populating data | `eJzFWG...` |
7. Click **Deploy Web Service** to go live!

#### ⏱️ Keeping Render Awake 24/7 (Preventing Free-Tier Inactivity Sleep)
Render's free tier spins down web services after 15 minutes of zero incoming web traffic. Because BudgetCheck uses Telegram long-polling rather than inbound web visits, keep it active 24/7 using any free uptime monitor:
1. Copy your Render web service URL (e.g., `https://your-bot.onrender.com`).
2. Go to **[cron-job.org](https://cron-job.org)** or **[UptimeRobot](https://uptimerobot.com)** and create a free account.
3. Add a new HTTP monitor pointing to your Render URL with a **10-minute interval**.
4. **Done!** The periodic ping hits BudgetCheck's built-in HTTP health endpoint (`/`), ensuring the bot stays awake and responds to Telegram buttons 24/7 without hibernating.

---

### Option B: Local Setup (macOS / Linux / Windows)

#### 1. Clone & Set Up Virtual Environment
```bash
git clone https://github.com/Willszs/budget_check.git
cd budget_check

python3 -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

#### 2. Configure Environment Variables
Copy the template configuration file:
```bash
cp .env.example .env
```
Edit `.env` and fill in your credentials:
```env
BOT_TOKEN=your_telegram_bot_token_here
OWNER_CHAT_ID=your_numeric_chat_id_here
REMINDER_HOUR=9
REMINDER_MINUTE=0
TIMEZONE=Asia/Shanghai
```

#### 3. Run the Bot
```bash
python3 bot.py
```

---

### Option C: Docker Container Deployment

The repository includes a lightweight, container-ready `Dockerfile`:

```bash
# Build Docker image
docker build -t budgetcheck-bot .

# Run container with persistent volume
docker run -d \
  --name budgetcheck \
  --restart always \
  -v $(pwd)/data:/app/data \
  -e DB_PATH=/app/data/budget_data.db \
  -e BOT_TOKEN="your_bot_token" \
  -e OWNER_CHAT_ID="your_chat_id" \
  -e TIMEZONE="Asia/Shanghai" \
  budgetcheck-bot
```

---

## ⚙️ Environment Variables Reference

| Variable | Default | Required | Description |
| :--- | :--- | :--- | :--- |
| `BOT_TOKEN` | - | **Yes** | Telegram Bot API authentication token |
| `OWNER_CHAT_ID` | - | **Yes** | Whitelisted Telegram User ID for admin authorization |
| `TIMEZONE` | `Asia/Shanghai` | No | Timezone for calculations and reminder dispatch |
| `REMINDER_HOUR` | `9` | No | Hour of the daily standup reminder (24-hour format) |
| `REMINDER_MINUTE`| `0` | No | Minute of the daily standup reminder |
| `PORT` | - | No | Port for the built-in HTTP health probe (for cloud platforms) |
| `DB_PATH` | `./budget_data.db`| No | Custom path to the SQLite database file |
| `SEED_DATA` | - | No | Base64 zlib string for headless initial database restoration |

---

## 📖 Command & Interaction Reference

### Slash Commands
- `/start` - Launch or refresh the interactive financial dashboard
- `/backup` - Export and download the active SQLite database file (`.db`)

### Natural Language Fast Commands (Chat Input)
| Goal | Example Message |
| :--- | :--- |
| **Add Income** | `+300` / `+500eur` / `income 2000` / `earned 1500` |
| **Record Expense** | `-50` / `-100eur` / `spent 200` |
| **Set Total Liquidity** | `balance 5000` / `total 65000cny` / `savings 3000` |
| **Update Hourly Rate** | `rate 20` / `rate 25usd` / `rate 150cny` |
| **Mark Milestone Done** | `done Rent` / `complete Laptop` / `finish Project A` |
| **Create Milestone** | `Design Project 600eur 2026-11-15` / `Camera 800cny 2026-12-01` |
| **Add Leave Day** | `leave 2026-10-25` / `leave 2026-10-15~2026-10-18 Vacation` |
| **Add Weekend Shift** | `overtime 2026-10-18` / `shift 2026-10-19` |

---

## 🔒 Security & Privacy Guarantee

- **Single-User Access Control**: Unauthenticated messages from unauthorized chat IDs are silently discarded (`is_owner` filter).
- **Zero Credential Exposure**: `.gitignore` strictly prevents `.env`, local `.db` files, and session logs from reaching version control.
- **End-to-End Transport Security**: All communications leverage Telegram's encrypted MTProto and HTTPS/TLS protocols.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE). Feel free to fork, customize, and self-host. If you find BudgetCheck helpful, please consider giving it a ⭐️ **Star** on GitHub!

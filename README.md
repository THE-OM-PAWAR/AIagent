# Telegram AI Assistant - Production Ubuntu Deployment & Backend

A clean, modular, and production-ready Python backend for a personal Telegram AI assistant powered by Google Gemini and persistent MongoDB. Built with `python-telegram-bot` (v21+), `google-genai`, `pymongo` with `AsyncMongoClient`, and `uv`.

---

## 📑 Table of Contents

- [Features & Architecture](#-features--architecture)
- [Project Structure](#-project-structure)
- [Ubuntu Server Prerequisites](#-ubuntu-server-prerequisites)
- [Step-by-Step Installation on Ubuntu](#-step-by-step-installation-on-ubuntu)
- [Environment Configuration](#-environment-configuration)
- [Systemd Service Setup & Auto-Restart](#-systemd-service-setup--auto-restart)
- [Service Management & Logs](#-service-management--logs)
- [Health Checks (CLI & In-Chat)](#-health-checks-cli--in-chat)
- [Security & Access Control](#-security--access-control)
- [Troubleshooting](#-troubleshooting)
- [Automated Testing](#-automated-testing)

---

## 🌟 Features & Architecture

- **Strict Access Control**: Enforces an allowlist of authorized Telegram user IDs (`ALLOWED_TELEGRAM_USER_IDS`). Unauthorized users are rejected immediately with security audit warnings.
- **Persistent Multi-Turn Context**: Uses MongoDB dual-collection schema (`conversations` and `messages`) to maintain chat history across server restarts.
- **Configurable Context Limits**: Limits history injected into Gemini (`CONVERSATION_HISTORY_LIMIT`) to preserve token budgets and avoid bloated prompts.
- **Message Deduplication**: In-memory bounded cache drops duplicate Telegram `update_id`s to avoid redundant model calls.
- **Resilient Request Timeouts**: Bounded HTTP timeouts for Telegram polling and Gemini calls.
- **Lightweight Health Checks**: CLI command and in-chat `/health` command without exposing any public HTTP ports.
- **Production Systemd Service**: Configured with automatic restart on failure (`Restart=on-failure`), graceful shutdown (`TimeoutStopSec=15s`), and process hardening.

---

## 📁 Project Structure

```text
.
├── .env.example              # Environment variable template
├── .gitignore                # Excludes secrets, .venv, caches, and logs
├── requirements.txt          # Production dependencies
├── telegram-ai-bot.service   # Systemd service unit file for Ubuntu
├── README.md                 # Complete documentation and deployment guide
├── main.py                   # Asynchronous application entry point
├── tests/
│   └── test_chatbot.py       # Unit and integration test suite
└── src/
    ├── __init__.py
    ├── config.py             # Environment loading, validation & user authorization
    ├── logger.py             # Structured logging configuration
    ├── healthcheck.py        # Lightweight CLI health check (DB, Telegram, Config)
    ├── bot.py                # Telegram bot application setup, HTTP timeouts & lifecycle
    ├── handlers/
    │   ├── __init__.py
    │   └── handlers.py       # Authorized command/message handlers & deduplication
    ├── gemini/
    │   ├── __init__.py
    │   └── client.py         # Google GenAI client & conversation history builder
    └── database/
        ├── __init__.py
        └── client.py         # Async PyMongo client, schema collections & indexes
```

---

## 📋 Ubuntu Server Prerequisites

- **OS**: Ubuntu 22.04 LTS or Ubuntu 24.04 LTS (x86_64 or ARM64)
- **Python**: 3.11+
- **Database**: MongoDB Community Edition 6.0+ (running locally or via network URI)
- **Tooling**: [uv](https://docs.astral.sh/uv/) package manager
- **Credentials**:
  - Telegram Bot Token from [@BotFather](https://t.me/BotFather)
  - Numeric Telegram User ID from [@userinfobot](https://t.me/userinfobot)
  - Google Gemini API Key from [Google AI Studio](https://aistudio.google.com/)

---

## 🚀 Step-by-Step Installation on Ubuntu

### 1. Update Packages & Install MongoDB
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git gnupg ca-certificates

# Install MongoDB Community Edition (Ubuntu 22.04/24.04)
curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | \
  sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor

echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu $(lsb_release -cs)/mongodb-org/7.0 multiverse" | \
  sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list

sudo apt update
sudo apt install -y mongodb-org

# Start and enable MongoDB
sudo systemctl enable --now mongod
sudo systemctl status mongod
```

### 2. Install UV Package Manager
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env
```

### 3. Deploy Repository
Clone your project to a deployment directory (e.g. `/opt/telegram-ai-bot`):
```bash
sudo mkdir -p /opt/telegram-ai-bot
sudo chown -R $USER:$USER /opt/telegram-ai-bot
cd /opt/telegram-ai-bot

# Copy or clone codebase files here
git clone <your-repo-url> .
```

### 4. Create Virtual Environment & Install Dependencies
```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

---

## ⚙️ Environment Configuration

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
chmod 600 .env  # Restrict read permissions to owner only
```

Edit `.env` with your production values:
```env
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=123456789:ABCDefGhIJKlmNoPQRstuVWXyz

# Comma-separated list of authorized Telegram user IDs
# Messages from users not in this list will be rejected immediately
ALLOWED_TELEGRAM_USER_IDS=123456789

# Google Gemini API Configuration
GEMINI_API_KEY=AIzaSyYourGeminiApiKeyHere
GEMINI_MODEL=gemini-2.5-flash
GEMINI_SYSTEM_INSTRUCTION=You are a helpful, respectful, and concise personal AI assistant.
GEMINI_TIMEOUT_SECONDS=60.0

# MongoDB Configuration
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB_NAME=telegram_ai_bot
CONVERSATION_HISTORY_LIMIT=20

# Application Settings
LOG_LEVEL=INFO
ENVIRONMENT=production
```

> **How to find your numeric Telegram User ID**:
> 1. In Telegram, start a chat with [@userinfobot](https://t.me/userinfobot) or [@raw_data_bot](https://t.me/raw_data_bot).
> 2. Copy the integer `Id` (e.g. `123456789`) and paste it into `ALLOWED_TELEGRAM_USER_IDS`.
> 3. To allow multiple family/team members, separate with commas: `ALLOWED_TELEGRAM_USER_IDS=123456789,987654321`.

---

## 🛠 Systemd Service Setup & Auto-Restart

The repository provides a production systemd service file ([`telegram-ai-bot.service`](file:///Users/om/@code26/@important2026/AI%20Agent/telegram-ai-bot.service)).

### 1. Configure the Unit File
Check user, paths, and environment file in `telegram-ai-bot.service`:
```ini
[Unit]
Description=Personal Telegram AI Assistant Bot
After=network.target mongod.service
Wants=mongod.service

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/telegram-ai-bot
EnvironmentFile=/opt/telegram-ai-bot/.env
ExecStart=/opt/telegram-ai-bot/.venv/bin/python main.py

# Automatic restart on failure
Restart=on-failure
RestartSec=5s
StartLimitIntervalSec=60s
StartLimitBurst=5

# Graceful termination
KillSignal=SIGTERM
TimeoutStopSec=15s
SendSIGKILL=yes

# Hardening
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier=telegram-ai-bot
```

### 2. Install and Enable Service
```bash
sudo cp telegram-ai-bot.service /etc/systemd/system/telegram-ai-bot.service
sudo systemctl daemon-reload
sudo systemctl enable telegram-ai-bot
sudo systemctl start telegram-ai-bot
```

---

## 📊 Service Management & Logs

### Check Service Status
```bash
sudo systemctl status telegram-ai-bot
```

### Restart Service
```bash
sudo systemctl restart telegram-ai-bot
```

### Stop Service
```bash
sudo systemctl stop telegram-ai-bot
```

### Live Stream Logs
View structured logs in real time via `journalctl`:
```bash
# Follow live logs
sudo journalctl -u telegram-ai-bot -f

# View recent 100 log lines
sudo journalctl -u telegram-ai-bot -n 100 --no-pager

# View error-level logs only
sudo journalctl -u telegram-ai-bot -p err -n 50
```

---

## 🩺 Health Checks (CLI & In-Chat)

No public HTTP ports are opened. Health is validated via two lightweight methods:

### 1. CLI Health Check
Execute the health check tool directly on the server (ideal for cron jobs, monitoring agents, or pre-flight checks):
```bash
uv run python -m src.healthcheck
```
**Healthy Output (`exit 0`):**
```json
{
  "status": "healthy",
  "timestamp": "2026-11-07T12:00:00Z",
  "checks": {
    "config": {
      "status": "ok",
      "environment": "production",
      "allowed_users_count": 1
    },
    "mongodb": {
      "status": "ok",
      "database": "telegram_ai_bot"
    },
    "telegram": {
      "status": "ok",
      "bot_username": "my_personal_ai_bot"
    }
  }
}
```

### 2. In-Chat Health Check Command
Authorized users can send `/health` inside Telegram to get instant status feedback:
```text
🏥 System Health Status

• Status: Operational
• MongoDB: Connected (1.2ms)
• Gemini Model: gemini-2.5-flash
• Authorized Users: 1
• Log Level: INFO
• Environment: production
```

---

## 🔒 Security & Access Control

1. **Telegram User ID Allowlist**: Every command and message handler verifies `user.id in config.allowed_telegram_user_ids`. Unauthorized users receive `⛔ Access denied.` and an audit log entry is recorded with their ID and username.
2. **No Message Content Logging**: In compliance with privacy standards, incoming text content and AI prompt payloads are never logged. Only sanitized metadata (e.g. `user_id`, message length, and message IDs) is logged.
3. **Credential Sanitization**: MongoDB connection strings with usernames and passwords are masked automatically (`mongodb://***:***@...`).
4. **No Exposed HTTP Ports**: The bot runs purely via Telegram long-polling; no incoming ports are opened in `ufw` firewall.

---

## ❓ Troubleshooting

| Issue | Cause | Resolution |
| :--- | :--- | :--- |
| **`⛔ Access denied` reply in chat** | Your Telegram user ID is not in `ALLOWED_TELEGRAM_USER_IDS`. | Get your ID from [@userinfobot](https://t.me/userinfobot) and add it to `.env`, then restart service. |
| **`[CONFIG ERROR] Configuration validation failed`** | Missing or blank required environment variables in `.env`. | Verify all required keys in `.env` against `.env.example`. |
| **MongoDB connection fails (`ServerSelectionTimeoutError`)** | MongoDB service is stopped or listening on another port. | Check `sudo systemctl status mongod`. Start with `sudo systemctl start mongod`. |
| **Telegram `InvalidToken` error** | Bot token is mistyped or revoked. | Check `TELEGRAM_BOT_TOKEN` in `.env` using token from [@BotFather](https://t.me/BotFather). |
| **Bot keeps restarting in systemd** | Configuration error or process crash. | Run `sudo journalctl -u telegram-ai-bot -n 50 --no-pager` to inspect the exact traceback. |
| **`Rate limit exceeded` message** | Gemini API free tier quota exhausted. | Wait 1 minute or configure a paid project API key in `GEMINI_API_KEY`. |

---

## 🧪 Automated Testing

Execute the full automated test suite (22 unit & integration tests):
```bash
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

All tests verify configuration validation, authorization enforcement, update deduplication, MongoDB persistence across restarts, context truncation, and error handling.

# Telegram AI Assistant

A clean, production-oriented personal Telegram AI assistant powered by Google Gemini and persistent MongoDB.

---

## ⚡ Features & Commands

- **Access Control**: Restricts bot access to configured Telegram user IDs (`ALLOWED_TELEGRAM_USER_IDS`).
- **Persistent Context**: Multi-turn conversation history stored in MongoDB across restarts.
- **Context Limits**: Configurable history window (`CONVERSATION_HISTORY_LIMIT`) prevents prompt bloat.
- **Deduplication & Timeouts**: Drops duplicate update IDs and enforces bounded HTTP timeouts.
- **Health Checks**: CLI tool and in-chat `/health` command without exposing public HTTP ports.

### Bot Commands
- `/start` - Start the assistant and clear previous conversation context.
- `/clear` - Archive current context and start fresh (alias: `/reset`).
- `/health` - View service status and MongoDB latency.
- `/help` - Display available commands.

---

## 🚀 Setup

### 1. Install Dependencies
```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
```

Set your values in `.env`:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_TELEGRAM_USER_IDS=123456789          # Numeric ID from @userinfobot
GEMINI_API_KEY=your_gemini_key_here
GEMINI_MODEL=gemini-2.5-flash
GEMINI_SYSTEM_INSTRUCTION=You are a helpful, respectful, and concise personal AI assistant.
GEMINI_TIMEOUT_SECONDS=60.0
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB_NAME=telegram_ai_bot
CONVERSATION_HISTORY_LIMIT=20
LOG_LEVEL=INFO
ENVIRONMENT=production
```

---

## ⚙️ Running

```bash
# Run bot
uv run python main.py

# Run health check
uv run python -m src.healthcheck

# Run tests
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

---

## 🖥️ Ubuntu Systemd Service

1. **Install unit file**:
   ```bash
   sudo cp telegram-ai-bot.service /etc/systemd/system/
   # Adjust User, Group, and WorkingDirectory paths in the file if needed
   sudo systemctl daemon-reload
   sudo systemctl enable --now telegram-ai-bot
   ```

2. **Check status & logs**:
   ```bash
   sudo systemctl status telegram-ai-bot
   sudo journalctl -u telegram-ai-bot -f
   ```

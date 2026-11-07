# Telegram AI Assistant - Backend Foundation with Gemini & Persistent MongoDB

A clean, modular, and production-oriented Python backend for a personal Telegram AI assistant powered by Google Gemini and asynchronous MongoDB persistence.

---

## 🛠 Tech Stack

- **Python**: 3.11+
- **Package & Environment Management**: [uv](https://docs.astral.sh/uv/)
- **Telegram Bot Framework**: `python-telegram-bot` (v21+) with async handlers & long polling
- **LLM SDK**: Official `google-genai` SDK
- **Database Driver**: `pymongo` with `AsyncMongoClient`
- **Configuration & Environment**: `python-dotenv`

---

## 📁 Project Structure

```text
.
├── .env.example           # Template for environment variables
├── .gitignore             # Standard Python, secrets, and environment exclusions
├── requirements.txt       # Core project dependencies
├── README.md              # Documentation and setup instructions
├── main.py                # Asynchronous application entry point
├── tests/                 # Comprehensive test suite
│   └── test_chatbot.py
└── src/
    ├── __init__.py
    ├── config.py          # Centralized configuration & environment validation
    ├── logger.py          # Structured logging configuration
    ├── bot.py             # Telegram bot application builder & lifecycle
    ├── handlers/          # Telegram command and message handlers
    │   ├── __init__.py
    │   └── handlers.py
    ├── gemini/            # Google Gemini client & conversational session manager
    │   ├── __init__.py
    │   └── client.py
    └── database/          # Asynchronous MongoDB client & conversation repository
        ├── __init__.py
        └── client.py
```

---

## 🗄️ Database Architecture & Design

### Schema Design
To prevent document size bloat and ensure fast lookups, conversations are split across two collections:

1. **`conversations` collection**:
   Stores metadata for each conversational session:
   - `conversation_id`: Unique identifier (UUID hex).
   - `user_id`: Telegram user identifier.
   - `is_active`: Boolean (`True` while open, `False` when cleared via `/clear` or `/reset`).
   - `message_count`: Total messages exchanged in this conversation.
   - `created_at`: UTC timestamp.
   - `updated_at`: UTC timestamp of latest activity.

2. **`messages` collection**:
   Stores individual messages (never an entire chat in a single document):
   - `conversation_id`: Associates the message with a specific conversation session.
   - `user_id`: Telegram user identifier.
   - `role`: `"user"` or `"model"`.
   - `content`: Text content of the message.
   - `created_at`: UTC timestamp.

### Indexes
Automatically created on application startup:
- `messages`:
  - `idx_conv_created`: `("conversation_id", 1), ("created_at", 1)` for fast chronological history loading.
  - `idx_user_created`: `("user_id", 1), ("created_at", -1)` for user-centric queries.
- `conversations`:
  - `idx_conv_id_unique`: `("conversation_id", 1)` (unique).
  - `idx_user_active_updated`: `("user_id", 1), ("is_active", 1), ("updated_at", -1)` to instantly fetch the active conversation.

---

## 🚀 Setup Instructions (Using UV)

### 1. Prerequisites
- Python 3.11 or higher
- [uv](https://docs.astral.sh/uv/) package manager
- MongoDB server running (local or MongoDB Atlas)
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- A Google Gemini API Key (from [Google AI Studio](https://aistudio.google.com/))

### 2. Create Virtual Environment
```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
uv pip install -r requirements.txt
```

### 4. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Populate your `.env` configuration:
```env
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# Google Gemini API Configuration
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash
GEMINI_SYSTEM_INSTRUCTION=You are a helpful, respectful, and concise personal AI assistant.
GEMINI_TIMEOUT_SECONDS=60.0

# MongoDB Configuration
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB_NAME=telegram_ai_bot
CONVERSATION_HISTORY_LIMIT=20

# Application Settings
LOG_LEVEL=INFO
ENVIRONMENT=development
```

---

## 🤖 Bot Commands & Features

| Command | Description |
| :--- | :--- |
| `/start` | Welcome message and clears previous conversation context |
| `/clear` | Archives active conversation in MongoDB and starts fresh context |
| `/reset` | Alias for `/clear` |
| `/help` | Detailed help instructions and command reference |
| *Text Message* | Retrieves recent history from MongoDB, queries Gemini, and saves turns |

### Key Features
- **Persistent Context**: Conversation history persists across server and bot restarts.
- **Configurable Context Limits**: `CONVERSATION_HISTORY_LIMIT` prevents exceeding LLM context windows or incurring token waste.
- **Chronological Ordering**: Strictly orders history from earliest to latest for Gemini.
- **Security**: Database credentials and API keys are masked in logs and excluded from git.
- **Typing Indicators & Long Message Chunking**: Real-time feedback and automatic splitting for replies exceeding Telegram's 4096-character limit.
- **Graceful Shutdown**: Intercepts `SIGINT`/`SIGTERM` to stop polling and close database connections cleanly.

---

## ⚙️ Running the Bot

Run the bot with `uv`:
```bash
uv run python main.py
```

---

## 🧪 Testing

Run the test suite with `uv`:
```bash
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

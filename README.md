# AIagent - Production Telegram AI Assistant

Production-oriented personal Telegram AI assistant powered by Google Gemini and persistent MongoDB, containerized with Docker Compose.

---

## 🏗️ Architecture

```text
               Telegram Cloud (Long Polling)
                            │
               ┌────────────▼───────────┐
               │    AIagent App (Bot)   │ Slim Python 3.12 (Non-root appuser)
               └────────────┬───────────┘
                            │ (aiagent-network)
               ┌────────────▼───────────┐
               │    MongoDB Database    │ Internal only (Port 27017, Auth enabled)
               │ (Persistent Named Vol) │ Named volume: aiagent_mongo_data
               └────────────────────────┘
```

- **App (`aiagent-app`)**: Slim Python 3.12 container running as a non-root user (`appuser`). Operates via outbound Telegram long polling (no open ports or webhooks required).
- **Database (`aiagent-mongo`)**: MongoDB 7.0 with authentication enabled, persistent volume, and ports isolated strictly inside the internal Docker network.
- **Network (`aiagent-network`)**: Dedicated internal bridge network.

---

## 🚀 Quickstart (Docker Compose)

### 1. Configure Environment
```bash
cp .env.example .env
chmod 600 .env
```
Fill in `.env`:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_TELEGRAM_USER_IDS=123456789          # Numeric ID from @userinfobot
GEMINI_API_KEY=your_gemini_key_here
GEMINI_MODEL=gemini-2.5-flash
GEMINI_SYSTEM_INSTRUCTION=You are a helpful, respectful, and concise personal AI assistant.

# MongoDB Credentials
MONGO_INITDB_ROOT_USERNAME=admin
MONGO_INITDB_ROOT_PASSWORD=your_strong_root_password
MONGO_APP_USERNAME=aiagent_user
MONGO_APP_PASSWORD=your_strong_app_password
MONGODB_DB_NAME=telegram_ai_bot
CONVERSATION_HISTORY_LIMIT=20

# Internal network connection string used by app container
MONGODB_URI=mongodb://aiagent_user:your_strong_app_password@mongo:27017/telegram_ai_bot?authSource=telegram_ai_bot

LOG_LEVEL=INFO
ENVIRONMENT=production
```

### 2. Build and Start Containers
```bash
docker compose up -d --build
```

### 3. Verify Deployment
```bash
# Check container status
docker compose ps

# Stream application logs
docker compose logs -f app
```

---

## 🤖 Telegram Commands

- `/start` - Start or reset conversational context
- `/status` - Display agent operational state, uptime and current environment
- `/health` - Check application, MongoDB latency, and Telegram API status
- `/model` - Display active Gemini model, provider, timeout and context limit
- `/clear` - Archive conversation history and start a fresh session (alias: `/reset`)
- `/help` - Display list of available commands

---

## 🛠️ Operational Commands

### View Logs (Auto-rotated 10MB x 3 files)
```bash
# Live stream all services
docker compose logs -f

# Specific container logs
docker compose logs -f app
docker compose logs -f mongo
```

### Update Application
```bash
# Automated deployment / rollout
./scripts/deploy.sh

# Or manual update
git pull
docker compose build app
docker compose up -d --no-deps app
```

### Restart / Stop Services
```bash
# Restart application container
docker compose restart app

# Stop the entire stack
docker compose down

# Stop and remove volumes (CAUTION: deletes database volume)
docker compose down -v
```

---

## 💾 Backup and Recovery

### 1. Database Backup (`mongodump`)
Create a timestamped compressed backup archive directly from the container:
```bash
mkdir -p backups
docker compose exec -T mongo mongodump \
  --username admin \
  --password "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin \
  --db telegram_ai_bot \
  --archive --gzip > backups/telegram_ai_bot_$(date +%Y%m%d_%H%M%S).archive.gz
```

### 2. Database Recovery (`mongorestore`)
Restore an archive back into MongoDB:
```bash
docker compose exec -T mongo mongorestore \
  --username admin \
  --password "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin \
  --nsInclude="telegram_ai_bot.*" \
  --archive --gzip --drop < backups/your_backup_file.archive.gz
```

---

## 🔒 Security

- **MongoDB Isolation**: MongoDB port `27017` is **never published** to the host machine.
- **Non-Root User**: App container runs as `appuser:appgroup` (UID `10001`).
- **Telegram Access Control**: Enforced by `ALLOWED_TELEGRAM_USER_IDS`. Unauthorized users receive `⛔ Access denied`.
- **Zero Exposed Ingress Ports**: Long polling model eliminates incoming attack surface.

---

## 🚀 CI/CD & GHCR Integration

For continuous delivery using GitHub Container Registry (`ghcr.io`):

1. **Tag & Build Image**:
   ```bash
   docker build -t ghcr.io/<owner>/aiagent:latest .
   docker push ghcr.io/<owner>/aiagent:latest
   ```
2. **Deploy on Server**: Set `AIAGENT_IMAGE=ghcr.io/<owner>/aiagent` and `IMAGE_TAG=latest` in `.env`, then run `./scripts/deploy.sh`.
3. **Required GitHub Secrets** (for future automated GitHub Actions workflows):
   | Secret | Description |
   |---|---|
   | `GHCR_TOKEN` | Personal Access Token (PAT) with `read:packages` / `write:packages` scope |
   | `SSH_HOST` | Ubuntu server public IP or domain |
   | `SSH_USER` | Deploy user (e.g. `ubuntu` or `deploy`) |
   | `SSH_KEY` | SSH private key for server authentication |
   | `ENV_FILE_CONTENT` | Production `.env` secrets content |

---

## 🔧 Troubleshooting

| Issue | Likely Cause | Solution |
|---|---|---|
| `InvalidToken` error | Incorrect `TELEGRAM_BOT_TOKEN` | Verify token with `@BotFather` and update `.env` |
| App cannot reach MongoDB | Bad credentials or network issue | Check `MONGODB_URI` matches `MONGO_APP_USERNAME` / `PASSWORD` |
| `Permission denied` in volume | Host filesystem permission mismatch | Named volume `aiagent_mongo_data` manages UID internally |
| Bot not responding | User ID not in allowed list | Check `ALLOWED_TELEGRAM_USER_IDS` in `.env` against `@userinfobot` |

---

## 🧠 Memory Foundation & RAG Pipeline (Phase 1)

AIagent features a production-grade, hybrid memory system combining **Neon PostgreSQL** (authoritative relational storage and full-text search) and **Qdrant** (dense vector search):

### 1. Memory Architecture
- **5 Memory Categories**: `semantic` (facts/preferences), `episodic` (events/decisions), `project` (architecture/code), `task` (goals/blockers), `working` (ephemeral context).
- **Ingestion Pipeline**: Deduplication, conflict detection, provenance tracking, importance/confidence scoring.
- **Transactional Outbox**: Ensures reliable PostgreSQL-to-Qdrant synchronization without dual-write inconsistencies.
- **Hybrid RAG Retrieval**: Dense vector search + PostgreSQL full-text search fused with **Reciprocal Rank Fusion (RRF)** and multi-factor reranking.
- **User-Level Isolation**: All vector and keyword queries are strictly filtered by `user_id`.

### 2. Database Migrations (Alembic)
Run database migrations on PostgreSQL:
```bash
# Apply migrations
alembic upgrade head

# Generate SQL without applying
alembic upgrade head --sql
```

### 3. Safe MongoDB Migration
Migrate historical conversations and extract memories from MongoDB without data loss:
```bash
uv run python -c "
import asyncio
from src.database.mongo_migrator import MongoPostgresMigrator
from src.config import Config
cfg = Config.load()
migrator = MongoPostgresMigrator(
    mongodb_uri=cfg.mongodb_uri,
    mongodb_db_name=cfg.mongodb_db_name,
    postgres_url=cfg.database_url,
)
asyncio.run(migrator.run_migration(extract_memories=True))
"
```

---

## ⚡ Intelligent Context Engine (Phase 2)

AIagent features a modular, multi-source context construction system under `src/agent/context/`:

- **4 Context Sources**:
  1. Chronological conversation history (from PostgreSQL / in-memory sessions).
  2. Semantic & episodic memories (from Phase 1 hybrid RAG).
  3. Project context and architectural decisions.
  4. Active tasks & working state.
- **Intelligent Selector & Deduplication**:
  - Eliminates near-duplicate facts using token-level Jaccard similarity.
  - Suppresses memories redundant with recent conversation history turns.
  - Strict user and project isolation preventing cross-tenant leakage.
- **Multi-Factor Ranking & Decision Protection**:
  - Scores context items using relevance, importance, confidence, exponential recency decay, and project match.
  - Detects architectural decisions and user constraints, flagging them with `is_protected=True` so they survive budget pruning.
- **Model-Specific Token Budgeting**:
  - Accurate token counting via `tiktoken` BPE with calibrated fallback.
  - Dedicated budgets and response reservation buffers for Gemini 2.5 Flash, Gemini 1.5 Pro, GPT-4o, Claude 3.5 Sonnet, etc.
- **Lightweight Versioned Summarizer**:
  - Automatically summarizes long threads preserving key decisions, open questions, task state, and constraints.
  - Treats summaries as derived information with full provenance without touching raw message turns.

---

## 🎯 Intelligence Orchestrator (Phase 3)

The central request-processing orchestrator in `src/agent/orchestrator.py` connects the Telegram interface, Neon PostgreSQL, Phase 1 Memory RAG, Phase 2 Context Engine, and Google Gemini:

- **12-Step Complete Request Lifecycle**:
  1. Telegram message reception with `update_id` deduplication.
  2. User authentication & identification with log masking.
  3. Unique execution tracking (`exec_<uuid12>`).
  4. Active conversation resolution & history retrieval (PostgreSQL / MongoDB).
  5. Hybrid RAG memory retrieval across semantic, episodic, project, and task domains.
  6. Context selection, token budgeting, and summarization.
  7. Prompt assembly with injected memory facts & system instruction.
  8. Gemini invocation with retry, timeout, and rate-limit handling.
  9. Response validation (empty/malformed response protection).
  10. Atomically persisting message turns in database.
  11. Non-blocking background memory extraction & transactional sync.
  12. Execution completion tracking and latency metric recording.
- **Reliability & Concurrency**:
  - `ConversationLockManager`: Per-user/conversation async locks prevent race conditions and state corruption.
  - `UpdateDeduplicator`: Suppresses duplicate Telegram delivery attempts.
  - Failure Shielding: Failed memory extraction never loses a successfully delivered user response.
  - Safe Fallbacks: Gracefully degrades when database or RAG components are unavailable.

---

## 🤖 Multi-Agent Architecture & Model Routing (Phase 4)

AIagent features three specialized logical agents and multi-tier model routing under `src/agents/` and `src/llm/`:

- **3 Specialized Agents Sharing Infrastructure**:
  1. **General Assistant (`general`)**: Everyday conversations, personal assistance, and natural context-aware dialog.
  2. **Analyst Agent (`analyst`)**: Deep technical research, architectural tradeoffs, comparative evaluations, and risk assessments.
  3. **Executor Agent (`executor`)**: Task decomposition, actionable step planning, status tagging (`[TODO]`, `[IN_PROGRESS]`, `[COMPLETED]`, `[BLOCKED]`), and automatic persistence of task memories.
- **Shared Infrastructure**:
  - All agents share Neon PostgreSQL, Qdrant vector retrieval, the Phase 1 Memory Manager, and the Phase 2 Context Engine.
- **Intelligent Intent & Role Router (`src/agent/router.py`)**:
  - Deterministic intent classification with regex pattern rules and heuristic ambiguity scoring.
  - Safe fallback to General Assistant; avoids unnecessary agent-switching and loops.
- **Model Tiers & Provider Fallback (`src/llm/`)**:
  - **Small (`gemini-2.5-flash`)**: Quick turns, greetings, classification, and summarization.
  - **Medium (`gemini-2.5-flash`)**: Standard conversation and everyday reasoning.
  - **Large (`gemini-1.5-pro`)**: Complex analytical queries, tradeoffs, and deep planning.
  - **Automatic Fallback**: If a Large tier model encounters rate limits or errors, `LLMManager` automatically falls back to Medium tier.
  - **Telemetry & Cost Tracking**: Accurately tracks input/output tokens and estimated USD costs per request.

---

## 🧪 Testing

Run the full automated test suite (75 unit, integration, infrastructure, memory RAG, context engine, orchestrator, and multi-agent tests):
```bash
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

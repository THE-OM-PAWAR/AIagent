# AIagent - Production Telegram AI Assistant

Production-oriented personal Telegram AI assistant powered by Google Gemini and persistent MongoDB, containerized with Docker Compose and Nginx reverse proxy.

---

## 🏗️ Architecture

```text
               Internet
                  │
          ┌───────▼───────┐
          │     Nginx     │  Ports 80 / 443 (HTTP/HTTPS, TLS, /healthz)
          └───────┬───────┘
                  │  (aiagent-network)
      ┌───────────┴───────────┐
      │                       │
┌─────▼─────┐           ┌─────▼─────┐
│  AIagent  │ Long-Poll │  MongoDB  │ Internal only (Port 27017)
│    App    ├───────────►   (Auth)  │ Persistent Named Volume
└───────────┘           └───────────┘
```

- **App (`aiagent-app`)**: Slim Python 3.12 container running as a non-root user (`appuser`). Operates via long-polling (no public webhooks required).
- **Database (`aiagent-mongo`)**: MongoDB 7.0 with authentication enabled, persistent volume, and ports isolated strictly inside the internal Docker network.
- **Gateway (`aiagent-nginx`)**: Modular Nginx reverse proxy providing TLS termination foundation, ACME challenge location, and `/healthz`.
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

# Nginx Gateway Ports
NGINX_PORT_HTTP=80
NGINX_PORT_HTTPS=443
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

# Check Nginx health endpoint
curl -i http://localhost/healthz

# Stream application logs
docker compose logs -f app
```

---

## 🛠️ Operational Commands

### View Logs (Auto-rotated 10MB x 3 files)
```bash
# Live stream all services
docker compose logs -f

# Specific container logs
docker compose logs -f app
docker compose logs -f mongo
docker compose logs -f nginx
```

### Update Application
```bash
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

## 🔒 Security & TLS Certificates

- **MongoDB Isolation**: MongoDB port `27017` is **never published** to the host machine.
- **Non-Root User**: App container runs as `appuser:appgroup` (UID `10001`).
- **Telegram Access Control**: Enforced by `ALLOWED_TELEGRAM_USER_IDS`. Unauthorized users receive `⛔ Access denied`.
- **TLS Certificates**: Mount production certificates (e.g., from Certbot) into `./docker/nginx/ssl/cert.pem` and `./docker/nginx/ssl/key.pem`.
  - Self-signed helper: `docker/nginx/ssl/generate-self-signed.sh`

---

## 🧪 Testing

Run the full automated test suite (24 unit, integration, and infrastructure tests):
```bash
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

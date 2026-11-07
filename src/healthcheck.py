"""Lightweight CLI health check for application and database connectivity."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

# Ensure repository root is on sys.path when invoked directly as a script
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from telegram import Bot
from telegram.request import HTTPXRequest

from src.config import Config, ConfigError
from src.database import DatabaseClient


async def run_health_check() -> tuple[bool, dict]:
    """Execute connectivity checks against configuration, MongoDB, and Telegram API.

    Returns:
        Tuple of (is_healthy, report_dict)
    """
    report = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {},
    }
    all_ok = True

    # 1. Configuration check
    try:
        config = Config.load()
        report["checks"]["config"] = {
            "status": "ok",
            "environment": config.environment,
            "allowed_users_count": len(config.allowed_telegram_user_ids),
        }
    except ConfigError as exc:
        report["checks"]["config"] = {
            "status": "error",
            "error": "Configuration validation failed",
        }
        report["status"] = "unhealthy"
        return False, report
    except Exception as exc:
        report["checks"]["config"] = {
            "status": "error",
            "error": exc.__class__.__name__,
        }
        report["status"] = "unhealthy"
        return False, report

    # 2. MongoDB Connectivity Check
    db_client = DatabaseClient(uri=config.mongodb_uri, db_name=config.mongodb_db_name)
    try:
        await db_client.connect()
        report["checks"]["mongodb"] = {
            "status": "ok",
            "database": config.mongodb_db_name,
        }
    except Exception as exc:
        report["checks"]["mongodb"] = {
            "status": "error",
            "error": exc.__class__.__name__,
        }
        all_ok = False
    finally:
        try:
            await db_client.close()
        except Exception:
            pass

    # 3. Telegram API Check
    try:
        request = HTTPXRequest(connect_timeout=5.0, read_timeout=5.0)
        bot = Bot(token=config.telegram_bot_token, request=request)
        me = await bot.get_me()
        report["checks"]["telegram"] = {
            "status": "ok",
            "bot_username": me.username,
        }
    except Exception as exc:
        report["checks"]["telegram"] = {
            "status": "error",
            "error": exc.__class__.__name__,
        }
        all_ok = False

    if not all_ok:
        report["status"] = "unhealthy"

    return all_ok, report


def main() -> None:
    """CLI entry point for health check reporting."""
    try:
        is_healthy, report = asyncio.run(run_health_check())
        print(json.dumps(report, indent=2))
        sys.exit(0 if is_healthy else 1)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "unhealthy",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "error": exc.__class__.__name__,
                },
                indent=2,
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

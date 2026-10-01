# -*- coding: utf-8 -*-
"""
Единый источник настроек. Значения читаются из .env (через python-dotenv) и из окружения.
Секретов в коде нет: cookie сессии лежит в отдельном файле (HCP_COOKIE_FILE).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent

# читаем .env; override=False — реальные переменные окружения (напр. из Docker) важнее
load_dotenv(BASE / ".env", override=False)


def _path(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (BASE / p)


# --- Housecall Pro ---
HCP_BASE_URL = os.getenv("HCP_BASE_URL", "https://pro.housecallpro.com").rstrip("/")
HCP_COOKIE_FILE = _path(os.getenv("HCP_COOKIE_FILE", "secrets/hcp_cookie.txt"))
HCP_PAGE_SIZE = int(os.getenv("HCP_PAGE_SIZE", "80"))
# Одно окно на всё: и докуда листать журнал, и за сколько дней качать аудио.
# 0 = без ограничения.
HCP_BACKFILL_DAYS = int(os.getenv("HCP_BACKFILL_DAYS", "60"))
HCP_DELAY_LIST = float(os.getenv("HCP_DELAY_LIST", "1.5"))
HCP_DELAY_DOWNLOAD = float(os.getenv("HCP_DELAY_DOWNLOAD", "0.2"))
HCP_LIST_WORKERS = int(os.getenv("HCP_LIST_WORKERS", "3"))
HCP_DOWNLOAD_WORKERS = int(os.getenv("HCP_DOWNLOAD_WORKERS", "15"))
COOKIE_NAME = "_housecall-web_session_with_domain"
USER_AGENT = os.getenv("HCP_USER_AGENT", "hcp-calls-collector/1.0")

# --- PostgreSQL (контейнер hcp_pg из docker-compose.yml) ---
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5439")
PG_USER = os.getenv("PG_USER", "hcp")
PG_PASSWORD = os.getenv("PG_PASSWORD", "change_me_local_only")
PG_DB = os.getenv("PG_DB", "hcp_calls")
PG_DSN = os.getenv(
    "PG_DSN",
    f"host={PG_HOST} port={PG_PORT} user={PG_USER} password={PG_PASSWORD} "
    f"dbname={PG_DB} connect_timeout=5",
)

# --- Файлы на диске ---
DATA_DIR = _path(os.getenv("DATA_DIR", "data"))
REC_DIR = DATA_DIR / "recordings"   # аудиофайлы: data/recordings/ГГГГ/ММ/ДД/RE….wav

# --- Уведомления в Telegram (необязательно) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

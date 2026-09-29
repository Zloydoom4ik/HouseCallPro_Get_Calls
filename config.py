# -*- coding: utf-8 -*-
"""
Единый источник настроек. Читает .env (если есть) и переменные окружения.
Секретов в коде нет: cookie сессии лежит в отдельном файле (HCP_COOKIE_FILE).
"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env(BASE / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _path(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (BASE / p)


# --- Housecall Pro ---
HCP_BASE_URL = _get("HCP_BASE_URL", "https://pro.housecallpro.com").rstrip("/")
HCP_COOKIE_FILE = _path(_get("HCP_COOKIE_FILE", "secrets/hcp_cookie.txt"))
HCP_PAGE_SIZE = int(_get("HCP_PAGE_SIZE", "80"))
# Сколько дней истории тянуть при первом полном проходе (записи старше ~6 мес уже удалены).
# Одно окно на всё: и докуда листать журнал, и за сколько дней качать аудио.
# Ставь сам сколько нужно (напр. 60 = последние 2 месяца). 0 = без ограничения.
HCP_BACKFILL_DAYS = int(_get("HCP_BACKFILL_DAYS", "60"))
# Паузы между запросами, сек — не нагружаем их сервер.
HCP_DELAY_LIST = float(_get("HCP_DELAY_LIST", "1.5"))
HCP_DELAY_DOWNLOAD = float(_get("HCP_DELAY_DOWNLOAD", "0.2"))
# Параллельность: сколько страниц журнала / файлов качать одновременно.
HCP_LIST_WORKERS = int(_get("HCP_LIST_WORKERS", "3"))
HCP_DOWNLOAD_WORKERS = int(_get("HCP_DOWNLOAD_WORKERS", "15"))
COOKIE_NAME = "_housecall-web_session_with_domain"
USER_AGENT = _get("HCP_USER_AGENT", "hcp-calls-collector/1.0")

# --- PostgreSQL (контейнер hcp_pg из docker-compose.yml) ---
PG_HOST = _get("PG_HOST", "localhost")
PG_PORT = _get("PG_PORT", "5439")
PG_USER = _get("PG_USER", "hcp")
PG_PASSWORD = _get("PG_PASSWORD", "change_me_local_only")
PG_DB = _get("PG_DB", "hcp_calls")
PG_DSN = _get(
    "PG_DSN",
    f"host={PG_HOST} port={PG_PORT} user={PG_USER} password={PG_PASSWORD} dbname={PG_DB} "
    f"connect_timeout=5",
)

# --- Файлы на диске ---
DATA_DIR = _path(_get("DATA_DIR", "data"))
REC_DIR = DATA_DIR / "recordings"   # аудиофайлы: data/recordings/ГГГГ/ММ/ДД/RE….wav

# --- Уведомления в Telegram (необязательно) ---
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID", "").strip()

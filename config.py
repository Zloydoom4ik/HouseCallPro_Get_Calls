# -*- coding: utf-8 -*-
"""
Единый источник настроек. Читает файл .env (если он есть) и переменные окружения.
Секретов в коде нет: cookie сессии лежит в отдельном файле (см. HCP_COOKIE_FILE).
"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _load_env(path: Path) -> None:
    """Простейший парсер .env, без внешних зависимостей."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        # не перетираем то, что уже задано в окружении
        os.environ.setdefault(key.strip(), val.strip())


_load_env(BASE / ".env")


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _path(rel: str) -> Path:
    """Относительный путь считаем от папки проекта, абсолютный оставляем как есть."""
    p = Path(rel)
    return p if p.is_absolute() else (BASE / p)


# --- Housecall Pro ---
HCP_BASE_URL = _get("HCP_BASE_URL", "https://pro.housecallpro.com").rstrip("/")
HCP_COOKIE_FILE = _path(_get("HCP_COOKIE_FILE", "secrets/hcp_cookie.txt"))
HCP_PAGE_SIZE = int(_get("HCP_PAGE_SIZE", "80"))
# Сколько дней истории тянуть при первом (полном) проходе.
# Записи старше ~6 месяцев Housecall Pro всё равно удаляет, поэтому по умолчанию ~7 мес.
HCP_BACKFILL_DAYS = int(_get("HCP_BACKFILL_DAYS", "210"))
# Вежливые паузы между запросами (сек), чтобы не нагружать их сервер.
HCP_DELAY_LIST = float(_get("HCP_DELAY_LIST", "1.5"))
HCP_DELAY_DOWNLOAD = float(_get("HCP_DELAY_DOWNLOAD", "1.0"))
# Имя cookie, по которому Housecall Pro узнаёт залогиненного пользователя.
COOKIE_NAME = "_housecall-web_session_with_domain"
# Обычный User-Agent, чтобы запрос выглядел как из браузера.
USER_AGENT = _get(
    "HCP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)

# --- Хранилище ---
DATA_DIR = _path(_get("DATA_DIR", "data"))
DB_PATH = _path(_get("DB_PATH", "data/hcp_calls.db"))
REC_DIR = DATA_DIR / "recordings"   # сюда кладём аудиофайлы
RAW_DIR = DATA_DIR / "raw"          # сырой JSON каждой страницы (страховка)

# --- Уведомления в Telegram (необязательно) ---
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID", "").strip()

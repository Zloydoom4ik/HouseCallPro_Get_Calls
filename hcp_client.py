# -*- coding: utf-8 -*-
"""
Клиент к внутреннему API Housecall Pro (/alpha/voice/call_logs) и к файлам Twilio.

Важное:
  * Список звонков берём у Housecall Pro по cookie сессии.
  * Сами аудиофайлы качаем прямо у Twilio по ссылке из ответа — БЕЗ cookie
    (чтобы не передавать сессию Housecall Pro третьей стороне).
  * cookie сессии после каждого ответа обновляется и переписывается в файл —
    так вход не «протухает», пока скрипт ходит хотя бы раз в несколько дней.
"""
import time
import threading
import requests
import config

_cookie_lock = threading.Lock()   # cookie пишут несколько потоков — по очереди


class AuthError(Exception):
    """Сессия истекла / нас разлогинило. Нужно заново скопировать cookie."""


class SchemaError(Exception):
    """Ответ пришёл, но структура не та, что ожидали (API мог поменяться)."""


EXPANDS = ["call_dispositions", "customers", "recordings", "voicemails"]


def _read_cookie() -> str:
    if not config.HCP_COOKIE_FILE.exists():
        raise AuthError(
            f"Нет файла с cookie: {config.HCP_COOKIE_FILE}\n"
            f"Скопируй значение cookie '{config.COOKIE_NAME}' из браузера в этот файл "
            f"(см. README, раздел «Cookie»)."
        )
    return config.HCP_COOKIE_FILE.read_text(encoding="utf-8").strip()


def _write_cookie(value: str) -> None:
    with _cookie_lock:
        config.HCP_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.HCP_COOKIE_FILE.write_text(value.strip(), encoding="utf-8")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    cookie = _read_cookie()
    s.cookies.set(config.COOKIE_NAME, cookie, domain="pro.housecallpro.com", path="/")
    return s


def _refresh_cookie(session: requests.Session) -> None:
    fresh = session.cookies.get(config.COOKIE_NAME, domain="pro.housecallpro.com")
    if fresh:
        _write_cookie(fresh)


def _with_retries(fn, what: str, attempts: int = 5):
    """Повтор при 429/5xx/сетевых сбоях с нарастающей паузой. AuthError/SchemaError не повторяем."""
    delay = 5
    for i in range(1, attempts + 1):
        try:
            return fn()
        except (AuthError, SchemaError):
            raise
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code not in (429, 500, 502, 503, 504) or i == attempts:
                raise
            wait = _retry_after(e.response, delay)
            print(f"  [retry] {what}: HTTP {code}, пауза {wait}s ({i}/{attempts})")
            time.sleep(wait)
            delay = min(delay * 2, 120)
        except requests.RequestException as e:
            if i == attempts:
                raise
            print(f"  [retry] {what}: {e}, пауза {delay}s ({i}/{attempts})")
            time.sleep(delay)
            delay = min(delay * 2, 120)


def _retry_after(resp, default: int) -> int:
    if resp is None:
        return default
    ra = resp.headers.get("Retry-After")
    if ra and ra.isdigit():
        return int(ra)
    reset = resp.headers.get("RateLimit-Reset")
    if reset and reset.isdigit():
        return max(default, int(reset) - int(time.time()))
    return default


def fetch_page(session: requests.Session, page: int) -> dict:
    """Одна страница журнала звонков. Бросает AuthError/SchemaError при проблемах."""
    url = f"{config.HCP_BASE_URL}/alpha/voice/call_logs"
    params = [
        ("id", "call_logs"),
        ("page", str(page)),
        ("page_size", str(config.HCP_PAGE_SIZE)),
    ] + [("expand[]", e) for e in EXPANDS]

    def do():
        r = session.get(url, params=params, timeout=60)
        if r.status_code in (401, 403):
            raise AuthError(f"HTTP {r.status_code} от Housecall Pro — сессия истекла.")
        r.raise_for_status()
        if "json" not in r.headers.get("content-type", "").lower():
            # обычно значит, что нас перекинуло на страницу логина
            raise AuthError("Ответ не JSON — вероятно, сессия истекла (страница логина).")
        data = r.json()
        if not isinstance(data, dict) or "data" not in data:
            raise SchemaError(f"В ответе нет поля 'data'. Ключи: {list(data)[:10]}")
        _refresh_cookie(session)
        return data

    return _with_retries(do, f"page {page}")


def download_recording(wav_url: str, dest, stereo: bool = True) -> int:
    """
    Качаем запись у Twilio отдельной сессией без cookie.
    stereo=True добавляет ?RequestedChannels=2 (оператор и клиент на разных каналах);
    если Twilio отвечает 400 — падаем обратно на моно.
    Возвращает размер файла в байтах.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    def attempt(url):
        with requests.get(url, timeout=120, stream=True,
                          headers={"User-Agent": config.USER_AGENT}) as r:
            if r.status_code == 400 and stereo:
                return None  # сигнал «нет стерео, пробуем моно»
            r.raise_for_status()
            size = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk)
                        size += len(chunk)
            return size

    url = wav_url + ("?RequestedChannels=2" if stereo else "")
    size = _with_retries(lambda: attempt(url), "download")
    if size is None:  # стерео недоступно — моно
        size = _with_retries(lambda: attempt(wav_url), "download-mono")
    if not size:
        raise requests.RequestException("пустой файл")
    tmp.replace(dest)
    return size

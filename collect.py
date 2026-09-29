# -*- coding: utf-8 -*-
"""
Сборщик звонков Housecall Pro.

  1) Листает журнал звонков (/alpha/voice/call_logs) и пишет метаданные в PostgreSQL.
  2) Скачивает аудиозаписи из Twilio на диск (стерео, если доступно).
  3) Помнит, что уже выгружено, — повторный запуск не создаёт дублей.

Режимы:
  python collect.py                 инкрементальный проход (крон раз в сутки)
  python collect.py --full          полный проход истории назад на HCP_BACKFILL_DAYS
  python collect.py --metadata-only только метаданные, без скачивания файлов
  python collect.py --downloads-only только докачать записи, что уже в БД
  python collect.py --max-pages N   ограничить число страниц (для проверки)

Коды выхода: 0 — ок; 2 — сессия истекла (нужен новый cookie); 3 — API изменился; 1 — прочее.
"""
import sys
import time
import argparse
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import db
from hcp_client import (
    make_session, fetch_page, download_recording, AuthError, SchemaError,
)


# ----------------------------- разбор JSON -----------------------------

def window_start():
    """Начало окна выгрузки: now - HCP_BACKFILL_DAYS. None, если лимита нет (0)."""
    if config.HCP_BACKFILL_DAYS <= 0:
        return None
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=config.HCP_BACKFILL_DAYS)


def parse_dt(value):
    """Дата из ISO-строки / epoch / datetime -> aware datetime или None."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _customer(call: dict):
    cust = call.get("customer") or call.get("customers")
    if isinstance(cust, list):
        cust = cust[0] if cust else None
    if not isinstance(cust, dict):
        return None, None
    uuid = _first(cust, "uuid", "id")
    name = _first(cust, "name", "display_name", "full_name")
    if not name:
        fn, ln = cust.get("first_name") or "", cust.get("last_name") or ""
        name = (fn + " " + ln).strip() or None
    return uuid, name


def _dispositions(call: dict):
    disp = call.get("call_dispositions") or call.get("call_disposition")
    if isinstance(disp, dict) and "data" in disp:
        disp = disp["data"]
    if isinstance(disp, list):
        names = [_first(x, "name", "label", "value") if isinstance(x, dict) else str(x)
                 for x in disp]
        return ", ".join(n for n in names if n) or None
    if isinstance(disp, dict):
        return _first(disp, "name", "label", "value")
    return disp if isinstance(disp, str) else None


def _recordings(call: dict):
    rec = call.get("recordings")
    if isinstance(rec, dict) and "data" in rec:
        rec = rec["data"]
    return rec if isinstance(rec, list) else []


def flatten_call(call: dict) -> dict:
    cust_uuid, cust_name = _customer(call)
    created = parse_dt(_first(call, "created_at", "started_at", "created", "date"))
    voicemail = call.get("voicemail") or call.get("voicemails")
    return {
        "uuid": _first(call, "uuid", "id"),
        "twilio_call_sid": _first(call, "sid", "call_sid"),
        "direction": _first(call, "direction", "type", "call_type"),
        "status": _first(call, "status", "call_status"),
        "from_number": call.get("from"),
        "to_number": call.get("to"),
        "customer_uuid": cust_uuid,
        "customer_name": cust_name,
        "duration_seconds": _int(_first(call, "duration", "duration_seconds")),
        "disposition": _dispositions(call),
        "has_voicemail": bool(voicemail),
        "call_created_at": created.isoformat() if created else None,
        "raw_json": call,          # весь исходный JSON -> JSONB
    }


def flatten_recording(rec: dict, call_uuid: str, call_created_iso) -> dict:
    return {
        "sid": _first(rec, "sid", "recording_sid"),
        "call_uuid": call_uuid,
        "rec_uuid": _first(rec, "uuid", "id"),
        "status": _first(rec, "status"),
        "duration_seconds": _int(_first(rec, "duration", "duration_seconds")),
        "discarded_at": _first(rec, "discarded_at"),
        "wav_url": _first(rec, "wav_url", "wav", "media_url"),
        "mp3_url": _first(rec, "mp3_url", "mp3"),
        "call_created_at": call_created_iso,
    }


# ----------------------------- уведомления -----------------------------

def notify(text: str) -> None:
    print(text, file=sys.stderr)
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:4000]},
            timeout=20,
        )
    except Exception as e:
        print(f"(не удалось отправить в Telegram: {e})", file=sys.stderr)


# ----------------------------- фаза 1: журнал -----------------------------

def _store_page(conn, data: dict, page: int):
    """Записать звонки и записи одной страницы в БД (только главный поток).
    Возвращает (сколько новых звонков, самая старая дата на странице)."""
    calls = data.get("data") or []
    page_new = 0
    oldest = None
    for call in calls:
        flat = flatten_call(call)
        if not flat["uuid"]:
            continue
        if not db.call_exists(conn, flat["uuid"]):
            page_new += 1
        db.upsert_call(conn, flat)
        for rec in _recordings(call):
            fr = flatten_recording(rec, flat["uuid"], flat["call_created_at"])
            if fr["sid"] and fr["wav_url"]:
                db.upsert_recording(conn, fr)
        cdt = parse_dt(flat["call_created_at"])
        if cdt and (oldest is None or cdt < oldest):
            oldest = cdt
    return page_new, oldest


def _log_page(page, total_pages, ncalls, page_new, oldest):
    print(f"  стр.{page}" + (f"/{total_pages}" if total_pages else "")
          + f": звонков {ncalls}, новых {page_new}"
          + (f", самый старый {oldest.date()}" if oldest else ""))


def sync_metadata(conn, session, full: bool, max_pages=None) -> int:
    """Фаза 1: обход журнала. Скачивание страниц — в HCP_LIST_WORKERS потоков,
    запись в БД — в главном потоке. Возвращает число новых звонков."""
    watermark = parse_dt(db.get_state(conn, "last_run_at"))
    if watermark:
        watermark -= dt.timedelta(days=2)   # запас на «поздние» записи и итоги
    cutoff = window_start()   # None = без ограничения по дате

    # первая страница — узнаём число страниц
    data = fetch_page(session, 1)
    total_pages = data.get("total_pages_count") or 1
    n, oldest = _store_page(conn, data, 1)
    conn.commit()
    _log_page(1, total_pages, len(data.get("data") or []), n, oldest)
    total_new = n

    last = total_pages
    if max_pages:
        last = min(last, max_pages)

    # --- инкремент: последовательно, порядок важен для условий остановки ---
    if not full:
        known_pages_in_row = 0 if n else 1
        page = 2
        while page <= last:
            data = fetch_page(session, page)
            n, oldest = _store_page(conn, data, page)
            conn.commit()
            total_new += n
            _log_page(page, total_pages, len(data.get("data") or []), n, oldest)
            if n == 0:
                known_pages_in_row += 1
                if known_pages_in_row >= 2:
                    print("  две страницы подряд без новых звонков — стоп.")
                    break
            else:
                known_pages_in_row = 0
            if watermark and oldest and oldest < watermark:
                print("  ушли глубже отметки прошлого запуска — стоп.")
                break
            page += 1
            time.sleep(config.HCP_DELAY_LIST)
        return total_new

    # --- полный проход: качаем страницы пачками по HCP_LIST_WORKERS потоков ---
    workers = max(1, config.HCP_LIST_WORKERS)
    page = 2
    with ThreadPoolExecutor(max_workers=workers) as ex:
        while page <= last:
            chunk = list(range(page, min(page + workers, last + 1)))
            fetched = sorted(ex.map(lambda p: (p, fetch_page(session, p)), chunk),
                             key=lambda x: x[0])
            stop = False
            for p, pdata in fetched:
                n, oldest = _store_page(conn, pdata, p)
                total_new += n
                _log_page(p, total_pages, len(pdata.get("data") or []), n, oldest)
                if cutoff and oldest and oldest < cutoff:
                    stop = True
            conn.commit()
            if stop:
                print("  достигли границы бэкфилла — стоп.")
                break
            page += workers

    return total_new


# ----------------------------- фаза 2: скачивание -----------------------------

def _download_one(row) -> tuple:
    """Только сеть/диск (выполняется в рабочем потоке). БД здесь НЕ трогаем.
    Возвращает (sid, статус, путь|None, размер, ошибка|None)."""
    sid = row["sid"]
    created = parse_dt(row["call_created_at"]) or dt.datetime.now(dt.timezone.utc)
    dest = config.REC_DIR / f"{created:%Y/%m/%d}" / f"{sid}.wav"
    if dest.exists() and dest.stat().st_size > 0:
        return (sid, "downloaded", str(dest), dest.stat().st_size, None)
    try:
        size = download_recording(row["wav_url"], dest, stereo=True)
        return (sid, "downloaded", str(dest), size, None)
    except Exception as e:
        return (sid, "failed", None, 0, str(e))


def download_pending(conn) -> tuple[int, int]:
    """Фаза 2: скачивание в HCP_DOWNLOAD_WORKERS потоков.
    Файлы качают рабочие потоки, а запись в БД — только главный поток."""
    since = window_start()
    rows = db.get_downloadable(conn, since=since)
    total = len(rows)
    win = f"за последние {config.HCP_BACKFILL_DAYS} дн." if since else "без ограничения по дате"
    print(f"К скачиванию: {total} записей ({win}), потоков: {config.HCP_DOWNLOAD_WORKERS}")
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=max(1, config.HCP_DOWNLOAD_WORKERS)) as ex:
        futures = [ex.submit(_download_one, r) for r in rows]
        for i, fut in enumerate(as_completed(futures), 1):
            sid, status, path, size, err = fut.result()
            if status == "downloaded":
                db.mark_recording(conn, sid, download_status="downloaded",
                                  file_path=path, file_bytes=size, last_error=None)
                ok += 1
            else:
                db.bump_attempt(conn, sid, err)
                fail += 1
            if i % 25 == 0 or i == total:
                conn.commit()
                print(f"  {i}/{total}  скачано:{ok} ошибок:{fail}")
        conn.commit()
    return ok, fail


# ----------------------------- main -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Сборщик звонков Housecall Pro")
    ap.add_argument("--full", action="store_true", help="полный проход истории")
    ap.add_argument("--metadata-only", action="store_true", help="без скачивания файлов")
    ap.add_argument("--downloads-only", action="store_true", help="только докачать из БД")
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    # вывод построчно без буферизации (иначе в Git Bash ничего не видно до конца работы)
    sys.stdout.reconfigure(line_buffering=True)

    conn = db.connect()
    db.init(conn)

    full = args.full
    if not args.downloads_only and db.calls_count(conn) == 0:
        print("База пустая — делаю первый полный проход.")
        full = True

    try:
        if not args.downloads_only:
            new = sync_metadata(conn, make_session(), full=full, max_pages=args.max_pages)
            print(f"Новых звонков за проход: {new}")
        if not args.metadata_only:
            ok, fail = download_pending(conn)
            print(f"Скачано: {ok}, ошибок: {fail}")
    except AuthError as e:
        notify(f"[HCP] Сессия истекла, выгрузка остановлена.\n{e}")
        return 2
    except SchemaError as e:
        notify(f"[HCP] API журнала звонков изменился — проверь скрипт.\n{e}")
        return 3
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass  # соединение с БД уже потеряно — откатывать нечего
        notify(f"[HCP] Непредвиденная ошибка: {e}\nЗапусти снова — продолжит с места остановки.")
        return 1

    db.set_state(conn, "last_run_at", db.now_iso())
    db.set_state(conn, "last_run_status", "ok")
    conn.commit()
    print("Итог по базе:", db.counts(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())

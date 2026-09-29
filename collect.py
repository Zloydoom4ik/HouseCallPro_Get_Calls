# -*- coding: utf-8 -*-
"""
Сборщик звонков Housecall Pro.

Что делает:
  1) Листает журнал звонков (/alpha/voice/call_logs) и пишет метаданные в SQLite.
  2) Скачивает аудиозаписи из Twilio на диск (стерео, если доступно).
  3) Помнит, что уже выгружено, — повторный запуск не создаёт дублей.

Режимы:
  python collect.py                 обычный инкрементальный проход (для крона раз в сутки)
  python collect.py --full          полный проход истории (первый запуск), назад на HCP_BACKFILL_DAYS
  python collect.py --metadata-only только метаданные, без скачивания файлов
  python collect.py --downloads-only только докачать записи из БД (без обхода журнала)
  python collect.py --max-pages N   ограничить число страниц (для проверки)

Коды выхода: 0 — ок; 2 — сессия истекла (нужен новый cookie); 3 — API изменился; 1 — прочее.
"""
import sys
import json
import time
import argparse
import datetime as dt

import config
import db
from hcp_client import (
    make_session, fetch_page, download_recording, AuthError, SchemaError,
)


# ----------------------------- разбор JSON -----------------------------

def parse_dt(value):
    """Терпимо разбираем дату: ISO-строка или epoch. Возвращаем aware datetime или None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
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
        names = [ _first(x, "name", "label", "value") if isinstance(x, dict) else str(x)
                  for x in disp ]
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
        "duration_seconds": _first(call, "duration", "duration_seconds"),
        "disposition": _dispositions(call),
        "has_voicemail": 1 if voicemail else 0,
        "call_created_at": created.isoformat() if created else None,
        "raw_json": json.dumps(call, ensure_ascii=False),
    }


def flatten_recording(rec: dict, call_uuid: str, call_created_iso: str) -> dict:
    return {
        "sid": _first(rec, "sid", "recording_sid"),
        "call_uuid": call_uuid,
        "rec_uuid": _first(rec, "uuid", "id"),
        "status": _first(rec, "status"),
        "duration_seconds": _first(rec, "duration", "duration_seconds"),
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
    except Exception as e:  # уведомление не должно ронять скрипт
        print(f"(не удалось отправить в Telegram: {e})", file=sys.stderr)


# ----------------------------- фаза 1: журнал -----------------------------

def sync_metadata(conn, session, full: bool, max_pages=None) -> int:
    """Обходим страницы журнала, пишем звонки и записи в БД. Возвращаем число новых звонков."""
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    last_run = db.get_state(conn, "last_run_at")
    watermark = parse_dt(last_run)
    if watermark:
        watermark -= dt.timedelta(days=2)  # запас на «поздние» записи и итоги
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=config.HCP_BACKFILL_DAYS)

    total_new = 0
    known_pages_in_row = 0
    page = 1
    total_pages = None

    while True:
        if max_pages and page > max_pages:
            break
        data = fetch_page(session, page)
        total_pages = data.get("total_pages_count")
        calls = data.get("data") or []
        if not calls:
            break

        # сырой JSON на диск — страховка на случай смены схемы
        (config.RAW_DIR / f"page_{page:05d}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

        page_new = 0
        oldest_on_page = None
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
            if cdt and (oldest_on_page is None or cdt < oldest_on_page):
                oldest_on_page = cdt
        conn.commit()
        total_new += page_new

        print(f"  стр.{page}"
              + (f"/{total_pages}" if total_pages else "")
              + f": звонков {len(calls)}, новых {page_new}"
              + (f", самый старый {oldest_on_page.date()}" if oldest_on_page else ""))

        # --- условия остановки ---
        if total_pages and page >= total_pages:
            break
        if full:
            # первый проход: дошли до записей старше окна бэкофилла — дальше только удалённые
            if oldest_on_page and oldest_on_page < cutoff:
                print("  достигли границы бэкфилла — стоп.")
                break
        else:
            # инкремент: остановимся, когда пошли сплошь известные/старые страницы
            if page_new == 0:
                known_pages_in_row += 1
                if known_pages_in_row >= 2:
                    print("  две страницы подряд без новых звонков — стоп.")
                    break
            else:
                known_pages_in_row = 0
            if watermark and oldest_on_page and oldest_on_page < watermark:
                print("  ушли глубже отметки прошлого запуска — стоп.")
                break

        page += 1
        time.sleep(config.HCP_DELAY_LIST)

    return total_new


# ----------------------------- фаза 2: скачивание -----------------------------

def download_pending(conn) -> tuple[int, int]:
    rows = db.get_downloadable(conn)
    print(f"К скачиванию: {len(rows)} записей.")
    ok = fail = 0
    for row in rows:
        sid = row["sid"]
        created = parse_dt(row["call_created_at"]) or dt.datetime.now(dt.timezone.utc)
        dest = (config.REC_DIR / f"{created:%Y/%m/%d}" / f"{sid}.wav")
        if dest.exists() and dest.stat().st_size > 0:
            db.mark_recording(conn, sid, download_status="downloaded",
                              file_path=str(dest), file_bytes=dest.stat().st_size)
            conn.commit()
            ok += 1
            continue
        try:
            size = download_recording(row["wav_url"], dest, stereo=True)
            db.mark_recording(conn, sid, download_status="downloaded",
                              file_path=str(dest), file_bytes=size, last_error=None)
            ok += 1
            print(f"  ✓ {sid}  {size/1024:.0f} КБ")
        except Exception as e:
            db.bump_attempt(conn, sid, str(e))
            fail += 1
            print(f"  ✗ {sid}: {e}")
        conn.commit()
        time.sleep(config.HCP_DELAY_DOWNLOAD)
    return ok, fail


# ----------------------------- main -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Сборщик звонков Housecall Pro")
    ap.add_argument("--full", action="store_true", help="полный проход истории")
    ap.add_argument("--metadata-only", action="store_true", help="без скачивания файлов")
    ap.add_argument("--downloads-only", action="store_true", help="только докачать из БД")
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()

    conn = db.connect()
    db.init(conn)

    # первый запуск (звонков в БД ещё нет) — всегда полный проход
    if not args.downloads_only:
        empty = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
        full = args.full or empty
        if empty:
            print("База пустая — делаю первый полный проход.")

    try:
        if not args.downloads_only:
            new = sync_metadata(conn, make_session(), full=full, max_pages=args.max_pages)
            print(f"Новых звонков за проход: {new}")
        if not args.metadata_only:
            ok, fail = download_pending(conn)
            print(f"Скачано: {ok}, ошибок: {fail}")
    except AuthError as e:
        notify(f"[HCP] Сессия истекла, выгрузка звонков остановлена.\n{e}")
        return 2
    except SchemaError as e:
        notify(f"[HCP] API журнала звонков изменился — проверь скрипт.\n{e}")
        return 3
    except Exception as e:
        notify(f"[HCP] Непредвиденная ошибка: {e}")
        raise

    db.set_state(conn, "last_run_at", db.now_iso())
    db.set_state(conn, "last_run_status", "ok")
    conn.commit()
    print("Итог по базе:", db.counts(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())

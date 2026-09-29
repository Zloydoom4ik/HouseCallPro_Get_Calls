# -*- coding: utf-8 -*-
"""
Хранилище метаданных — SQLite (файл data/hcp_calls.db).
Аудиофайлы в БД НЕ кладём, только пути к ним на диске.

Схема специально написана так, чтобы её один-в-один можно было перенести
в PostgreSQL, когда дойдём до программы-анализатора (см. README, раздел «База данных»).
"""
import sqlite3
import datetime as dt
import config


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
-- Звонки. Ключ — uuid из Housecall Pro (vcl_...).
CREATE TABLE IF NOT EXISTS calls (
    uuid             TEXT PRIMARY KEY,      -- vcl_...
    twilio_call_sid  TEXT,                  -- CA... (тот же звонок в Twilio)
    direction        TEXT,                  -- inbound / outbound
    status           TEXT,
    from_number      TEXT,
    to_number        TEXT,
    customer_uuid    TEXT,                  -- cus_... (связь с клиентом в HCP)
    customer_name    TEXT,
    duration_seconds INTEGER,
    disposition      TEXT,                  -- итог/причина звонка (метки сотрудников)
    has_voicemail    INTEGER DEFAULT 0,
    call_created_at  TEXT,                  -- когда был звонок (ISO)
    raw_json         TEXT,                  -- полный JSON звонка (ничего не теряем)
    first_seen_at    TEXT,                  -- когда мы впервые его выгрузили
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_created  ON calls(call_created_at);
CREATE INDEX IF NOT EXISTS idx_calls_customer ON calls(customer_uuid);

-- Записи разговоров. Ключ — sid из Twilio (RE_...). У одного звонка их может быть несколько.
CREATE TABLE IF NOT EXISTS recordings (
    sid              TEXT PRIMARY KEY,      -- RE...
    call_uuid        TEXT,                  -- vcl_... (FK -> calls.uuid)
    rec_uuid         TEXT,                  -- rec_...
    status           TEXT,                  -- completed / ...
    duration_seconds INTEGER,
    discarded_at     TEXT,                  -- если не NULL — файл в Twilio уже удалён
    wav_url          TEXT,
    mp3_url          TEXT,
    file_path        TEXT,                  -- путь к скачанному файлу на диске
    file_bytes       INTEGER,
    download_status  TEXT DEFAULT 'pending',-- pending/downloaded/failed/discarded
    download_attempts INTEGER DEFAULT 0,
    last_error       TEXT,
    call_created_at  TEXT,                  -- дата звонка (для раскладки по папкам)
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rec_status ON recordings(download_status);
CREATE INDEX IF NOT EXISTS idx_rec_call   ON recordings(call_uuid);

-- Служебное состояние (когда был последний запуск и чем закончился).
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def call_exists(conn, uuid: str) -> bool:
    return conn.execute("SELECT 1 FROM calls WHERE uuid=?", (uuid,)).fetchone() is not None


def upsert_call(conn, c: dict) -> None:
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO calls (uuid, twilio_call_sid, direction, status, from_number,
            to_number, customer_uuid, customer_name, duration_seconds, disposition,
            has_voicemail, call_created_at, raw_json, first_seen_at, updated_at)
        VALUES (:uuid, :twilio_call_sid, :direction, :status, :from_number,
            :to_number, :customer_uuid, :customer_name, :duration_seconds, :disposition,
            :has_voicemail, :call_created_at, :raw_json, :ts, :ts)
        ON CONFLICT(uuid) DO UPDATE SET
            twilio_call_sid=excluded.twilio_call_sid,
            direction=excluded.direction,
            status=excluded.status,
            from_number=excluded.from_number,
            to_number=excluded.to_number,
            customer_uuid=excluded.customer_uuid,
            customer_name=excluded.customer_name,
            duration_seconds=excluded.duration_seconds,
            disposition=excluded.disposition,
            has_voicemail=excluded.has_voicemail,
            call_created_at=excluded.call_created_at,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        {**c, "ts": ts},
    )


def upsert_recording(conn, r: dict) -> None:
    """Пишем/обновляем метаданные записи, НЕ трогая уже проставленный статус скачивания."""
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO recordings (sid, call_uuid, rec_uuid, status, duration_seconds,
            discarded_at, wav_url, mp3_url, call_created_at, download_status, updated_at)
        VALUES (:sid, :call_uuid, :rec_uuid, :status, :duration_seconds,
            :discarded_at, :wav_url, :mp3_url, :call_created_at, :init_status, :ts)
        ON CONFLICT(sid) DO UPDATE SET
            call_uuid=excluded.call_uuid,
            rec_uuid=excluded.rec_uuid,
            status=excluded.status,
            duration_seconds=excluded.duration_seconds,
            discarded_at=excluded.discarded_at,
            wav_url=excluded.wav_url,
            mp3_url=excluded.mp3_url,
            call_created_at=excluded.call_created_at,
            updated_at=excluded.updated_at,
            -- если запись в Twilio удалили — фиксируем это
            download_status=CASE
                WHEN excluded.discarded_at IS NOT NULL
                     AND recordings.download_status <> 'downloaded'
                THEN 'discarded' ELSE recordings.download_status END
        """,
        {**r, "init_status": "discarded" if r.get("discarded_at") else "pending", "ts": ts},
    )


def get_downloadable(conn):
    """Записи, которые ещё надо скачать: живые, завершённые, не скачанные."""
    return conn.execute(
        """
        SELECT * FROM recordings
        WHERE discarded_at IS NULL
          AND status = 'completed'
          AND download_status IN ('pending', 'failed')
        ORDER BY call_created_at DESC
        """
    ).fetchall()


def mark_recording(conn, sid: str, **fields) -> None:
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=:{k}" for k in fields)
    conn.execute(f"UPDATE recordings SET {cols} WHERE sid=:sid", {**fields, "sid": sid})


def bump_attempt(conn, sid: str, error: str) -> None:
    conn.execute(
        "UPDATE recordings SET download_attempts=download_attempts+1, "
        "download_status='failed', last_error=:e, updated_at=:t WHERE sid=:sid",
        {"e": error[:500], "t": now_iso(), "sid": sid},
    )


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_state(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def counts(conn) -> dict:
    def one(q):
        return conn.execute(q).fetchone()[0]
    return {
        "calls": one("SELECT COUNT(*) FROM calls"),
        "recordings": one("SELECT COUNT(*) FROM recordings"),
        "downloaded": one("SELECT COUNT(*) FROM recordings WHERE download_status='downloaded'"),
        "pending": one("SELECT COUNT(*) FROM recordings WHERE download_status IN ('pending','failed')"),
        "discarded": one("SELECT COUNT(*) FROM recordings WHERE download_status='discarded'"),
    }

# -*- coding: utf-8 -*-
"""
Хранилище метаданных — PostgreSQL (контейнер из docker-compose.yml), драйвер psycopg 3.
Аудиофайлы в БД не кладём — только путь к ним на диске (recordings.file_path).

Связь БД <-> диск:
  recordings.sid        = имя файла (RE….wav)
  recordings.file_path  = полный путь к файлу
  recordings.call_uuid  = какому звонку (calls.uuid) принадлежит запись
"""
import datetime as dt

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import config


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


SCHEMA = [
    # Звонки. Ключ — uuid из Housecall Pro (vcl_...).
    """
    CREATE TABLE IF NOT EXISTS calls (
        uuid             TEXT PRIMARY KEY,       -- vcl_...
        twilio_call_sid  TEXT,                   -- CA... (тот же звонок в Twilio)
        direction        TEXT,                   -- inbound / outbound
        status           TEXT,
        from_number      TEXT,
        to_number        TEXT,
        customer_uuid    TEXT,                   -- cus_... (клиент в HCP)
        customer_name    TEXT,
        operator_id      TEXT,                   -- pro_... (кто обработал звонок)
        operator_name    TEXT,                   -- напр. "Max V."
        duration_seconds INTEGER,
        disposition      TEXT,                   -- итог/причина звонка (метки сотрудников)
        has_voicemail    BOOLEAN DEFAULT FALSE,
        call_created_at  TIMESTAMPTZ,            -- когда был звонок
        raw_json         JSONB,                  -- полный исходный JSON звонка
        first_seen_at    TIMESTAMPTZ,
        updated_at       TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_calls_created  ON calls(call_created_at)",
    "CREATE INDEX IF NOT EXISTS idx_calls_customer ON calls(customer_uuid)",
    # для уже существующей таблицы (добавляем колонки оператора, если их нет)
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS operator_id   TEXT",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS operator_name TEXT",
    "CREATE INDEX IF NOT EXISTS idx_calls_operator ON calls(operator_id)",
    # Записи разговоров. Ключ — sid из Twilio (RE...). У звонка их может быть несколько.
    """
    CREATE TABLE IF NOT EXISTS recordings (
        sid               TEXT PRIMARY KEY,      -- RE...  (= имя файла на диске)
        call_uuid         TEXT REFERENCES calls(uuid),
        rec_uuid          TEXT,                  -- rec_...
        status            TEXT,                  -- completed / ...
        duration_seconds  INTEGER,
        discarded_at      TEXT,                  -- не NULL => файл в Twilio удалён
        wav_url           TEXT,
        mp3_url           TEXT,
        file_path         TEXT,                  -- полный путь к скачанному файлу
        file_bytes        BIGINT,
        download_status   TEXT DEFAULT 'pending',-- pending/downloaded/failed/discarded
        download_attempts INTEGER DEFAULT 0,
        last_error        TEXT,
        call_created_at   TIMESTAMPTZ,           -- дата звонка (раскладка по папкам)
        updated_at        TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rec_status ON recordings(download_status)",
    "CREATE INDEX IF NOT EXISTS idx_rec_call   ON recordings(call_uuid)",
    # Служебное: когда был последний запуск.
    """
    CREATE TABLE IF NOT EXISTS sync_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
]


def connect():
    try:
        return psycopg.connect(config.PG_DSN, row_factory=dict_row)
    except psycopg.OperationalError as e:
        raise SystemExit(
            f"Postgres недоступен ({config.PG_HOST}:{config.PG_PORT}). "
            f"Подними базу: docker compose up -d\n{e}"
        )


def init(conn) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.commit()


def call_exists(conn, uuid: str) -> bool:
    return conn.execute("SELECT 1 FROM calls WHERE uuid=%s", (uuid,)).fetchone() is not None


def calls_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]


def upsert_call(conn, c: dict) -> None:
    p = {**c, "raw_json": Jsonb(c["raw_json"]), "ts": now()}
    conn.execute(
        """
        INSERT INTO calls (uuid, twilio_call_sid, direction, status, from_number, to_number,
            customer_uuid, customer_name, operator_id, operator_name, duration_seconds,
            disposition, has_voicemail, call_created_at, raw_json, first_seen_at, updated_at)
        VALUES (%(uuid)s, %(twilio_call_sid)s, %(direction)s, %(status)s, %(from_number)s,
            %(to_number)s, %(customer_uuid)s, %(customer_name)s, %(operator_id)s,
            %(operator_name)s, %(duration_seconds)s,
            %(disposition)s, %(has_voicemail)s, %(call_created_at)s::timestamptz,
            %(raw_json)s, %(ts)s, %(ts)s)
        ON CONFLICT (uuid) DO UPDATE SET
            twilio_call_sid  = EXCLUDED.twilio_call_sid,
            direction        = EXCLUDED.direction,
            status           = EXCLUDED.status,
            from_number      = EXCLUDED.from_number,
            to_number        = EXCLUDED.to_number,
            customer_uuid    = EXCLUDED.customer_uuid,
            customer_name    = EXCLUDED.customer_name,
            operator_id      = EXCLUDED.operator_id,
            operator_name    = EXCLUDED.operator_name,
            duration_seconds = EXCLUDED.duration_seconds,
            disposition      = EXCLUDED.disposition,
            has_voicemail    = EXCLUDED.has_voicemail,
            call_created_at  = EXCLUDED.call_created_at,
            raw_json         = EXCLUDED.raw_json,
            updated_at       = EXCLUDED.updated_at
        """,
        p,
    )


def upsert_recording(conn, r: dict) -> None:
    """Метаданные записи; уже проставленный статус скачивания не трогаем."""
    p = {**r, "init_status": "discarded" if r.get("discarded_at") else "pending", "ts": now()}
    conn.execute(
        """
        INSERT INTO recordings (sid, call_uuid, rec_uuid, status, duration_seconds,
            discarded_at, wav_url, mp3_url, call_created_at, download_status, updated_at)
        VALUES (%(sid)s, %(call_uuid)s, %(rec_uuid)s, %(status)s, %(duration_seconds)s,
            %(discarded_at)s, %(wav_url)s, %(mp3_url)s, %(call_created_at)s::timestamptz,
            %(init_status)s, %(ts)s)
        ON CONFLICT (sid) DO UPDATE SET
            call_uuid        = EXCLUDED.call_uuid,
            rec_uuid         = EXCLUDED.rec_uuid,
            status           = EXCLUDED.status,
            duration_seconds = EXCLUDED.duration_seconds,
            discarded_at     = EXCLUDED.discarded_at,
            wav_url          = EXCLUDED.wav_url,
            mp3_url          = EXCLUDED.mp3_url,
            call_created_at  = EXCLUDED.call_created_at,
            updated_at       = EXCLUDED.updated_at,
            download_status  = CASE
                WHEN EXCLUDED.discarded_at IS NOT NULL
                     AND recordings.download_status <> 'downloaded'
                THEN 'discarded' ELSE recordings.download_status END
        """,
        p,
    )


def get_downloadable(conn, since=None):
    """Записи, которые ещё надо скачать: живые, завершённые, не скачанные.
    since — минимальная дата звонка (None = без ограничения по дате)."""
    return conn.execute(
        """
        SELECT * FROM recordings
        WHERE discarded_at IS NULL
          AND status = 'completed'
          AND download_status IN ('pending', 'failed')
          AND (%(since)s IS NULL OR call_created_at >= %(since)s)
        ORDER BY call_created_at DESC NULLS LAST
        """,
        {"since": since},
    ).fetchall()


def mark_recording(conn, sid: str, **fields) -> None:
    fields["updated_at"] = now()
    sets = ", ".join(f"{k}=%({k})s" for k in fields)
    conn.execute(f"UPDATE recordings SET {sets} WHERE sid=%(sid)s", {**fields, "sid": sid})


def bump_attempt(conn, sid: str, error: str) -> None:
    conn.execute(
        "UPDATE recordings SET download_attempts=download_attempts+1, "
        "download_status='failed', last_error=%(e)s, updated_at=%(t)s WHERE sid=%(sid)s",
        {"e": error[:500], "t": now(), "sid": sid},
    )


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        (key, value),
    )


def get_state(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM sync_state WHERE key=%s", (key,)).fetchone()
    return row["value"] if row else default


def counts(conn) -> dict:
    def one(q):
        return conn.execute(q).fetchone()["n"]
    return {
        "calls": one("SELECT COUNT(*) AS n FROM calls"),
        "recordings": one("SELECT COUNT(*) AS n FROM recordings"),
        "downloaded": one("SELECT COUNT(*) AS n FROM recordings WHERE download_status='downloaded'"),
        "pending": one("SELECT COUNT(*) AS n FROM recordings WHERE download_status IN ('pending','failed')"),
        "discarded": one("SELECT COUNT(*) AS n FROM recordings WHERE download_status='discarded'"),
    }

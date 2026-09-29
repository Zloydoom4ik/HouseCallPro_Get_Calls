-- Целевая схема для PostgreSQL — понадобится, когда дойдём до программы-анализатора.
-- Совпадает 1-в-1 с SQLite-схемой сборщика (db.py), плюс таблицы под результаты анализа.

CREATE TABLE IF NOT EXISTS calls (
    uuid             TEXT PRIMARY KEY,
    twilio_call_sid  TEXT,
    direction        TEXT,
    status           TEXT,
    from_number      TEXT,
    to_number        TEXT,
    customer_uuid    TEXT,
    customer_name    TEXT,
    duration_seconds INTEGER,
    disposition      TEXT,
    has_voicemail    BOOLEAN DEFAULT FALSE,
    call_created_at  TIMESTAMPTZ,
    raw_json         JSONB,
    first_seen_at    TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_calls_created  ON calls(call_created_at);
CREATE INDEX IF NOT EXISTS idx_calls_customer ON calls(customer_uuid);

CREATE TABLE IF NOT EXISTS recordings (
    sid               TEXT PRIMARY KEY,
    call_uuid         TEXT REFERENCES calls(uuid),
    rec_uuid          TEXT,
    status            TEXT,
    duration_seconds  INTEGER,
    discarded_at      TIMESTAMPTZ,
    wav_url           TEXT,
    mp3_url           TEXT,
    file_path         TEXT,
    file_bytes        BIGINT,
    download_status   TEXT DEFAULT 'pending',
    download_attempts INTEGER DEFAULT 0,
    last_error        TEXT,
    call_created_at   TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_rec_status ON recordings(download_status);

-- Результаты анализа (заполняет вторая программа): расшифровка + выводы модели.
CREATE TABLE IF NOT EXISTS transcripts (
    recording_sid TEXT PRIMARY KEY REFERENCES recordings(sid),
    language      TEXT,
    engine        TEXT,               -- whisper / deepgram / ...
    text          TEXT,               -- полная расшифровка
    segments      JSONB,              -- реплики по говорящим и таймкодам
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS call_analysis (
    call_uuid   TEXT PRIMARY KEY REFERENCES calls(uuid),
    summary     TEXT,                 -- краткое содержание
    outcome     TEXT,                 -- записались / отказ / перезвон / ...
    sentiment   TEXT,
    topics      JSONB,
    flags       JSONB,                -- грубость, пропущенный апселл и т.п.
    model       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

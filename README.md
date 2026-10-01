# HCP Calls — export calls and recordings from Housecall Pro

Pulls the Housecall Pro call log (internal API `/alpha/voice/call_logs`, via the session cookie),
writes metadata to **PostgreSQL**, and downloads audio recordings from Twilio into **files on disk**.
The whole app is packaged in Docker: the host only needs Docker.

## Files
- `collect.py` — collector: walk the journal + download recordings
- `probe.py` — recon: print the structure of one call, writes nothing
- `hcp_client.py` — HTTP to Housecall Pro / Twilio (cookie, retries, session refresh)
- `db.py` — Postgres: schema, read/write
- `config.py` — settings (from `.env` / environment variables)
- `Dockerfile` — collector image;  `docker-compose.yml` — database + collector + scheduler
- `.env` — the single place for settings (port, DB login/password, delays). Create from `.env.example`
- `secrets/hcp_cookie.txt` — session cookie (create manually; not committed to git)
- `run_daily.bat` — launcher for Windows Task Scheduler
- `data/recordings/YYYY/MM/DD/RE….wav` — downloaded audio files

## Run (Docker)
```
cd "C:\Dev\retail компания\hcp_calls_scripts"
cp .env.example .env            # once; optionally adjust password/port
docker compose up -d            # Postgres (container hcp_pg, port from .env, 5439 by default)
```
Cookie (once): F12 → Application → Cookies → pro.housecallpro.com →
`_housecall-web_session_with_domain` → paste the value as a single line into `secrets\hcp_cookie.txt`.
```
docker compose run --rm collector --full      # first full pass (HCP_BACKFILL_DAYS)
docker compose run --rm collector             # one-off incremental pass (new only)
docker compose --profile cron up -d           # scheduler: runs a pass once a day on its own
docker compose logs -f hcp_scheduler          # watch what the scheduler does
docker compose down                           # stop everything (data is kept)
```
Collector flags: `--metadata-only` (no files), `--downloads-only` (only finish downloads), `--max-pages N`.
After changing the code, rebuild the image: `docker compose build`.

## Autorun: batch file + Windows Task Scheduler
`run_daily.bat` is the autorun wrapper. Inside:
```bat
cd /d "%~dp0"                       REM go to the project folder
call venv\Scripts\activate.bat      REM activate venv
python collect.py >> data\collect.log 2>&1   REM incremental; all output into the log file
```

Put it on a schedule (once a day), without the Docker scheduler:
1. Open Windows "Task Scheduler" → "Create Basic Task".
2. Trigger: daily, a time (e.g. at night).
3. Action: "Start a program" → point to the path of `run_daily.bat`.
4. Done — Windows will run the collection once a day by itself, the log accumulates in `data\collect.log`.

This replaces the Docker `scheduler` service: whoever prefers the native Scheduler uses the batch file,
whoever prefers a container uses `docker compose --profile cron up -d`. Do only one of them.

## How it works
1. Reads the cookie → walks `call_logs` in pages of 80, new calls → table `calls`,
   recordings from calls → table `recordings` (`download_status = pending`).
2. `--full` goes back as far as `HCP_BACKFILL_DAYS` (window in days from `.env`; `0` = all history).
   Incremental — until two pages in a row with no new calls, or down to the last run's date.
   The same window also limits audio downloading (files older than the window are not downloaded).
3. Then it downloads all `pending` recordings from Twilio: `wav_url?RequestedChannels=2` (stereo:
   agent and customer on separate channels) → `data/recordings/…/RE….wav`, in the DB `file_path`,
   `download_status = downloaded`.
4. After each response the cookie is refreshed in the file — the session (14 days) renews itself.

## Concurrency and downloading
Work runs in two phases, both multithreaded. Network work — in worker threads, DB writes — only
in the main thread (that keeps psycopg safe, no races). Thread counts are set in `.env`.

**Phase 1 — journal (metadata), `HCP_LIST_WORKERS`:**
- pages are fetched in parallel batches (HCP_LIST_WORKERS);
- once a batch arrives → the main thread writes calls/recordings to the DB in order → take the next batch;
- a `HCP_DELAY_LIST` (sec) pause between batches.

**Phase 2 — audio download, `HCP_DOWNLOAD_WORKERS`:**
- all recordings from the queue (`download_status in (pending, failed)` within the window) go into a pool;
- threads download **one file each**; a freed thread takes the next one from the queue
  (not fixed "thread N downloads file N" — whoever is free takes it);
- the file is fetched from Twilio **without the cookie** (so the session isn't exposed to a third party),
  in stereo `?RequestedChannels=2` (agent and customer on separate channels), on 400 — fall back to mono;
- the downloaded file → `data/recordings/YYYY/MM/DD/RE….wav`, the DB gets `file_path` and `downloaded`;
- if the file is already on disk — it is not downloaded again (checked before downloading);
- progress is printed every 25 finished files: `25/6563 downloaded:25 errors:0`.

**Reliability:** on `429/5xx`/network drop each thread waits with increasing backoff and retries
(up to 5 times); duplicates are impossible (`uuid`/`sid` are keys + `ON CONFLICT DO UPDATE`); if interrupted
midway — just run again, it continues.

## DB <-> disk
- `calls` (key `vcl_…`) — the call, customer `cus_…`, disposition, `raw_json` (the whole original JSON)
- `recordings` (key `RE…`) — `call_uuid` → calls, `wav_url`, **`file_path`**,
  `download_status`: pending / downloaded / failed / discarded
- File name = the recording's `sid`. Before downloading, the file's presence is checked — no re-download.
- Re-running does not duplicate: `ON CONFLICT DO UPDATE`.
- Inside the container `file_path` = `/app/data/...`, on the host it is `./data/...`.

## If something breaks
- exit code **2** — session expired → copy the cookie again
- exit code **3** — API changed → send the output
- 429/5xx — it waits and retries on its own; deleted recordings are marked `discarded`
- "Postgres unavailable" → `docker compose up -d`

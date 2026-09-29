# HCP Calls — выгрузка звонков и записей из Housecall Pro

Забирает журнал звонков Housecall Pro (внутренний API `/alpha/voice/call_logs`, по cookie сессии),
метаданные пишет в **PostgreSQL (Docker)**, аудиозаписи качает из Twilio в **файлы на диске**.

## Файлы
- `collect.py` — сборщик: обход журнала + скачивание записей
- `probe.py` — разведка: показать структуру одного звонка, ничего не пишет
- `hcp_client.py` — HTTP к Housecall Pro / Twilio (cookie, повторы, продление сессии)
- `db.py` — Postgres: схема, запись/чтение
- `config.py` — настройки (читает `.env`)
- `docker-compose.yml` — Postgres, контейнер `hcp_pg`, порт **5439**
- `run_daily.bat` — для Планировщика задач Windows
- `secrets/hcp_cookie.txt` — cookie сессии (создать вручную; в git не попадает)
- `data/recordings/ГГГГ/ММ/ДД/RE….wav` — аудио;  `data/raw/` — сырой JSON страниц

## Запуск
```
cd "C:\Dev\retail компания\hcp_calls_scripts"
docker compose up -d                 # база
venv\Scripts\activate
pip install -r requirements.txt      # requests + psycopg
copy .env.example .env
```
Cookie: F12 → Application → Cookies → pro.housecallpro.com → `_housecall-web_session_with_domain`
→ значение одной строкой в `secrets\hcp_cookie.txt`.
```
python probe.py               # доступ есть? какие поля в звонке?
python collect.py --full      # первый полный проход: история ~7 мес + все живые записи
python collect.py             # дальше раз в сутки (run_daily.bat -> Планировщик задач)
```
Режимы: `--metadata-only` (без файлов), `--downloads-only` (только докачать), `--max-pages N` (проверка).

## БД <-> диск
- `calls` (ключ `vcl_…`) — звонок, клиент `cus_…`, итог, `raw_json` (весь исходный JSON)
- `recordings` (ключ `RE…`) — `call_uuid` -> calls, `wav_url`, **`file_path`** (путь к файлу),
  `download_status`: pending / downloaded / failed / discarded (Twilio удалил, старше 6 мес)
- Имя файла = `sid` записи. Перед скачиванием проверяется наличие файла на диске — повторно не качает.
- Повторный запуск не двоит: `ON CONFLICT DO UPDATE`.

## Если сломалось
- код выхода **2** — сессия истекла -> заново скопировать cookie
- код **3** — API изменился -> прислать вывод
- 429/5xx — сам ждёт и повторяет; удалённые записи помечаются `discarded`
- Postgres недоступен -> `docker compose up -d`
# HCP Calls — выгрузка звонков и записей из Housecall Pro

Забирает журнал звонков Housecall Pro (внутренний API `/alpha/voice/call_logs`, по cookie сессии),
метаданные пишет в **PostgreSQL**, аудиозаписи качает из Twilio в **файлы на диске**.
Всё приложение упаковано в Docker: на хосте нужен только Docker.

## Файлы
- `collect.py` — сборщик: обход журнала + скачивание записей
- `probe.py` — разведка: показать структуру одного звонка, ничего не пишет
- `hcp_client.py` — HTTP к Housecall Pro / Twilio (cookie, повторы, продление сессии)
- `db.py` — Postgres: схема, запись/чтение
- `config.py` — настройки (из `.env` / переменных окружения)
- `Dockerfile` — образ сборщика;  `docker-compose.yml` — база + сборщик + планировщик
- `.env` — единственное место с настройками (порт, логин/пароль БД, паузы). Создать из `.env.example`
- `secrets/hcp_cookie.txt` — cookie сессии (создать вручную; в git не попадает)
- `data/recordings/ГГГГ/ММ/ДД/RE….wav` — аудио;  `data/raw/` — сырой JSON страниц (страховка)

## Запуск (Docker)
```
cd "C:\Dev\retail компания\hcp_calls_scripts"
cp .env.example .env            # один раз; при желании поправить пароль/порт
docker compose up -d            # Postgres (контейнер hcp_pg, порт из .env, по умолчанию 5439)
```
Cookie (один раз): F12 → Application → Cookies → pro.housecallpro.com →
`_housecall-web_session_with_domain` → значение одной строкой в `secrets\hcp_cookie.txt`.
```
docker compose run --rm collector --full      # первый полный проход: ~7 мес истории + все живые записи
docker compose run --rm collector             # разовый инкрементальный проход (только новое)
docker compose --profile cron up -d           # планировщик: сам делает проход раз в сутки
docker compose logs -f hcp_scheduler          # смотреть, что делает планировщик
docker compose down                           # остановить всё (данные остаются)
```
Флаги сборщика: `--metadata-only` (без файлов), `--downloads-only` (только докачать), `--max-pages N`.
После правки кода пересобрать образ: `docker compose build`.

Локально без Docker тоже работает: `venv\Scripts\activate`, `pip install -r requirements.txt`,
`python collect.py --full` (база берётся с `localhost:PG_PORT`).

## Как работает
1. Читает cookie → листает `call_logs` страницами по 80, новые звонки → таблица `calls`,
   записи из звонков → таблица `recordings` (`download_status = pending`).
2. `--full` идёт вглубь до `HCP_BACKFILL_DAYS` (210 дней; старше записи уже удалены HCP).
   Инкремент — до двух страниц подряд без новых звонков или до даты прошлого запуска.
3. Затем качает все `pending` записи у Twilio: `wav_url?RequestedChannels=2` (стерео:
   оператор и клиент на разных каналах) → `data/recordings/…/RE….wav`, в БД `file_path`,
   `download_status = downloaded`.
4. Cookie после каждого ответа обновляется в файле — сессия (14 дней) продлевается сама.

## БД <-> диск
- `calls` (ключ `vcl_…`) — звонок, клиент `cus_…`, итог, `raw_json` (весь исходный JSON)
- `recordings` (ключ `RE…`) — `call_uuid` → calls, `wav_url`, **`file_path`**,
  `download_status`: pending / downloaded / failed / discarded (Twilio удалил, старше 6 мес)
- Имя файла = `sid` записи. Перед скачиванием проверяется наличие файла — повторно не качает.
- Повторный запуск не двоит: `ON CONFLICT DO UPDATE`.
- Внутри контейнера `file_path` = `/app/data/...`, на хосте это `./data/...`.

## Если сломалось
- код выхода **2** — сессия истекла → заново скопировать cookie
- код **3** — API изменился → прислать вывод
- 429/5xx — сам ждёт и повторяет; удалённые записи помечаются `discarded`
- «Postgres недоступен» → `docker compose up -d`
- Уведомления об ошибках в Telegram: заполнить `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в `.env`

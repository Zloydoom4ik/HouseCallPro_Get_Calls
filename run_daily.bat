@echo off
REM Ежедневный запуск сборщика. Поставь в Планировщик задач Windows (см. README).
cd /d "%~dp0"
call venv\Scripts\activate.bat
python collect.py >> data\collect.log 2>&1

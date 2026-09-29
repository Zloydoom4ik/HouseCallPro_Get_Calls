FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# аргументы docker compose run передаются напрямую в collect.py (--full, --downloads-only, ...)
ENTRYPOINT ["python", "collect.py"]

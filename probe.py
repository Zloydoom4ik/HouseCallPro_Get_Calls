# -*- coding: utf-8 -*-
"""
Разведка: тянет ОДНУ страницу журнала и показывает структуру ответа и одного звонка.
Ничего не пишет в БД и не качает файлы. Нужен, чтобы сверить реальные имена полей
(в collect.py часть имён угадана и подстрахована — здесь видно, как оно на самом деле).

Запуск:  python probe.py
"""
import json
import config
from hcp_client import make_session, fetch_page


def main():
    data = fetch_page(make_session(), 1)
    meta = {k: v for k, v in data.items() if k != "data"}
    print("Верхний уровень (без data):")
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    calls = data.get("data") or []
    print(f"\nЗвонков на странице: {len(calls)}")
    if not calls:
        return
    c = calls[0]
    print("\nКлючи звонка:", list(c.keys()))

    # прячем номера телефонов, чтобы можно было спокойно показать вывод
    red = dict(c)
    for k in ("from", "to"):
        if red.get(k):
            red[k] = "***"
    print("\nПервый звонок целиком (номера скрыты):")
    print(json.dumps(red, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    main()

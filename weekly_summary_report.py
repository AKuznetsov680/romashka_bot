#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Отчёт "Итоги недели" (TravelLine Partner API) -> Telegram.

Отдельный, компактный отчёт: только сравнение последней полной недели
(понедельник-воскресенье) год к году по загрузке, плюс детализация по
основным категориям размещения (Коттедж, Барнхаус, Дом, Аппартаменты) -
номероночи, % загрузки и выручка каждой категории, год к году.

Переиспользует уже проверенные функции из travelline_daily_report.py -
дублирования логики нет.

Обязательные переменные окружения:
  TL_CLIENT_ID, TL_CLIENT_SECRET, TL_PROPERTY_ID
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import sys
from collections import defaultdict
from datetime import date, timedelta

from travelline_daily_report import (
    get_access_token,
    get_rooms,
    get_room_type_meta,
    aggregate_week,
    get_room_type_breakdown,
    get_last_full_week,
    build_yoy_section,
    build_room_type_table,
    send_telegram_message,
)


def main():
    client_id = os.environ.get("TL_CLIENT_ID")
    client_secret = os.environ.get("TL_CLIENT_SECRET")
    property_id = os.environ.get("TL_PROPERTY_ID")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    missing = [name for name, val in [
        ("TL_CLIENT_ID", client_id),
        ("TL_CLIENT_SECRET", client_secret),
        ("TL_PROPERTY_ID", property_id),
    ] if not val]
    if missing:
        print(f"[!] Не заданы обязательные переменные окружения: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    today = date.today()

    try:
        token = get_access_token(client_id, client_secret)
    except Exception as e:
        print(f"[!] Ошибка авторизации в TravelLine: {e}", file=sys.stderr)
        sys.exit(1)

    # --- справочники (не критичны: при сбое просто теряем % и названия) ---
    try:
        rooms = get_rooms(token, property_id)
        total_rooms = len(rooms) or None
        room_id_to_type = {r.get("id"): r.get("roomTypeId") for r in rooms}
        rooms_per_type = defaultdict(int)
        for r in rooms:
            rooms_per_type[r.get("roomTypeId")] += 1
    except Exception as e:
        print(f"[!] Не удалось получить справочник номеров: {e}", file=sys.stderr)
        total_rooms = None
        room_id_to_type = {}
        rooms_per_type = {}

    try:
        room_type_meta = get_room_type_meta(token, property_id)
    except Exception as e:
        print(f"[!] Не удалось получить категории номеров: {e}", file=sys.stderr)
        room_type_meta = {}

    # --- последняя полная неделя vs та же неделя год назад ---
    this_week = get_last_full_week(today)
    last_year_week = (this_week[0] - timedelta(days=364), this_week[1] - timedelta(days=364))

    try:
        this_week_sums, this_warnings = aggregate_week(token, property_id, *this_week)
    except Exception as e:
        this_week_sums = defaultdict(int)
        this_warnings = [{"code": "FetchError", "message": str(e)}]

    try:
        last_year_sums, last_warnings = aggregate_week(token, property_id, *last_year_week)
    except Exception as e:
        last_year_sums = defaultdict(int)
        last_warnings = [{"code": "FetchError", "message": str(e)}]

    yoy_section = build_yoy_section(
        this_week, this_week_sums, last_year_week, last_year_sums,
        this_warnings, last_warnings, total_rooms
    )

    # --- детализация по категориям (Коттедж/Барнхаус/Дом/Аппартаменты) ---
    try:
        this_room_breakdown, was_capped_this = get_room_type_breakdown(
            token, property_id, room_id_to_type, this_week[0], this_week[1]
        )
    except Exception as e:
        this_room_breakdown, was_capped_this = {}, False
        print(f"[!] Не удалось получить детализацию по категориям (тек. неделя): {e}", file=sys.stderr)

    try:
        last_room_breakdown, was_capped_last = get_room_type_breakdown(
            token, property_id, room_id_to_type, last_year_week[0], last_year_week[1]
        )
    except Exception as e:
        last_room_breakdown, was_capped_last = {}, False
        print(f"[!] Не удалось получить детализацию по категориям (год назад): {e}", file=sys.stderr)

    room_type_section = build_room_type_table(
        this_room_breakdown, last_room_breakdown, room_type_meta, rooms_per_type,
        this_week, last_year_week, was_capped_this, was_capped_last
    )

    header = f"<b>🗓 Итоги недели: {this_week[0].isoformat()}—{this_week[1].isoformat()}</b>"
    full_report = "\n\n".join([header, yoy_section, room_type_section])

    print(full_report.replace("<b>", "").replace("</b>", "").replace("<u>", "").replace("</u>", ""))

    if not tg_token or not tg_chat_id:
        print("\n[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы - "
              "сообщение не отправлено, только выведено выше.", file=sys.stderr)
        return

    send_telegram_message(tg_token, tg_chat_id, full_report)
    print("\n[OK] Отправлено в Telegram.")


if __name__ == "__main__":
    main()

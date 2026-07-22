#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ежедневный отчёт по загрузке отеля (TravelLine Partner API) -> Telegram.

Источник данных: TravelLine Partner API, PMS Analytics API
  GET /v1/properties/{propertyId}/daily-occupancy
  https://www.travelline.ru/dev-portal/docs/api/#tag/PropertyAnalytics

Авторизация: OAuth 2.0, Client Credentials Flow.
  Токен выдаётся на 15 минут, обновление (refresh) не поддерживается -
  каждый запуск скрипта получает новый токен.

Обязательные переменные окружения:
  TL_CLIENT_ID        - client_id подключения TravelLine Partner API
  TL_CLIENT_SECRET     - client_secret подключения TravelLine Partner API
  TL_PROPERTY_ID       - ID средства размещения (отеля) в TravelLine
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Как получить TL_CLIENT_ID / TL_CLIENT_SECRET / TL_PROPERTY_ID - см. README.md.
"""

import os
import sys
import time
from datetime import datetime, timedelta

import requests

AUTH_URL = "https://partner.tlintegration.com/auth/token"
ANALYTICS_BASE = "https://partner.tlintegration.com/api/pms-analytics"
PMS_BASE = "https://partner.tlintegration.com/api/pms"
REQUEST_TIMEOUT = 20


# --------------------------------------------------------------------------
# Авторизация
# --------------------------------------------------------------------------

def get_access_token(client_id, client_secret):
    """Client Credentials Flow. Токен живёт 15 минут, без refresh."""
    resp = requests.post(
        AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Не удалось получить access_token: {data}")
    return token


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# TravelLine: загрузка (occupancy)
# --------------------------------------------------------------------------

def get_daily_occupancy(token, property_id, start_date, end_date):
    """
    GET /v1/properties/{propertyId}/daily-occupancy
    startStayDate / endStayDate - ISO-8601 YYYY-MM-DD, диапазон максимум 31 день.
    """
    url = f"{ANALYTICS_BASE}/v1/properties/{property_id}/daily-occupancy"
    resp = requests.get(
        url,
        params={"startStayDate": start_date, "endStayDate": end_date},
        headers=auth_headers(token),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_total_rooms_count(token, property_id):
    """
    Общее количество номеров отеля (для расчёта % загрузки).
    GET /v2/properties/{propertyId}/rooms - постраничный список.
    Если получить не удалось - возвращает None (в отчёте просто не будет %).
    """
    url = f"{PMS_BASE}/v2/properties/{property_id}/rooms"
    total = 0
    page_token = None
    for _ in range(50):  # защита от бесконечной пагинации
        params = {"maxPageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, params=params, headers=auth_headers(token),
                             timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        rooms = data.get("rooms", [])
        total += len(rooms)
        if not data.get("hasNextPage"):
            break
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return total or None


# --------------------------------------------------------------------------
# Формирование отчёта
# --------------------------------------------------------------------------

def format_money(value, currency=""):
    if value is None:
        return "н/д"
    try:
        return f"{float(value):,.0f} {currency}".replace(",", " ").strip()
    except (ValueError, TypeError):
        return str(value)


def build_report_section(occupancy_data, total_rooms, report_date):
    lines = [f"<b>🏨 Загрузка отеля — {report_date}</b>"]

    days = occupancy_data.get("dailyOccupancies") or occupancy_data.get("days") or []
    warnings = occupancy_data.get("warnings") or []

    if warnings:
        for w in warnings:
            code = w.get("code", "")
            msg = w.get("message", "")
            lines.append(f"⚠️ {code}: {msg}")

    if not days:
        lines.append("Нет данных за указанный период.")
        return "\n".join(lines)

    # Обычно данные за один день (startStayDate == endStayDate == вчера)
    for day in days:
        date = day.get("date", report_date)
        occupied = day.get("occupancyRoomCount")
        complimentary = day.get("complimentaryOccupancyRoomCount")
        closed = day.get("closedRoomCount")
        revenue = day.get("revenue")
        room_revenue = day.get("roomRevenue")
        meal_revenue = day.get("mealRevenue")
        arrivals = day.get("arrivalCount")
        guests = day.get("guestCount")

        lines.append(f"\n📅 <b>{date}</b>")

        if occupied is not None:
            occ_line = f"🛏 Занято номеров: {occupied}"
            if complimentary:
                occ_line += f" (+ {complimentary} без оплаты)"
            if total_rooms:
                available = max(total_rooms - (closed or 0), 1)
                pct = 100 * occupied / available
                occ_line += f" — загрузка {pct:.0f}% (из {available} доступных, всего {total_rooms})"
            lines.append(occ_line)

        if closed is not None:
            lines.append(f"🔧 Номера не в эксплуатации: {closed}")

        if arrivals is not None:
            lines.append(f"🚪 Заезды за день: {arrivals}")

        if guests is not None:
            lines.append(f"👥 Гостей: {guests}")

        if revenue is not None:
            lines.append(f"💰 Выручка отеля за день: {format_money(revenue)}")

        if room_revenue is not None:
            lines.append(f"   • по номерам: {format_money(room_revenue)}")

        if meal_revenue is not None:
            lines.append(f"   • по питанию: {format_money(meal_revenue)}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
        resp = requests.post(url, data={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram API error {resp.status_code}: {resp.text}")
        time.sleep(0.5)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

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
        print(f"[!] Не заданы обязательные переменные окружения: {', '.join(missing)}",
              file=sys.stderr)
        sys.exit(1)

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        token = get_access_token(client_id, client_secret)
    except Exception as e:
        print(f"[!] Ошибка авторизации в TravelLine: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        occupancy_data = get_daily_occupancy(token, property_id, yesterday, yesterday)
    except Exception as e:
        occupancy_data = {"warnings": [{"code": "FetchError", "message": str(e)}]}

    try:
        total_rooms = get_total_rooms_count(token, property_id)
    except Exception:
        total_rooms = None  # необязательные данные, отчёт не критичен без них

    report_text = build_report_section(occupancy_data, total_rooms, yesterday)

    print(report_text.replace("<b>", "").replace("</b>", ""))  # локальный лог

    if not tg_token or not tg_chat_id:
        print("\n[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы - "
              "сообщение не отправлено, только выведено выше.", file=sys.stderr)
        return

    send_telegram_message(tg_token, tg_chat_id, report_text)
    print("\n[OK] Отправлено в Telegram.")


if __name__ == "__main__":
    main()

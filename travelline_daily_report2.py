#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Отчёт по загрузке отеля (TravelLine Partner API) -> Telegram.

Что входит в отчёт:
  1. Загрузка за вчерашний день (как раньше).
  2. Сравнение последней полной недели (пн-вс) год к году:
     эта неделя в этом году vs та же неделя ровно 52 недели назад.
  3. Детализация по основным категориям номеров (room types) за последнюю
     полную неделю: количество занятых номеро-ночей, выручка, % загрузки
     по каждой категории.

Источники данных (TravelLine Partner API):
  - PMS Analytics API:     GET /v1/properties/{propertyId}/daily-occupancy
  - PMS API (Property):    GET /v2/properties/{propertyId}/rooms
  - PMS API (Reservation): GET /v2/properties/{propertyId}/reservations/search
                            GET /v2/properties/{propertyId}/reservations/{number}
  - Content API:           GET /v1/properties/{propertyId}   (названия категорий номеров)

Авторизация: OAuth 2.0, Client Credentials Flow. Токен живёт 15 минут, без refresh -
каждый запуск получает новый токен.

Обязательные переменные окружения:
  TL_CLIENT_ID, TL_CLIENT_SECRET, TL_PROPERTY_ID
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Ограничения этой версии (см. README.md):
  - Детализация по категориям номеров строится через постраничный поиск
    бронирований + запрос деталей по каждому - при очень большом количестве
    броней за неделю обработка ограничена MAX_RESERVATIONS_TO_PROCESS,
    чтобы не упереться в лимиты API и время выполнения.
  - Выручка по категории номеров считается пропорционально числу ночей,
    попадающих в отчётную неделю (простая пропорция, без учёта скидок/налогов
    на уровне отдельных ночей).
"""

import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, date

import requests

AUTH_URL = "https://partner.tlintegration.com/auth/token"
ANALYTICS_BASE = "https://partner.tlintegration.com/api/pms-analytics"
PMS_BASE = "https://partner.tlintegration.com/api/pms"
CONTENT_BASE = "https://partner.tlintegration.com/api/content"
REQUEST_TIMEOUT = 20

MAX_RESERVATIONS_TO_PROCESS = 250  # защита от долгого выполнения / лимитов API


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
# TravelLine: справочники (категории номеров, номера)
# --------------------------------------------------------------------------

def get_room_type_meta(token, property_id):
    """
    Content API: полные метаданные категорий номеров.
    roomTypes.categoryCode / categoryName - тип категории (здание/группа),
    например «Коттеджи» / «Апартаменты» - задаётся в TravelLine отельером
    в разделе «Категории номеров» и приходит готовым полем в ответе API.

    Возвращает dict: roomTypeId -> {
        "name": полное название категории,
        "code": префикс кода до ":" (например "К5Д"), для подгруппировки,
        "category": categoryName ("Коттеджи"/"Апартаменты"/...),
        "position": порядок в выдаче (для сохранения порядка отеля),
    }
    """
    url = f"{CONTENT_BASE}/v1/properties/{property_id}"
    resp = requests.get(url, headers=auth_headers(token), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    room_types = data.get("roomTypes", []) or []

    meta = {}
    for rt in room_types:
        name = rt.get("name", rt.get("id"))
        code = name.split(":", 1)[0].strip() if ":" in name else name
        meta[rt.get("id")] = {
            "name": name,
            "code": code,
            "category": rt.get("categoryName") or "Без категории",
            "position": rt.get("position", 0),
        }
    return meta


def get_rooms(token, property_id):
    """
    PMS API: список номеров/люксов отеля с привязкой к категории (roomTypeId).
    GET /v2/properties/{propertyId}/rooms - постраничный.
    Возвращает список словарей {id, roomTypeId, displayName}.
    """
    url = f"{PMS_BASE}/v2/properties/{property_id}/rooms"
    all_rooms = []
    page_token = None
    for _ in range(50):  # защита от бесконечной пагинации
        params = {"maxPageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, params=params, headers=auth_headers(token),
                             timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        all_rooms.extend(data.get("rooms", []))
        if not data.get("hasNextPage"):
            break
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return all_rooms


# --------------------------------------------------------------------------
# TravelLine: загрузка по дням (PMS Analytics API)
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


def aggregate_week(token, property_id, start_date, end_date):
    """
    Суммирует показатели daily-occupancy за диапазон дат (неделю).
    Возвращает dict с суммами + список предупреждений API (если были).
    """
    data = get_daily_occupancy(token, property_id, start_date.isoformat(), end_date.isoformat())
    days = data.get("dailyOccupancies") or data.get("days") or []
    warnings = data.get("warnings") or []

    sums = {
        "occupied_room_nights": 0,
        "closed_room_nights": 0,
        "arrivals": 0,
        "guests": 0,
        "revenue": 0.0,
        "room_revenue": 0.0,
        "meal_revenue": 0.0,
        "days_with_data": 0,
    }
    for day in days:
        sums["days_with_data"] += 1
        sums["occupied_room_nights"] += day.get("occupancyRoomCount") or 0
        sums["closed_room_nights"] += day.get("closedRoomCount") or 0
        sums["arrivals"] += day.get("arrivalCount") or 0
        sums["guests"] += day.get("guestCount") or 0
        sums["revenue"] += day.get("revenue") or 0
        sums["room_revenue"] += day.get("roomRevenue") or 0
        sums["meal_revenue"] += day.get("mealRevenue") or 0

    return sums, warnings


# --------------------------------------------------------------------------
# TravelLine: детализация по категориям номеров (через бронирования)
# --------------------------------------------------------------------------

def search_active_reservation_numbers(token, property_id, start_dt, end_dt):
    """
    PMS API: GET /v2/properties/{propertyId}/reservations/search
    Возвращает номера активных броней, затрагивающих период [start_dt, end_dt].
    """
    url = f"{PMS_BASE}/v2/properties/{property_id}/reservations/search"
    numbers = []
    page_token = None
    for _ in range(50):
        if page_token:
            params = {"pageToken": page_token}  # при токене остальные параметры игнорируются
        else:
            params = {
                "state": "Active",
                "startAffectPeriodDateTime": start_dt.strftime("%Y-%m-%dT%H:%M"),
                "endAffectPeriodDateTime": end_dt.strftime("%Y-%m-%dT%H:%M"),
                "maxPageSize": 100,
            }
        resp = requests.get(url, params=params, headers=auth_headers(token),
                             timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        numbers.extend(r.get("number") for r in data.get("reservations", []) if r.get("number"))
        if len(numbers) >= MAX_RESERVATIONS_TO_PROCESS or not data.get("hasNextPage"):
            break
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return numbers[:MAX_RESERVATIONS_TO_PROCESS]


def get_reservation_details(token, property_id, number):
    url = f"{PMS_BASE}/v2/properties/{property_id}/reservations/{number}"
    resp = requests.get(url, headers=auth_headers(token), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("reservation", {})


def nights_overlap(check_in, check_out, week_start, week_end_exclusive):
    """Число ночей проживания, попадающих в [week_start, week_end_exclusive)."""
    lo = max(check_in, week_start)
    hi = min(check_out, week_end_exclusive)
    return max((hi - lo).days, 0)


def get_room_type_breakdown(token, property_id, room_id_to_type, week_start, week_end):
    """
    Детализация по категориям номеров за неделю [week_start, week_end] (включительно).
    Возвращает dict: roomTypeId -> {"nights": int, "revenue": float, "reservations": int}
    и флаг was_capped (упёрлись ли в лимит обрабатываемых броней).
    """
    week_start_dt = datetime.combine(week_start, datetime.min.time())
    week_end_exclusive_dt = datetime.combine(week_end + timedelta(days=1), datetime.min.time())

    numbers = search_active_reservation_numbers(token, property_id, week_start_dt, week_end_exclusive_dt)
    was_capped = len(numbers) >= MAX_RESERVATIONS_TO_PROCESS

    breakdown = defaultdict(lambda: {"nights": 0, "revenue": 0.0, "reservations": 0})

    for number in numbers:
        try:
            reservation = get_reservation_details(token, property_id, number)
        except Exception:
            continue  # пропускаем единичные сбои, не прерываем весь отчёт
        time.sleep(0.05)  # бережём лимиты API

        for room_stay in reservation.get("roomStays", []):
            room_id = room_stay.get("roomId")
            room_type_id = room_id_to_type.get(room_id, room_stay.get("roomTypeId", "unknown"))

            try:
                check_in = datetime.fromisoformat(room_stay["checkInDateTime"]).date()
                check_out = datetime.fromisoformat(room_stay["checkOutDateTime"]).date()
            except (KeyError, ValueError):
                continue

            total_nights = max((check_out - check_in).days, 1)
            nights_in_week = nights_overlap(
                datetime.combine(check_in, datetime.min.time()),
                datetime.combine(check_out, datetime.min.time()),
                week_start_dt, week_end_exclusive_dt,
            )
            if nights_in_week <= 0:
                continue

            total_price = (room_stay.get("totalPrice", {}) or {}).get("amount", {}).get("value")
            revenue_share = (total_price or 0) * nights_in_week / total_nights

            entry = breakdown[room_type_id]
            entry["nights"] += nights_in_week
            entry["revenue"] += revenue_share
            entry["reservations"] += 1

    return dict(breakdown), was_capped


# --------------------------------------------------------------------------
# Вспомогательное: диапазоны дат
# --------------------------------------------------------------------------

def get_last_full_week(reference_date=None):
    """Последняя полная неделя (понедельник-воскресенье), закончившаяся до сегодня."""
    today = reference_date or date.today()
    days_since_monday = today.weekday()  # Monday = 0
    last_sunday = today - timedelta(days=days_since_monday + 1)
    last_monday = last_sunday - timedelta(days=6)
    return last_monday, last_sunday


# --------------------------------------------------------------------------
# Формирование отчёта
# --------------------------------------------------------------------------

def format_money(value):
    if value is None:
        return "н/д"
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


def format_delta_pct(current, previous):
    if not previous:
        return ""
    delta = 100 * (current - previous) / previous
    sign = "+" if delta >= 0 else ""
    arrow = "🟢" if delta >= 0 else "🔴"
    return f" {arrow} {sign}{delta:.0f}%"


def build_yesterday_section(occupancy_data, total_rooms, report_date):
    lines = [f"<b>🏨 Загрузка отеля — {report_date}</b>"]
    days = occupancy_data.get("dailyOccupancies") or occupancy_data.get("days") or []
    warnings = occupancy_data.get("warnings") or []

    for w in warnings:
        lines.append(f"⚠️ {w.get('code', '')}: {w.get('message', '')}")

    if not days:
        lines.append("Нет данных за указанный период.")
        return "\n".join(lines)

    day = days[0]
    occupied = day.get("occupancyRoomCount")
    complimentary = day.get("complimentaryOccupancyRoomCount")
    closed = day.get("closedRoomCount")

    if occupied is not None:
        occ_line = f"🛏 Занято номеров: {occupied}"
        if complimentary:
            occ_line += f" (+ {complimentary} без оплаты)"
        if total_rooms:
            available = max(total_rooms - (closed or 0), 1)
            pct = 100 * occupied / available
            occ_line += f" — загрузка {pct:.0f}%"
        lines.append(occ_line)

    if day.get("arrivalCount") is not None:
        lines.append(f"🚪 Заезды: {day['arrivalCount']}")
    if day.get("guestCount") is not None:
        lines.append(f"👥 Гостей: {day['guestCount']}")
    if day.get("revenue") is not None:
        lines.append(f"💰 Выручка за день: {format_money(day['revenue'])}")

    return "\n".join(lines)


def build_yoy_section(this_week, this_week_sums, last_year_week, last_year_sums,
                       this_warnings, last_warnings, total_rooms):
    lines = [
        "<b>📊 Сравнение недель, год к году</b>",
        f"Эта неделя: {this_week[0].isoformat()} — {this_week[1].isoformat()}",
        f"Та же неделя год назад: {last_year_week[0].isoformat()} — {last_year_week[1].isoformat()}",
        "",
    ]

    for w in (this_warnings or []):
        lines.append(f"⚠️ (текущая неделя) {w.get('code', '')}: {w.get('message', '')}")
    for w in (last_warnings or []):
        lines.append(f"⚠️ (неделя год назад) {w.get('code', '')}: {w.get('message', '')}")

    def occ_pct(sums):
        if not total_rooms or sums["days_with_data"] == 0:
            return None
        available_nights = max(total_rooms * sums["days_with_data"] - sums["closed_room_nights"], 1)
        return 100 * sums["occupied_room_nights"] / available_nights

    this_pct = occ_pct(this_week_sums)
    last_pct = occ_pct(last_year_sums)

    if this_pct is not None and last_pct is not None:
        lines.append(
            f"🛏 Загрузка: {this_pct:.0f}% vs {last_pct:.0f}% год назад"
            f"{format_delta_pct(this_pct, last_pct)}"
        )
    else:
        lines.append(
            f"🛏 Занятые номеро-ночи: {this_week_sums['occupied_room_nights']} "
            f"vs {last_year_sums['occupied_room_nights']} год назад"
            f"{format_delta_pct(this_week_sums['occupied_room_nights'], last_year_sums['occupied_room_nights'])}"
        )

    lines.append(
        f"🚪 Заезды: {this_week_sums['arrivals']} vs {last_year_sums['arrivals']} год назад"
        f"{format_delta_pct(this_week_sums['arrivals'], last_year_sums['arrivals'])}"
    )
    lines.append(
        f"👥 Гости: {this_week_sums['guests']} vs {last_year_sums['guests']} год назад"
        f"{format_delta_pct(this_week_sums['guests'], last_year_sums['guests'])}"
    )
    lines.append(
        f"💰 Выручка: {format_money(this_week_sums['revenue'])} "
        f"vs {format_money(last_year_sums['revenue'])} год назад"
        f"{format_delta_pct(this_week_sums['revenue'], last_year_sums['revenue'])}"
    )

    return "\n".join(lines)


def _fmt_pct(value):
    return f"{value:,.2f}%".replace(",", " ") if value is not None else "н/д"


def _truncate(text, width):
    return text if len(text) <= width else text[:width - 1] + "…"


def build_room_type_table(this_breakdown, last_breakdown, room_type_meta, rooms_per_type,
                           this_week, last_year_week, was_capped_this, was_capped_last):
    """
    Строит таблицу по категориям номеров (моноширинный текст для Telegram):
    группировка по полю categoryName (здание/тип, напр. «Коттеджи»/«Апартаменты»),
    внутри - подгруппы по общему префиксу кода (напр. «К5Д») с подытогом,
    если префикс встречается больше одного раза. Столбцы: номеров, доступно,
    продано номероночей и % загрузки - за текущую неделю и за ту же неделю год назад.
    """
    this_days = (this_week[1] - this_week[0]).days + 1
    last_days = (last_year_week[1] - last_year_week[0]).days + 1

    NAME_W, NUM_W = 42, 7
    header = (f"{'Категория номера':<{NAME_W}} {'Ном.':>4} "
              f"{'Дост.тек':>{NUM_W}} {'Прод.тек':>{NUM_W}} {'%тек':>7} "
              f"{'Прод.LY':>{NUM_W}} {'%LY':>7}")
    sep = "-" * len(header)

    lines = [
        "<b>🏷 Детализация по категориям номеров</b>",
        (f"Тек. неделя: {this_week[0].isoformat()}—{this_week[1].isoformat()}  |  "
         f"год назад: {last_year_week[0].isoformat()}—{last_year_week[1].isoformat()}"),
    ]
    if was_capped_this or was_capped_last:
        lines.append(
            f"⚠️ Обработано максимум {MAX_RESERVATIONS_TO_PROCESS} броней на неделю - "
            f"при очень высокой загрузке цифры могут быть неполными."
        )
    if not room_type_meta:
        lines.append("Нет данных о категориях номеров (Content API недоступен).")
        return "\n".join(lines)

    def row(name, rooms, this_nights, last_nights):
        available_this = (rooms or 0) * this_days
        available_last = (rooms or 0) * last_days
        pct_this = 100 * this_nights / available_this if available_this else None
        pct_last = 100 * last_nights / available_last if available_last else None
        return (f"{_truncate(name, NAME_W):<{NAME_W}} {rooms or 0:>4} "
                f"{available_this:>{NUM_W}} {this_nights:>{NUM_W}} {_fmt_pct(pct_this):>7} "
                f"{last_nights:>{NUM_W}} {_fmt_pct(pct_last):>7}")

    # Категории, которые нужно показывать в отчёте (в этом порядке)
    ALLOWED_CATEGORIES = ["Коттедж", "Барнхаус", "Дом", "Аппартаменты"]

    # Группировка: categoryName -> code (префикс) -> [roomTypeId, ...]
    by_category = defaultdict(lambda: defaultdict(list))
    seen_categories = set()
    for rt_id, meta in sorted(room_type_meta.items(), key=lambda kv: kv[1].get("position", 0)):
        cat = meta["category"]
        seen_categories.add(cat)
        by_category[cat][meta["code"]].append(rt_id)

    category_order = [cat for cat in ALLOWED_CATEGORIES if cat in seen_categories]

    table_lines = [header, sep]

    grand_rooms = grand_this = grand_last = 0
    revenue_lines = []

    for cat in category_order:
        table_lines.append(f"{cat}")
        cat_rooms = cat_this = cat_last = 0
        cat_revenue_this = cat_revenue_last = 0.0

        for code, rt_ids in by_category[cat].items():
            group_rooms = group_this = group_last = 0
            for rt_id in rt_ids:
                name = room_type_meta[rt_id]["name"]
                rooms = rooms_per_type.get(rt_id, 0)
                this_entry = this_breakdown.get(rt_id, {})
                last_entry = last_breakdown.get(rt_id, {})
                this_nights = this_entry.get("nights", 0)
                last_nights = last_entry.get("nights", 0)
                table_lines.append(row(name, rooms, this_nights, last_nights))
                group_rooms += rooms
                group_this += this_nights
                group_last += last_nights
                cat_revenue_this += this_entry.get("revenue", 0.0) or 0.0
                cat_revenue_last += last_entry.get("revenue", 0.0) or 0.0

            if len(rt_ids) > 1:
                table_lines.append(row(f"Итого {code}", group_rooms, group_this, group_last))

            cat_rooms += group_rooms
            cat_this += group_this
            cat_last += group_last

        table_lines.append(row(f"Итого {cat}", cat_rooms, cat_this, cat_last))
        table_lines.append("")

        grand_rooms += cat_rooms
        grand_this += cat_this
        grand_last += cat_last

        revenue_lines.append(
            f"{cat}: {format_money(cat_revenue_this)} vs {format_money(cat_revenue_last)} "
            f"год назад{format_delta_pct(cat_revenue_this, cat_revenue_last)}"
        )

    total_label = "Итого " + "+".join(category_order)
    table_lines.append(row(total_label, grand_rooms, grand_this, grand_last))

    lines.append("<pre>" + "\n".join(table_lines) + "</pre>")

    if revenue_lines:
        lines.append("<b>💰 Сравнение выручки по категориям, год к году</b>")
        lines.extend(revenue_lines)

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def _split_telegram_message(text, limit=4000):
    """
    Splits text into Telegram-safe chunks without breaking an open <pre> tag
    across chunk boundaries. If a chunk would cut through an open <pre> block,
    the current chunk is closed with </pre> and the next chunk is reopened
    with <pre> so every chunk is valid, self-contained HTML.
    """
    if len(text) <= limit:
        return [text]

    lines = text.split("\n")
    chunks = []
    current_lines = []
    current_len = 0
    in_pre = False

    def flush():
        nonlocal current_lines, current_len, in_pre
        if not current_lines:
            return
        chunk = "\n".join(current_lines)
        if in_pre:
            chunk += "</pre>"
        chunks.append(chunk)
        current_lines = []
        current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for the newline joining it back
        # Reserve room for a closing </pre> if we're currently inside a <pre>
        # block, so a flush triggered mid-loop never exceeds the limit.
        reserve = len("</pre>") if in_pre else 0

        if current_lines and current_len + line_len + reserve > limit:
            flush()
            if in_pre:
                # We just closed an open <pre> block; reopen it for the new chunk.
                current_lines.append("<pre>")
                current_len += len("<pre>\n")

        current_lines.append(line)
        current_len += line_len

        if "<pre>" in line:
            in_pre = True
        if "</pre>" in line:
            in_pre = False

    flush()
    return chunks or [text]


def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = _split_telegram_message(text, limit=4000)
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

    yesterday = (datetime.now() - timedelta(days=1)).date()

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
    except Exception:
        total_rooms = None
        room_id_to_type = {}
        rooms_per_type = {}

    try:
        room_type_meta = get_room_type_meta(token, property_id)
    except Exception:
        room_type_meta = {}

    # --- вчера ---
    try:
        occupancy_data = get_daily_occupancy(token, property_id, yesterday.isoformat(), yesterday.isoformat())
    except Exception as e:
        occupancy_data = {"warnings": [{"code": "FetchError", "message": str(e)}]}
    yesterday_section = build_yesterday_section(occupancy_data, total_rooms, yesterday.isoformat())

    # --- YoY по неделям ---
    this_week = get_last_full_week()
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

    yoy_section = build_yoy_section(this_week, this_week_sums, last_year_week, last_year_sums,
                                     this_warnings, last_warnings, total_rooms)

    # --- детализация по категориям номеров: тек. неделя и та же неделя год назад ---
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

    full_report = "\n\n".join([yesterday_section, yoy_section, room_type_section])

    print(full_report.replace("<b>", "").replace("</b>", "").replace("<u>", "").replace("</u>", ""))

    if not tg_token or not tg_chat_id:
        print("\n[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы - "
              "сообщение не отправлено, только выведено выше.", file=sys.stderr)
        return

    send_telegram_message(tg_token, tg_chat_id, full_report)
    print("\n[OK] Отправлено в Telegram.")


if __name__ == "__main__":
    main()

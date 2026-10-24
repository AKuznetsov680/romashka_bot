#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Отчёт "Месяц к месяцу год назад" (TravelLine Partner API) -> Telegram.

Сравнивает текущий месяц с тем же месяцем год назад:
  - Month-to-Date: с 1-го числа по вчера vs тот же отрезок месяца год назад
    (одинаковое число дней, поэтому сравнение честное, пока месяц не закончился);
  - для справки - полный прошлогодний месяц (итог, к которому надо прийти);
  - общие метрики: выручка, загрузка, ADR, RevPAR, заезды, гости;
  - детализация по категориям размещения (Коттедж, Барнхаус, Дом, Аппартаменты):
    номероночи, загрузка и выручка год к году.

Если сегодня 1-е число (данных за текущий месяц ещё нет), сравнивается
полностью завершившийся предыдущий месяц.

Сравнение идёт по календарным датам (1-24 октября vs 1-24 октября), а не по
сдвигу -364 дня: для месячного отчёта важен именно календарный месяц.

Переиспользует функции из travelline_daily_report.py.

Обязательные переменные окружения:
  TL_CLIENT_ID, TL_CLIENT_SECRET, TL_PROPERTY_ID
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import calendar
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
    format_money,
    format_delta_pct,
    _fmt_pct,
    send_telegram_message,
)

ALLOWED_CATEGORIES = ["Коттедж", "Барнхаус", "Дом", "Аппартаменты"]

MONTHS_RU = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}


# --------------------------------------------------------------------------
# Периоды
# --------------------------------------------------------------------------

def _month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def get_month_periods(today=None):
    """
    Возвращает словарь с периодами:
      cur    - (start, end) текущий месяц с 1-го по вчера (или весь прошлый
               месяц, если сегодня 1-е число);
      ly     - тот же отрезок месяца год назад (то же число дней);
      ly_full - полный прошлогодний месяц;
      cur_full - полный текущий месяц (до конца календарного месяца);
      is_partial - True, если текущий месяц ещё не закончился.
    """
    today = today or date.today()
    yesterday = today - timedelta(days=1)

    if yesterday.month != today.month:
        # сегодня 1-е число: берём завершившийся предыдущий месяц целиком
        year, month = yesterday.year, yesterday.month
        start = date(year, month, 1)
        end = _month_end(year, month)
    else:
        year, month = today.year, today.month
        start = date(year, month, 1)
        end = yesterday

    ly_year = year - 1
    ly_start = date(ly_year, month, 1)
    ly_last_day = _month_end(ly_year, month)
    # min по номеру дня ДО построения даты: 29 февраля в невисокосном году не существует
    ly_end = date(ly_year, month, min(end.day, ly_last_day.day))

    cur_full_end = _month_end(year, month)

    return {
        "year": year,
        "month": month,
        "cur": (start, end),
        "ly": (ly_start, ly_end),
        "ly_full": (ly_start, ly_last_day),
        "cur_full": (start, cur_full_end),
        "is_partial": end < cur_full_end,
    }


# --------------------------------------------------------------------------
# Получение данных
# --------------------------------------------------------------------------

def sum_period(token, property_id, start, end):
    """
    daily-occupancy за произвольный период, окнами по 31 день
    (ограничение PMS Analytics API). Возвращает (sums, warnings).
    """
    totals = {
        "occupied_room_nights": 0, "closed_room_nights": 0, "arrivals": 0,
        "guests": 0, "revenue": 0.0, "room_revenue": 0.0, "meal_revenue": 0.0,
        "days_with_data": 0,
    }
    warnings = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=30), end)
        sums, warns = aggregate_week(token, property_id, chunk_start, chunk_end)
        for key in totals:
            totals[key] += sums.get(key, 0) or 0
        warnings.extend(warns)
        chunk_start = chunk_end + timedelta(days=1)
    return totals, warnings


def breakdown_for_period(token, property_id, room_id_to_type, start, end):
    """
    Детализация по типам номеров за период. Месяц режется на недельные окна:
    get_room_type_breakdown ограничен MAX_RESERVATIONS_TO_PROCESS на один вызов,
    а бронь делится по ночам пропорционально, поэтому суммирование окон корректно.
    Возвращает (breakdown, was_capped_any).
    """
    merged = defaultdict(lambda: {"nights": 0, "revenue": 0.0})
    capped_any = False
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=6), end)
        part, capped = get_room_type_breakdown(
            token, property_id, room_id_to_type, chunk_start, chunk_end
        )
        capped_any = capped_any or capped
        for rt_id, entry in part.items():
            merged[rt_id]["nights"] += entry.get("nights", 0) or 0
            merged[rt_id]["revenue"] += entry.get("revenue", 0.0) or 0.0
        chunk_start = chunk_end + timedelta(days=1)
    return dict(merged), capped_any


# --------------------------------------------------------------------------
# Расчёты
# --------------------------------------------------------------------------

def calc_metrics(sums, total_rooms, days):
    """Загрузка, ADR, RevPAR из суммарных данных периода."""
    available = None
    if total_rooms:
        available = max(total_rooms * days - sums.get("closed_room_nights", 0), 1)
    occupied = sums.get("occupied_room_nights", 0)
    revenue = sums.get("revenue", 0.0)
    occ = 100 * occupied / available if available else None
    adr = revenue / occupied if occupied else None
    revpar = revenue / available if available else None
    return {"occ": occ, "adr": adr, "revpar": revpar, "available": available}


def _days(period):
    return (period[1] - period[0]).days + 1


def _money_or_nd(value):
    return format_money(value) if value is not None else "н/д"


def _delta(cur, prev):
    return format_delta_pct(cur, prev) if cur is not None and prev else ""


# --------------------------------------------------------------------------
# Секции отчёта
# --------------------------------------------------------------------------

def build_header(periods):
    month_name = MONTHS_RU[periods["month"]]
    cur_s, cur_e = periods["cur"]
    ly_s, ly_e = periods["ly"]
    lines = [f"<b>🗓 {month_name.capitalize()} {periods['year']} vs {month_name} {periods['year'] - 1}</b>"]
    lines.append(f"Текущий период: {cur_s.isoformat()} — {cur_e.isoformat()} ({_days(periods['cur'])} дн.)")
    lines.append(f"Тот же отрезок год назад: {ly_s.isoformat()} — {ly_e.isoformat()} ({_days(periods['ly'])} дн.)")
    if periods["is_partial"]:
        lines.append("ℹ️ Месяц ещё не закончился: сравниваются одинаковые отрезки месяца.")
    return "\n".join(lines)


def build_totals_section(periods, cur_sums, ly_sums, ly_full_sums, total_rooms):
    days_cur = _days(periods["cur"])
    days_ly = _days(periods["ly"])
    days_ly_full = _days(periods["ly_full"])

    cur = calc_metrics(cur_sums, total_rooms, days_cur)
    ly = calc_metrics(ly_sums, total_rooms, days_ly)
    ly_full = calc_metrics(ly_full_sums, total_rooms, days_ly_full)

    lines = ["<b>📊 Итого по базе отдыха</b>"]
    lines.append(
        f"💰 Выручка: {format_money(cur_sums['revenue'])} vs {format_money(ly_sums['revenue'])} год назад"
        f"{format_delta_pct(cur_sums['revenue'], ly_sums['revenue'])}"
    )
    lines.append(
        f"🛏 Загрузка: {_fmt_pct(cur['occ'])} vs {_fmt_pct(ly['occ'])} год назад"
        f"{_delta(cur['occ'], ly['occ'])}"
    )
    lines.append(
        f"🏷 ADR (средняя цена ночи): {_money_or_nd(cur['adr'])} vs {_money_or_nd(ly['adr'])} год назад"
        f"{_delta(cur['adr'], ly['adr'])}"
    )
    lines.append(
        f"📐 RevPAR: {_money_or_nd(cur['revpar'])} vs {_money_or_nd(ly['revpar'])} год назад"
        f"{_delta(cur['revpar'], ly['revpar'])}"
    )
    lines.append(
        f"🚪 Заезды: {cur_sums['arrivals']} vs {ly_sums['arrivals']} год назад"
        f"{format_delta_pct(cur_sums['arrivals'], ly_sums['arrivals'])}"
    )
    lines.append(
        f"👥 Гости: {cur_sums['guests']} vs {ly_sums['guests']} год назад"
        f"{format_delta_pct(cur_sums['guests'], ly_sums['guests'])}"
    )

    if periods["is_partial"]:
        lines.append("")
        lines.append(
            f"📌 Для ориентира, весь {MONTHS_RU[periods['month']]} {periods['year'] - 1} целиком "
            f"({days_ly_full} дн.): выручка {format_money(ly_full_sums['revenue'])}, "
            f"загрузка {_fmt_pct(ly_full['occ'])}, ADR {_money_or_nd(ly_full['adr'])}."
        )
        if ly_full_sums["revenue"]:
            progress = 100 * cur_sums["revenue"] / ly_full_sums["revenue"]
            lines.append(
                f"Выручка текущего месяца на сегодня составляет {progress:.0f}% от прошлогоднего итога за месяц."
            )
    return "\n".join(lines)


def build_category_section(periods, cur_bd, ly_bd, room_type_meta, rooms_per_type, capped):
    days_cur = _days(periods["cur"])
    days_ly = _days(periods["ly"])

    cat_rooms = defaultdict(int)
    cat_cur_nights = defaultdict(int)
    cat_ly_nights = defaultdict(int)
    cat_cur_rev = defaultdict(float)
    cat_ly_rev = defaultdict(float)

    for rt_id, meta in room_type_meta.items():
        cat = meta.get("category")
        if cat not in ALLOWED_CATEGORIES:
            continue
        cat_rooms[cat] += rooms_per_type.get(rt_id, 0)
        cur_e = cur_bd.get(rt_id, {})
        ly_e = ly_bd.get(rt_id, {})
        cat_cur_nights[cat] += cur_e.get("nights", 0) or 0
        cat_ly_nights[cat] += ly_e.get("nights", 0) or 0
        cat_cur_rev[cat] += cur_e.get("revenue", 0.0) or 0.0
        cat_ly_rev[cat] += ly_e.get("revenue", 0.0) or 0.0

    lines = ["<b>🏷 Детализация по категориям размещения</b>"]
    any_data = False
    tot_rooms = tot_cur = tot_ly = 0
    tot_cur_rev = tot_ly_rev = 0.0

    for cat in ALLOWED_CATEGORIES:
        rooms = cat_rooms.get(cat, 0)
        if rooms == 0:
            continue
        any_data = True
        avail_cur = rooms * days_cur
        avail_ly = rooms * days_ly
        occ_cur = 100 * cat_cur_nights[cat] / avail_cur if avail_cur else None
        occ_ly = 100 * cat_ly_nights[cat] / avail_ly if avail_ly else None
        adr_cur = cat_cur_rev[cat] / cat_cur_nights[cat] if cat_cur_nights[cat] else None
        adr_ly = cat_ly_rev[cat] / cat_ly_nights[cat] if cat_ly_nights[cat] else None

        lines.append("")
        lines.append(f"<u>{cat}</u> ({rooms} ном.)")
        lines.append(
            f"Загрузка: {_fmt_pct(occ_cur)} vs {_fmt_pct(occ_ly)} год назад"
            f"{_delta(occ_cur, occ_ly)}"
        )
        lines.append(
            f"Номероночи: {cat_cur_nights[cat]} из {avail_cur} vs {cat_ly_nights[cat]} из {avail_ly}"
        )
        lines.append(
            f"Выручка: {format_money(cat_cur_rev[cat])} vs {format_money(cat_ly_rev[cat])} год назад"
            f"{format_delta_pct(cat_cur_rev[cat], cat_ly_rev[cat])}"
        )
        lines.append(
            f"ADR: {_money_or_nd(adr_cur)} vs {_money_or_nd(adr_ly)} год назад{_delta(adr_cur, adr_ly)}"
        )

        tot_rooms += rooms
        tot_cur += cat_cur_nights[cat]
        tot_ly += cat_ly_nights[cat]
        tot_cur_rev += cat_cur_rev[cat]
        tot_ly_rev += cat_ly_rev[cat]

    if not any_data:
        lines.append("Нет данных по категориям Коттедж/Барнхаус/Дом/Аппартаменты.")
        return "\n".join(lines)

    avail_cur = tot_rooms * days_cur
    avail_ly = tot_rooms * days_ly
    occ_cur = 100 * tot_cur / avail_cur if avail_cur else None
    occ_ly = 100 * tot_ly / avail_ly if avail_ly else None
    lines.append("")
    lines.append(
        f"<u>Итого по 4 категориям</u>: загрузка {_fmt_pct(occ_cur)} vs {_fmt_pct(occ_ly)}"
        f"{_delta(occ_cur, occ_ly)}; выручка {format_money(tot_cur_rev)} vs {format_money(tot_ly_rev)}"
        f"{format_delta_pct(tot_cur_rev, tot_ly_rev)}"
    )

    if capped:
        lines.append(
            "⚠️ В отдельных неделях достигнут лимит обрабатываемых броней - "
            "детализация по категориям может быть неполной."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# main
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
        print(f"[!] Не заданы обязательные переменные окружения: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    periods = get_month_periods(date.today())

    try:
        token = get_access_token(client_id, client_secret)
    except Exception as e:
        print(f"[!] Ошибка авторизации в TravelLine: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        rooms = get_rooms(token, property_id)
        total_rooms = len(rooms) or None
        room_id_to_type = {r.get("id"): r.get("roomTypeId") for r in rooms}
        rooms_per_type = defaultdict(int)
        for r in rooms:
            rooms_per_type[r.get("roomTypeId")] += 1
    except Exception as e:
        print(f"[!] Не удалось получить справочник номеров: {e}", file=sys.stderr)
        total_rooms, room_id_to_type, rooms_per_type = None, {}, {}

    try:
        room_type_meta = get_room_type_meta(token, property_id)
    except Exception as e:
        print(f"[!] Не удалось получить категории номеров: {e}", file=sys.stderr)
        room_type_meta = {}

    sections = [build_header(periods)]

    # --- общие метрики ---
    try:
        cur_sums, _ = sum_period(token, property_id, *periods["cur"])
        ly_sums, _ = sum_period(token, property_id, *periods["ly"])
        if periods["is_partial"]:
            ly_full_sums, _ = sum_period(token, property_id, *periods["ly_full"])
        else:
            ly_full_sums = ly_sums
        sections.append(build_totals_section(periods, cur_sums, ly_sums, ly_full_sums, total_rooms))
    except Exception as e:
        sections.append(f"<b>📊 Итого по базе отдыха</b>\n⚠️ Ошибка получения данных: {e}")
        print(f"[!] Ошибка общих метрик: {e}", file=sys.stderr)

    # --- по категориям ---
    try:
        cur_bd, capped_cur = breakdown_for_period(token, property_id, room_id_to_type, *periods["cur"])
        ly_bd, capped_ly = breakdown_for_period(token, property_id, room_id_to_type, *periods["ly"])
        sections.append(build_category_section(
            periods, cur_bd, ly_bd, room_type_meta, rooms_per_type, capped_cur or capped_ly
        ))
    except Exception as e:
        sections.append(f"<b>🏷 Детализация по категориям размещения</b>\n⚠️ Ошибка: {e}")
        print(f"[!] Ошибка детализации по категориям: {e}", file=sys.stderr)

    full_report = "\n\n".join(sections)

    print(full_report.replace("<b>", "").replace("</b>", "").replace("<u>", "").replace("</u>", ""))

    if not tg_token or not tg_chat_id:
        print("\n[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы - "
              "сообщение не отправлено, только выведено выше.", file=sys.stderr)
        return

    send_telegram_message(tg_token, tg_chat_id, full_report)
    print("\n[OK] Отправлено в Telegram.")


if __name__ == "__main__":
    main()

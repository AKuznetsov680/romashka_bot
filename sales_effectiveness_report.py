#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Отчёт "Эффективность продаж" (TravelLine Partner API) -> Telegram.

Отдельный от travelline_daily_report.py отчёт: не про загрузку вчера/на неделю,
а про то, НАСКОЛЬКО ЭФФЕКТИВНО идут продажи на текущую дату, с оглядкой на то,
что база отдыха имеет ярко выраженную сезонность. Собраны метрики, которые
используют revenue-менеджеры в мировой гостиничной индустрии:

  1. Итоговый вывод (traffic light) - сводный сигнал по нескольким метрикам.
  2. Season-to-Date (STD) - сезон с начала до сегодня vs тот же отрезок
     сезона год назад: выручка, загрузка, ADR, RevPAR. Это ключевая метрика
     именно для сезонного бизнеса - показывает, где вы находитесь относительно
     прошлого сезона на ту же календарную дату сезона.
  3. ADR / RevPAR по категориям номеров (последняя полная неделя, год к году) -
     стандартные метрики revenue-менеджмента: ADR показывает не проседаете ли
     по цене, RevPAR - сводная эффективность (цена x загрузка).
  4. On-the-Books pace curve на 1-6 недель вперёд (год к году, тот же срок
     до заезда) - классический "pickup"/"OTB" отчёт: видно саму форму кривой
     спроса на ближайшие недели, а не разовый срез.
  5. Окно бронирования (booking window / lead time), доля отмен и разбивка
     выручки по каналам продаж - показывают, СКОЛЬКО заранее бронируют,
     насколько бронирования "долетают" до заезда, и через какие каналы идут
     основные продажи.
  6. Конверсия ближайших будних/выходных дат в брони по категориям (год к году).

Источники данных: см. travelline_daily_report.py - этот скрипт переиспользует
её функции авторизации, справочников и работы с API, чтобы не дублировать
уже проверенный код.

Обязательные переменные окружения:
  TL_CLIENT_ID, TL_CLIENT_SECRET, TL_PROPERTY_ID
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Опциональные переменные окружения (границы высокого сезона для Season-to-Date,
по умолчанию 1 мая - 30 сентября - ПОДСТРОЙТЕ под реальный сезон базы отдыха):
  SEASON_START_MONTH, SEASON_START_DAY, SEASON_END_MONTH, SEASON_END_DAY
"""

import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from travelline_daily_report import (
    get_access_token,
    get_rooms,
    get_room_type_meta,
    aggregate_week,
    search_active_reservation_numbers,
    get_reservation_details,
    get_booking_created_at,
    get_room_type_breakdown,
    get_pickup_snapshot,
    get_future_full_week,
    get_last_full_week,
    build_occupancy_conversion_section,
    format_money,
    format_delta_pct,
    _fmt_pct,
    send_telegram_message,
)

ALLOWED_CATEGORIES = ["Коттедж", "Барнхаус", "Дом", "Аппартаменты"]
PACE_WEEKS_AHEAD = (1, 2, 3, 4, 5, 6)

# Границы высокого сезона - подстройте под реальный календарь базы отдыха.
SEASON_START_MONTH = int(os.environ.get("SEASON_START_MONTH", 5))
SEASON_START_DAY = int(os.environ.get("SEASON_START_DAY", 1))
SEASON_END_MONTH = int(os.environ.get("SEASON_END_MONTH", 9))
SEASON_END_DAY = int(os.environ.get("SEASON_END_DAY", 30))


# --------------------------------------------------------------------------
# Вспомогательные расчёты
# --------------------------------------------------------------------------

def _same_date_last_year(d):
    """d минус 1 календарный год, с защитой от 29 февраля."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def calc_adr_revpar(revenue, occupied_nights, available_nights):
    """
    ADR (Average Daily Rate) = выручка / проданные номероночи.
    RevPAR (Revenue per Available Room) = выручка / номероночи в наличии.
    Обе метрики - стандарт revenue-менеджмента в гостиничной индустрии.
    """
    adr = revenue / occupied_nights if occupied_nights else None
    revpar = revenue / available_nights if available_nights else None
    occ_pct = 100 * occupied_nights / available_nights if available_nights else None
    return adr, revpar, occ_pct


def get_season_bounds(reference_date=None):
    """
    Границы высокого сезона для года reference_date. Если сегодня после конца
    сезона, границы всё равно считаются по текущему году (сезон закрыт -
    Season-to-Date покажет итог завершившегося сезона).
    """
    today = reference_date or date.today()
    start = date(today.year, SEASON_START_MONTH, SEASON_START_DAY)
    end = date(today.year, SEASON_END_MONTH, SEASON_END_DAY)
    in_season = start <= today <= end
    return start, end, in_season


def sum_period(token, property_id, start, end):
    """
    Суммирует daily-occupancy за произвольный диапазон дат, разбивая его на
    окна по 31 дню (ограничение PMS Analytics API), и суммируя aggregate_week
    по каждому окну. Подходит для многомесячных периодов (season-to-date),
    не упирается в лимиты enumerate-подхода через reservations/search.
    """
    empty = {
        "occupied_room_nights": 0, "closed_room_nights": 0, "arrivals": 0,
        "guests": 0, "revenue": 0.0, "room_revenue": 0.0, "meal_revenue": 0.0,
        "days_with_data": 0,
    }
    if start > end:
        return dict(empty), []

    totals = dict(empty)
    all_warnings = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=30), end)  # окно <= 31 дня
        sums, warnings = aggregate_week(token, property_id, chunk_start, chunk_end)
        for key in totals:
            totals[key] += sums.get(key, 0)
        all_warnings.extend(warnings)
        chunk_start = chunk_end + timedelta(days=1)

    return totals, all_warnings


# --------------------------------------------------------------------------
# Секция 1: Season-to-Date (сезон к текущей дате)
# --------------------------------------------------------------------------

def build_season_to_date_section(token, property_id, total_rooms, reference_date=None):
    """
    Возвращает (текст секции, occ_delta_в_п.п._или_None).
    occ_delta нужен итоговому выводу (build_executive_summary).
    """
    today = reference_date or date.today()
    season_start, season_end, in_season = get_season_bounds(today)

    lines = ["<b>🌞 Сезон к текущей дате (Season-to-Date)</b>"]

    if today < season_start:
        lines.append(
            f"Сезон {season_start.isoformat()}—{season_end.isoformat()} ещё не начался - "
            f"сравнение появится с {season_start.isoformat()}."
        )
        return "\n".join(lines), None

    std_end = min(today, season_end)
    ly_start = _same_date_last_year(season_start)
    ly_end = _same_date_last_year(std_end)

    try:
        this_totals, _ = sum_period(token, property_id, season_start, std_end)
    except Exception as e:
        lines.append(f"⚠️ Ошибка получения данных за текущий сезон: {e}")
        return "\n".join(lines), None

    try:
        ly_totals, _ = sum_period(token, property_id, ly_start, ly_end)
    except Exception as e:
        lines.append(f"⚠️ Ошибка получения данных за сезон год назад: {e}")
        return "\n".join(lines), None

    days_this = (std_end - season_start).days + 1
    days_ly = (ly_end - ly_start).days + 1

    avail_this = max(total_rooms * days_this - this_totals["closed_room_nights"], 1) if total_rooms else None
    avail_ly = max(total_rooms * days_ly - ly_totals["closed_room_nights"], 1) if total_rooms else None

    adr_this, revpar_this, occ_this = calc_adr_revpar(
        this_totals["revenue"], this_totals["occupied_room_nights"], avail_this
    )
    adr_ly, revpar_ly, occ_ly = calc_adr_revpar(
        ly_totals["revenue"], ly_totals["occupied_room_nights"], avail_ly
    )

    lines.append(
        f"Период: {season_start.isoformat()}—{std_end.isoformat()} ({days_this} дн.) "
        f"vs год назад {ly_start.isoformat()}—{ly_end.isoformat()} ({days_ly} дн.)"
    )
    lines.append(
        f"Выручка: {format_money(this_totals['revenue'])} vs {format_money(ly_totals['revenue'])} "
        f"год назад{format_delta_pct(this_totals['revenue'], ly_totals['revenue'])}"
    )
    lines.append(
        f"Загрузка: {_fmt_pct(occ_this)} vs {_fmt_pct(occ_ly)} год назад"
        + (format_delta_pct(occ_this, occ_ly) if occ_this is not None and occ_ly else "")
    )
    lines.append(
        f"ADR (средняя цена ночи): {format_money(adr_this) if adr_this is not None else 'н/д'} vs "
        f"{format_money(adr_ly) if adr_ly is not None else 'н/д'} год назад"
        + (format_delta_pct(adr_this, adr_ly) if adr_this and adr_ly else "")
    )
    lines.append(
        f"RevPAR: {format_money(revpar_this) if revpar_this is not None else 'н/д'} vs "
        f"{format_money(revpar_ly) if revpar_ly is not None else 'н/д'} год назад"
        + (format_delta_pct(revpar_this, revpar_ly) if revpar_this and revpar_ly else "")
    )
    if not in_season:
        lines.append(f"ℹ️ Сегодня вне высокого сезона - показан итог сезона {season_start.year} года.")

    occ_delta = (occ_this - occ_ly) if (occ_this is not None and occ_ly is not None) else None
    return "\n".join(lines), occ_delta


# --------------------------------------------------------------------------
# Секция 2: ADR / RevPAR по категориям (последняя полная неделя)
# --------------------------------------------------------------------------

def build_adr_revpar_by_category_section(this_breakdown, last_breakdown, room_type_meta,
                                          rooms_per_type, this_week, last_year_week):
    lines = ["<b>💵 ADR / RevPAR по категориям (последняя полная неделя)</b>"]
    days_this = (this_week[1] - this_week[0]).days + 1
    days_last = (last_year_week[1] - last_year_week[0]).days + 1

    cat_rooms = defaultdict(int)
    cat_this_nights = defaultdict(int)
    cat_this_revenue = defaultdict(float)
    cat_last_nights = defaultdict(int)
    cat_last_revenue = defaultdict(float)

    for rt_id, meta in room_type_meta.items():
        cat = meta.get("category")
        if cat not in ALLOWED_CATEGORIES:
            continue
        rooms = rooms_per_type.get(rt_id, 0)
        cat_rooms[cat] += rooms
        this_e = this_breakdown.get(rt_id, {})
        last_e = last_breakdown.get(rt_id, {})
        cat_this_nights[cat] += this_e.get("nights", 0)
        cat_this_revenue[cat] += this_e.get("revenue", 0.0) or 0.0
        cat_last_nights[cat] += last_e.get("nights", 0)
        cat_last_revenue[cat] += last_e.get("revenue", 0.0) or 0.0

    any_data = False
    for cat in ALLOWED_CATEGORIES:
        rooms = cat_rooms.get(cat, 0)
        if rooms == 0:
            continue
        any_data = True
        avail_this = rooms * days_this
        avail_last = rooms * days_last

        adr_this, revpar_this, _ = calc_adr_revpar(cat_this_revenue[cat], cat_this_nights[cat], avail_this)
        adr_last, revpar_last, _ = calc_adr_revpar(cat_last_revenue[cat], cat_last_nights[cat], avail_last)

        lines.append("")
        lines.append(f"<u>{cat}</u>")
        lines.append(
            f"ADR: {format_money(adr_this) if adr_this is not None else 'н/д'} vs "
            f"{format_money(adr_last) if adr_last is not None else 'н/д'} год назад"
            + (format_delta_pct(adr_this, adr_last) if adr_this and adr_last else "")
        )
        lines.append(
            f"RevPAR: {format_money(revpar_this) if revpar_this is not None else 'н/д'} vs "
            f"{format_money(revpar_last) if revpar_last is not None else 'н/д'} год назад"
            + (format_delta_pct(revpar_this, revpar_last) if revpar_this and revpar_last else "")
        )

    if not any_data:
        lines.append("Нет данных по категориям Коттедж/Барнхаус/Дом/Аппартаменты.")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Секция 3: On-the-Books pace curve, 1-6 недель вперёд
# --------------------------------------------------------------------------

def build_pace_curve_section(token, property_id, total_rooms, weeks_ahead_list=PACE_WEEKS_AHEAD,
                              reference_date=None):
    """
    Возвращает (текст секции, средний_разрыв_в_п.п._или_None).
    """
    today = reference_date or date.today()
    lines = ["<b>📈 Кривая бронирований на 1-6 недель вперёд (OTB pace, год к году)</b>"]
    lines.append(
        "Загрузка \"на сегодня\" по будущим неделям vs год назад на том же сроке "
        "до заезда - стандартный pickup/OTB-отчёт revenue-менеджмента."
    )

    created_before_now = datetime.combine(today + timedelta(days=1), datetime.min.time())
    gaps = []

    for weeks_ahead in weeks_ahead_list:
        week_start, week_end = get_future_full_week(weeks_ahead, today)
        ly_week_start = week_start - timedelta(days=364)
        ly_week_end = week_end - timedelta(days=364)
        ly_created_before = datetime.combine(
            today - timedelta(days=364) + timedelta(days=1), datetime.min.time()
        )
        days_count = (week_end - week_start).days + 1

        try:
            current_totals, _, _ = get_pickup_snapshot(
                token, property_id, week_start, week_end, created_before_now
            )
            ly_totals, _, _ = get_pickup_snapshot(
                token, property_id, ly_week_start, ly_week_end, ly_created_before
            )
        except Exception as e:
            lines.append(f"Неделя +{weeks_ahead}: ⚠️ ошибка расчёта: {e}")
            continue

        avail = total_rooms * days_count if total_rooms else None
        pct_this = 100 * current_totals["nights"] / avail if avail else None
        pct_ly = 100 * ly_totals["nights"] / avail if avail else None

        if pct_this is not None and pct_ly is not None:
            gaps.append(pct_this - pct_ly)

        lines.append(
            f"Неделя +{weeks_ahead} ({week_start.isoformat()}—{week_end.isoformat()}): "
            f"{_fmt_pct(pct_this)} vs {_fmt_pct(pct_ly)} год назад"
            + (format_delta_pct(pct_this, pct_ly) if pct_this is not None and pct_ly else "")
        )

    avg_gap = sum(gaps) / len(gaps) if gaps else None
    if avg_gap is not None:
        sign = "🟢 опережаем" if avg_gap > 0.5 else ("🔴 отстаём" if avg_gap < -0.5 else "🟡 на уровне")
        lines.append(f"\nСредний разрыв к прошлому году по загрузке на 6 недель вперёд: {avg_gap:+.1f} п.п. {sign}")

    return "\n".join(lines), avg_gap


# --------------------------------------------------------------------------
# Секция 4: окно бронирования (lead time), отмены, каналы продаж
# --------------------------------------------------------------------------

def collect_week_insights(token, property_id, week_start, week_end):
    """
    Один проход по броням недели: среднее окно бронирования (дни от создания
    брони до заезда), доля отменённых броней, разбивка выручки по каналам
    продаж. Использует уже существующие функции работы с API - дополнительной
    инфраструктуры не требуется.
    """
    week_start_dt = datetime.combine(week_start, datetime.min.time())
    week_end_exclusive_dt = datetime.combine(week_end + timedelta(days=1), datetime.min.time())

    active_numbers = search_active_reservation_numbers(
        token, property_id, week_start_dt, week_end_exclusive_dt, state="Active"
    )
    try:
        cancelled_numbers = search_active_reservation_numbers(
            token, property_id, week_start_dt, week_end_exclusive_dt, state="Cancelled"
        )
    except Exception:
        cancelled_numbers = []

    lead_times = []
    channel_revenue = defaultdict(float)
    total_revenue = 0.0

    for number in active_numbers:
        try:
            reservation = get_reservation_details(token, property_id, number)
        except Exception:
            continue
        time.sleep(0.05)

        channel = (
            (reservation.get("channelInformation") or {}).get("channelName")
            or (reservation.get("creationSource") or {}).get("name")
            or "Не определён"
        )

        try:
            created_at = get_booking_created_at(token, property_id, number)
        except Exception:
            created_at = None
        time.sleep(0.05)

        for room_stay in reservation.get("roomStays", []):
            try:
                check_in = datetime.fromisoformat(room_stay["checkInDateTime"])
            except (KeyError, ValueError):
                continue

            price = ((room_stay.get("totalPrice") or {}).get("amount") or {}).get("value") or 0
            channel_revenue[channel] += price
            total_revenue += price

            if created_at is not None:
                lead_days = (check_in.date() - created_at.date()).days
                if lead_days >= 0:
                    lead_times.append(lead_days)

    total_bookings = len(active_numbers) + len(cancelled_numbers)
    cancel_rate = 100 * len(cancelled_numbers) / total_bookings if total_bookings else None
    avg_lead_time = sum(lead_times) / len(lead_times) if lead_times else None

    return {
        "avg_lead_time": avg_lead_time,
        "cancel_rate": cancel_rate,
        "active_count": len(active_numbers),
        "cancelled_count": len(cancelled_numbers),
        "channel_revenue": dict(channel_revenue),
        "total_revenue": total_revenue,
    }


def build_lead_cancel_channel_section(this_insights, last_insights):
    lines = ["<b>🧭 Окно бронирования, отмены и каналы продаж (последняя полная неделя)</b>"]

    lt_this = this_insights["avg_lead_time"]
    lt_last = last_insights["avg_lead_time"]
    lines.append(
        "Среднее окно бронирования: "
        f"{f'{lt_this:.1f} дн.' if lt_this is not None else 'н/д'} vs "
        f"{f'{lt_last:.1f} дн.' if lt_last is not None else 'н/д'} год назад"
    )

    cr_this = this_insights["cancel_rate"]
    cr_last = last_insights["cancel_rate"]
    lines.append(f"Доля отменённых броней: {_fmt_pct(cr_this)} vs {_fmt_pct(cr_last)} год назад")

    lines.append("")
    lines.append("<u>Каналы продаж (доля выручки, тек. неделя)</u>")
    total_rev = this_insights["total_revenue"]
    if total_rev:
        top_channels = sorted(
            this_insights["channel_revenue"].items(), key=lambda kv: kv[1], reverse=True
        )[:5]
        for channel, revenue in top_channels:
            share = 100 * revenue / total_rev
            lines.append(f"{channel}: {format_money(revenue)} ({share:.1f}%)")
    else:
        lines.append("Нет данных о выручке по каналам.")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Секция 5: итоговый вывод (traffic light)
# --------------------------------------------------------------------------

def build_executive_summary(report_date, occ_delta_last_week, avg_pace_gap, std_occ_delta, cancel_rate_delta):
    lines = [f"<b>🎯 Эффективность продаж на {report_date.isoformat()}</b>"]

    signals = [s for s in (occ_delta_last_week, avg_pace_gap, std_occ_delta) if s is not None]

    if signals:
        avg_signal = sum(signals) / len(signals)
        if avg_signal > 2:
            verdict = "🟢 Продажи опережают прошлый год"
        elif avg_signal < -2:
            verdict = "🔴 Продажи отстают от прошлого года - нужны меры (акции, доп. каналы, гибкая цена)"
        else:
            verdict = "🟡 Продажи примерно на уровне прошлого года"
    else:
        verdict = "⚪ Недостаточно данных для вывода"

    lines.append(verdict)

    parts = []
    if occ_delta_last_week is not None:
        parts.append(f"загрузка пред. недели {occ_delta_last_week:+.1f} п.п.")
    if avg_pace_gap is not None:
        parts.append(f"темп на 6 нед. вперёд {avg_pace_gap:+.1f} п.п.")
    if std_occ_delta is not None:
        parts.append(f"загрузка с начала сезона {std_occ_delta:+.1f} п.п.")
    if cancel_rate_delta is not None:
        parts.append(f"отмены {cancel_rate_delta:+.1f} п.п.")
    if parts:
        lines.append("Сигналы (год к году): " + "; ".join(parts) + ".")

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

    today = date.today()

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
        total_rooms = None
        room_id_to_type = {}
        rooms_per_type = {}

    try:
        room_type_meta = get_room_type_meta(token, property_id)
    except Exception as e:
        print(f"[!] Не удалось получить категории номеров: {e}", file=sys.stderr)
        room_type_meta = {}

    this_week = get_last_full_week(today)
    last_year_week = (this_week[0] - timedelta(days=364), this_week[1] - timedelta(days=364))

    # --- динамика загрузки последней полной недели (для итогового вывода) ---
    occ_delta_last_week = None
    try:
        this_sums, _ = aggregate_week(token, property_id, *this_week)
        last_sums, _ = aggregate_week(token, property_id, *last_year_week)
        if total_rooms:
            days_this = (this_week[1] - this_week[0]).days + 1
            days_last = (last_year_week[1] - last_year_week[0]).days + 1
            avail_this = max(total_rooms * days_this - this_sums.get("closed_room_nights", 0), 1)
            avail_last = max(total_rooms * days_last - last_sums.get("closed_room_nights", 0), 1)
            occ_this_w = 100 * this_sums.get("occupied_room_nights", 0) / avail_this
            occ_last_w = 100 * last_sums.get("occupied_room_nights", 0) / avail_last
            occ_delta_last_week = occ_this_w - occ_last_w
    except Exception as e:
        print(f"[!] Не удалось посчитать динамику загрузки прошлой недели: {e}", file=sys.stderr)

    # --- Season-to-Date ---
    try:
        std_section, std_occ_delta = build_season_to_date_section(token, property_id, total_rooms, today)
    except Exception as e:
        std_section, std_occ_delta = f"<b>🌞 Сезон к текущей дате</b>\n⚠️ Ошибка: {e}", None
        print(f"[!] Ошибка секции Season-to-Date: {e}", file=sys.stderr)

    # --- ADR / RevPAR по категориям ---
    try:
        this_breakdown, _ = get_room_type_breakdown(token, property_id, room_id_to_type, *this_week)
        last_breakdown, _ = get_room_type_breakdown(token, property_id, room_id_to_type, *last_year_week)
        adr_section = build_adr_revpar_by_category_section(
            this_breakdown, last_breakdown, room_type_meta, rooms_per_type, this_week, last_year_week
        )
    except Exception as e:
        adr_section = f"<b>💵 ADR / RevPAR по категориям</b>\n⚠️ Ошибка: {e}"
        print(f"[!] Ошибка секции ADR/RevPAR: {e}", file=sys.stderr)

    # --- OTB pace curve на 1-6 недель вперёд ---
    try:
        pace_section, avg_pace_gap = build_pace_curve_section(token, property_id, total_rooms, reference_date=today)
    except Exception as e:
        pace_section, avg_pace_gap = f"<b>📈 Кривая бронирований</b>\n⚠️ Ошибка: {e}", None
        print(f"[!] Ошибка секции pace curve: {e}", file=sys.stderr)

    # --- окно бронирования, отмены, каналы ---
    cancel_rate_delta = None
    try:
        this_insights = collect_week_insights(token, property_id, *this_week)
        last_insights = collect_week_insights(token, property_id, *last_year_week)
        insight_section = build_lead_cancel_channel_section(this_insights, last_insights)
        if this_insights["cancel_rate"] is not None and last_insights["cancel_rate"] is not None:
            cancel_rate_delta = this_insights["cancel_rate"] - last_insights["cancel_rate"]
    except Exception as e:
        insight_section = f"<b>🧭 Окно бронирования, отмены и каналы</b>\n⚠️ Ошибка: {e}"
        print(f"[!] Ошибка секции lead time/отмены/каналы: {e}", file=sys.stderr)

    # --- конверсия ближайших будни/выходные по категориям ---
    try:
        conversion_section = build_occupancy_conversion_section(
            token, property_id, room_type_meta, rooms_per_type, room_id_to_type, today
        )
    except Exception as e:
        conversion_section = f"<b>📊 Конверсия дат в брони</b>\n⚠️ Ошибка: {e}"
        print(f"[!] Ошибка секции конверсии дат: {e}", file=sys.stderr)

    summary_section = build_executive_summary(
        today, occ_delta_last_week, avg_pace_gap, std_occ_delta, cancel_rate_delta
    )

    full_report = "\n\n".join([
        summary_section, std_section, adr_section, pace_section, insight_section, conversion_section
    ])

    print(full_report.replace("<b>", "").replace("</b>", "").replace("<u>", "").replace("</u>", ""))

    if not tg_token or not tg_chat_id:
        print("\n[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы - "
              "сообщение не отправлено, только выведено выше.", file=sys.stderr)
        return

    send_telegram_message(tg_token, tg_chat_id, full_report)
    print("\n[OK] Отправлено в Telegram.")


if __name__ == "__main__":
    main()

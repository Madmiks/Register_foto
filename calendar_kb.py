import calendar
from datetime import datetime, date
from zoneinfo import ZoneInfo # 🔹 ИМПОРТ ТАЙМЗОНЫ
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

MONTH_NAMES = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
STATUS_EMOJI = {"free": "🟢", "pending_admin": "🟡", "pending_payment": "🟠", "booked": "✅", "empty": "⚪"}
STATUS_HINT = {"free": "свободно", "pending_admin": "проверка", "pending_payment": "оплата", "booked": "занято", "empty": "нет слотов"}

TZ = ZoneInfo("Europe/Moscow")
def build_calendar(year: int, month: int, date_statuses: dict, mode: str = "client"):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    today = datetime.now(TZ).date()

    # Навигация
    can_prev = not (mode == "client" and (year < today.year or (year == today.year and month <= today.month)))
    can_next = not (mode == "client" and (year > today.year + 1 or (year == today.year + 1 and month > 6)))

    kb.inline_keyboard.append([
        InlineKeyboardButton(text="◀️", callback_data=f"cal:prev:{year}:{month}:{mode}" if can_prev else "ignore"),
        InlineKeyboardButton(text=f"{MONTH_NAMES[month - 1]} {year}", callback_data="ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"cal:next:{year}:{month}:{mode}" if can_next else "ignore")
    ])

    kb.inline_keyboard.append([InlineKeyboardButton(text=wd, callback_data="ignore") for wd in WEEKDAYS])

    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text="  ", callback_data="ignore"))
            else:
                date_iso = f"{year}-{month:02d}-{day:02d}"
                date_obj = date(year, month, day)

                if mode == "client" and date_obj < today:
                    row.append(InlineKeyboardButton(text="•", callback_data="ignore"))
                    continue

                info = date_statuses.get(date_iso)
                if info:
                    status, count = info["status"], info["count"]
                    emoji = STATUS_EMOJI.get(status, "⚪")
                    hint = STATUS_HINT.get(status, "")

                    if mode == "client" and status == "free":
                        row.append(InlineKeyboardButton(text=f"{emoji} {day}", callback_data=f"day:{date_iso}"))
                    else:
                        text = f"{emoji} {day}\n({count} {hint})" if mode == "admin" else f"{emoji} {day}"
                        row.append(InlineKeyboardButton(
                            text=text,
                            callback_data=f"admin_day:{date_iso}" if mode == "admin" else "ignore"
                        ))
                else:
                    text = "⚪ •\n(нет слотов)" if mode == "admin" else "⚪ •"
                    row.append(InlineKeyboardButton(
                        text=text,
                        callback_data=f"admin_day:{date_iso}" if mode == "admin" else "ignore"
                    ))
        kb.inline_keyboard.append(row)

    kb.inline_keyboard.append(
        [InlineKeyboardButton(text="🔙 В меню", callback_data="main:client" if mode == "client" else "main:admin")]
    )
    return kb
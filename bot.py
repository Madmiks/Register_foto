from aiohttp import web
import uuid
import asyncio
import os
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo  # 🔹 ИМПОРТ ДЛЯ ТАЙМЗОНЫ
from dotenv import load_dotenv

from yookassa import Configuration, Payment, Refund
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError  # 🔹 ИМПОРТ ОШИБОК TELEGRAM
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database
from calendar_kb import build_calendar
from scheduler_tasks import check_payment_timeout

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# 🔹 ФИКСИРУЕМ ЧАСОВОЙ ПОЯС
TZ = ZoneInfo("Europe/Moscow")


class AddSlotState(StatesGroup):
    picking_date = State()
    entering_times = State()


class AdminSettingsState(StatesGroup):
    changing_price = State()
    changing_timeout = State()


class CopyScheduleState(StatesGroup):
    picking_source = State()
    picking_target = State()


# ================= Клавиатуры (БЕЗ ИЗМЕНЕНИЙ) =================
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Записаться"), KeyboardButton(text="📋 Мои записи")],
        [KeyboardButton(text="📞 Контакты")]
    ], resize_keyboard=True)


def times_kb(slots):
    builder = []
    for sid, dt_str, status in slots:
        if status == "free":
            time_part = dt_str.split()[-1]
            builder.append([InlineKeyboardButton(text=f"🟢 {time_part}", callback_data=f"book:{sid}")])
    return InlineKeyboardMarkup(inline_keyboard=builder) if builder else None


def payment_kb(slot_id: int, payment_url: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"paycheck:{slot_id}")]
    ])


def admin_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Добавить слот"), KeyboardButton(text="🗑 Удалить слот")],
        [KeyboardButton(text="📋 Расписание"), KeyboardButton(text="👥 Записи")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="🔄 Копировать слоты"), KeyboardButton(text="🚪 Выйти")]
    ], resize_keyboard=True)


def admin_decision_kb(slot_id: int, user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve:{slot_id}:{user_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{slot_id}:{user_id}")]
    ])


def delete_slots_kb(slots):
    STATUS_EMOJI = {"free": "🟢", "pending_admin": "🟡", "pending_payment": "🟠", "booked": "✅"}
    builder = []
    for sid, dt, status, uname, fname in slots:
        emoji = STATUS_EMOJI.get(status, "⚪")
        builder.append([InlineKeyboardButton(text=f"{emoji} {dt} ({status})", callback_data=f"del:{sid}")])
    builder.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main:admin")])
    return InlineKeyboardMarkup(inline_keyboard=builder)


def status_legend():
    return "📊 Статусы:\n🟢 свободно | 🟡 проверка | 🟠 оплата | ✅ занято | ⚪ нет записей"


# ================= Клиент =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await database.add_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    price = await database.get_setting("price") or "4500₽"
    welcome_text = (
        f"👋 Здравствуйте, {message.from_user.first_name}!\n\n"
        f"📸 Добро пожаловать в систему записи на фотосессию!\n\n"
        f"🔄 Как работает система:\n"
        f"• Свободный специалист приезжает к вам в нужную дату и делает фото.\n"
        f"• Уже через 10-20 минут фото будут в руках у наших специалистов по обработке.\n"
        f"• Это значительно экономит ваше время!\n"
        f"• Обычно от съёмки до готового результата стандартная работа занимает 1-2 дня.\n\n"
        f"💰 Цена: {price}\n\n"
        f"Нажмите 📅 «Записаться», чтобы выбрать удобное время."
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_kb())


@dp.message(F.text == "📅 Записаться")
async def open_calendar(message: Message):
    now = datetime.now(TZ)  # 🔹 ТАЙМЗОНА
    statuses = await database.get_date_statuses(now.year, now.month)
    await message.answer("🗓 Выберите дату:\n" + status_legend(), parse_mode="HTML",
                         reply_markup=build_calendar(now.year, now.month, statuses, "client"))


@dp.callback_query(F.data.startswith("cal:"))
async def navigate_calendar(callback: CallbackQuery):
    _, action, year, month, mode = callback.data.split(":")
    year, month = int(year), int(month)
    if action == "prev":
        month -= 1
        if month == 0: month, year = 12, year - 1
    else:
        month += 1
        if month == 13: month, year = 1, year + 1
    statuses = await database.get_date_statuses(year, month)
    await callback.message.edit_reply_markup(reply_markup=build_calendar(year, month, statuses, mode))
    await callback.answer()


@dp.callback_query(F.data.startswith("day:"))
async def select_day(callback: CallbackQuery):
    _, date_iso = callback.data.split(":", maxsplit=1)
    slots = await database.get_slots_by_date(date_iso)
    free = [(s, d, st) for s, d, st in slots if st == "free"]
    if not free:
        await callback.answer("❌ На эту дату нет свободных слотов", show_alert=True)
        return
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    await callback.message.edit_text(f"📅 {dt.strftime('%d.%m.%Y')}\nВыберите время:", reply_markup=times_kb(free))
    await callback.answer()


@dp.callback_query(F.data.startswith("book:"))
async def process_booking(callback: CallbackQuery):
    _, slot_id = callback.data.split(":", maxsplit=1)
    slot_id = int(slot_id)
    user = callback.from_user

    # ПРОВЕРКА: Удалось ли забронировать?
    success = await database.book_slot_pending_admin(slot_id, user.id)

    if not success:
        await callback.answer("❌ Извините, этот слот только что был занят другим пользователем.", show_alert=True)
        # Обновляем сообщение, чтобы убрать занятый слот
        await select_day(callback)
        return

    await callback.message.edit_text("✅ Заявка отправлена! Ждите подтверждения.")

    details = await database.get_slot_details(slot_id)
    slot_dt = details[0] if details else "Неизвестно"

    user_tag = f"@{user.username}" if user.username else user.first_name

    # 🔹 БЕЗОПАСНАЯ ОТПРАВКА АДМИНУ
    try:
        await bot.send_message(ADMIN_ID,
                               f"🔔 Новая заявка\n👤 {user_tag} (ID: {user.id})\n📅 {slot_dt}\n🎫 Слот: {slot_id}",
                               parse_mode="HTML", reply_markup=admin_decision_kb(slot_id, user.id))
    except TelegramAPIError as e:
        print(f"Ошибка отправки сообщения админу: {e}")

    await callback.answer()


@dp.callback_query(F.data == "main:client")
async def client_main(callback: CallbackQuery):
    await callback.message.answer("Главное меню", reply_markup=main_kb())
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass # Игнорируем ошибку, если сообщение нельзя удалить
    await callback.answer()


# ================= Админ =================
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 Админ-панель", reply_markup=admin_main_kb())


@dp.message(F.text == "🚪 Выйти", F.from_user.id == ADMIN_ID)
async def exit_admin(message: Message, state: FSMContext):
    # 🔹 ИСПРАВЛЕНИЕ 3: Очищаем любые зависшие состояния (например, ввод цены или слотов)
    await state.clear()
    await message.answer("Вы вышли.", reply_markup=main_kb())


@dp.message(F.text == "📋 Расписание", F.from_user.id == ADMIN_ID)
async def show_schedule(message: Message, state: FSMContext):
    await state.clear() # 🔹 Очищаем состояние
    schedule = await database.get_schedule()
    if not schedule:
        await message.answer("📭 Расписание пусто.")
        return

    text = "🗓 Расписание:\n🟢 свободно | 🟡 проверка | 🟠 оплата | ✅ занято\n\n"
    STATUS_EMOJI = {"free": "🟢", "pending_admin": "🟡", "pending_payment": "🟠", "booked": "✅"}

    for sid, dt_raw, status, uname, fname in schedule:
        # dt_raw сейчас '2024-04-14 10:00:00'
        # Превращаем в красивый формат перед отправкой:
        dt_pretty = datetime.fromisoformat(dt_raw).strftime("%d.%m.%Y %H:%M")

        user_tag = f"@{uname}" if uname else (fname or "Не указан")
        emoji = STATUS_EMOJI.get(status, "⚪")
        text += f"{emoji} {dt_pretty} — {status} {user_tag if status != 'free' else ''}\n"

    await message.answer(text)


@dp.message(F.text == "👥 Записи", F.from_user.id == ADMIN_ID)
async def show_bookings(message: Message, state: FSMContext):
    await state.clear() # 🔹 Очищаем состояние
    bookings = await database.get_booked_details()
    if not bookings:
        await message.answer("📭 Нет активных записей.")
        return

    text = "📋 Активные записи:\n\n"
    status_map = {"pending_admin": "🟡", "pending_payment": "🟠", "booked": "✅"}
    kb = InlineKeyboardMarkup(inline_keyboard=[])

    for sid, dt, status, uname, fname, uid in bookings:
        real_uid = uid if uid else "Не указан"
        user_tag = f"@{uname}" if uname else (fname or f"ID: {real_uid}")
        emoji = status_map.get(status, "⚪")
        text += f"{emoji} {dt}\n👤 {user_tag} (TG ID: {real_uid})\n🎫 Слот #{sid}\n\n"
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"admin_cancel:{sid}")
        ])

    await message.answer(text.strip(), reply_markup=kb)


@dp.callback_query(F.data.startswith("admin_cancel:"), F.from_user.id == ADMIN_ID)
async def admin_cancel_booking(callback: CallbackQuery):
    slot_id = int(callback.data.split(":")[1])

    success, old_status, user_id = await database.admin_cancel_booking(slot_id)
    if not success:
        await callback.answer("❌ Слот не найден.", show_alert=True)
        return

    refund_info = ""
    # 1. Логика возврата
    if old_status == "booked":
        payment_id = await database.get_payment_id(slot_id)
        if payment_id:
            try:
                # 🔹 ИСПРАВЛЕНИЕ 2: Асинхронный вызов к API ЮKassa
                payment = await asyncio.to_thread(Payment.find_one, payment_id)

                if payment.status == 'succeeded':
                    refund_data = {
                        "amount": payment.amount,
                        "payment_id": payment_id,
                        "description": f"Возврат: Слот #{slot_id}"
                    }
                    # 🔹 ИСПРАВЛЕНИЕ 2: Асинхронное создание возврата
                    await asyncio.to_thread(Refund.create, refund_data, uuid.uuid4())
                    refund_info = "\n💸 Деньги возвращены через ЮKassa."

            except Exception as e:
                refund_info = f"\n⚠️ Ошибка возврата (Сбой ЮКассы): {e}"
                print(f"Ошибка возврата {payment_id}: {e}")

    # 2. Уведомление клиента
    if user_id:
        try:
            status_msg = "была отменена администратором"
            await bot.send_message(user_id, f"⚠️ Ваша запись {status_msg}.{refund_info}")
        except TelegramAPIError:
            print(f"Пользователь {user_id} заблокировал бота, уведомление об отмене не отправлено.")

    # 3. Очистка планировщика
    job_id = f"to:{slot_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    # 4. Обновление интерфейса админа
    await callback.answer(f"✅ Готово.{refund_info}", show_alert=True)
    await show_bookings(callback.message)
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass  # Игнорируем ошибку, если сообщение нельзя удалить


@dp.message(F.text == "🗑 Удалить слот", F.from_user.id == ADMIN_ID)
async def del_menu(message: Message, state: FSMContext):
    await state.clear() # 🔹 Очищаем состояние
    schedule = await database.get_schedule()
    if schedule:
        await message.answer("Выберите слот:", reply_markup=delete_slots_kb(schedule))
    else:
        await message.answer("Нет слотов.")


@dp.callback_query(F.data.startswith("del:"))
async def delete_slot(callback: CallbackQuery):
    _, sid = callback.data.split(":", maxsplit=1)
    await database.delete_slot(int(sid))
    await callback.answer(f"✅ Слот {sid} удалён.")
    schedule = await database.get_schedule()
    if schedule:
        try:
            await callback.message.edit_reply_markup(reply_markup=delete_slots_kb(schedule))
        except Exception:
            pass
    else:
        await callback.message.edit_text("Нет доступных слотов для удаления.", reply_markup=None)


@dp.callback_query(F.data == "main:admin")
async def admin_main(callback: CallbackQuery):
    # 🔹 БЕЗОПАСНОЕ УДАЛЕНИЕ СООБЩЕНИЯ
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass  # Игнорируем ошибку, если сообщение нельзя удалить

    await callback.message.answer("🛠 Админ-панель", reply_markup=admin_main_kb())
    await callback.answer()


@dp.message(F.text == "➕ Добавить слот", F.from_user.id == ADMIN_ID)
async def admin_add_start(message: Message, state: FSMContext):
    now = datetime.now(TZ)  # 🔹 ТАЙМЗОНА
    statuses = await database.get_date_statuses(now.year, now.month)
    await message.answer("🗓 Выберите дату:\n" + status_legend(), parse_mode="HTML",
                         reply_markup=build_calendar(now.year, now.month, statuses, "admin"))
    await state.set_state(AddSlotState.picking_date)


@dp.callback_query(AddSlotState.picking_date, F.data.startswith("admin_day:"))
async def admin_pick_date(callback: CallbackQuery, state: FSMContext):
    _, date_iso = callback.data.split(":", maxsplit=1)
    await state.update_data(date=date_iso)
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    await callback.message.edit_text(f"📅 {dt.strftime('%d.%m.%Y')}\nВведите время через запятую (14:00, 16:00):")
    await state.set_state(AddSlotState.entering_times)
    await callback.answer()


@dp.message(AddSlotState.entering_times, F.from_user.id == ADMIN_ID)
async def admin_save_slots(message: Message, state: FSMContext):
    data = await state.get_data()
    date_iso = data['date']

    # Отфильтровываем пустые строки, если админ случайно ввел "14:00, "
    raw_times = [t.strip() for t in message.text.split(',') if t.strip()]

    if not raw_times:
        await message.answer("❌ Вы не ввели время. Попробуйте еще раз (например: 14:00, 16:00):")
        return

    valid_dt_objects = []

    # Пытаемся распарсить каждое введенное время
    try:
        for t in raw_times:
            full_dt = f"{date_iso} {t}"
            # Если время некорректное (например 25:00 или 14:oo), тут вылетит ValueError
            dt_obj = datetime.strptime(full_dt, "%Y-%m-%d %H:%M")
            valid_dt_objects.append(dt_obj)
    except ValueError:
        await message.answer(
            "❌ Ошибка в формате времени!\n"
            "Пожалуйста, используйте формат ЧЧ:ММ (например, 14:00, 16:30) и проверьте, что такое время существует.\n\n"
            "Введите время заново:"
        )
        return  # Прерываем функцию, state не очищаем, ждем новую попытку от админа

    # Если все проверки пройдены успешно, сохраняем слоты в БД
    for dt_obj in valid_dt_objects:
        await database.add_slot_to_db(dt_obj)

    await message.answer(f"✅ Успешно добавлено слотов: {len(valid_dt_objects)}", reply_markup=admin_main_kb())
    await state.clear()


@dp.callback_query(F.data.startswith("approve:"))
async def approve_booking(callback: CallbackQuery):
    _, slot_id, user_id = callback.data.split(":")
    slot_id, user_id = int(slot_id), int(user_id)

    # Выносим настройки цены
    price_str = await database.get_setting("price") or "4500"
    price_num = int("".join(filter(str.isdigit, price_str)))
    timeout_hours = int(await database.get_setting("payment_timeout_hours") or 3)

    # 🔹 ПРОДАКШЕН: Выполняем синхронный запрос в отдельном потоке
    try:
        payment_data = {
            "amount": {"value": f"{price_num}.00", "currency": "RUB"},
            "confirmation": {
                "type": "redirect",
                "return_url": f"https://t.me/{(await bot.get_me()).username}"
            },
            "capture": True,
            "description": f"Оплата фотосессии (Слот #{slot_id})"
        }
        # Используем to_thread, чтобы не блокировать бота
        payment = await asyncio.to_thread(Payment.create, payment_data, uuid.uuid4())
    except Exception as e:
        print(f"YooKassa error: {e}")
        await callback.answer("❌ Ошибка платежной системы.", show_alert=True)
        return

    payment_url = payment.confirmation.confirmation_url
    await database.approve_slot_booking(slot_id, payment.id, timeout_hours)

    await callback.message.edit_text(f"✅ Заявка {slot_id} одобрена. Ссылка на оплату отправлена клиенту.",
                                     reply_markup=None)

    run_date = datetime.now(TZ) + timedelta(hours=timeout_hours)  # 🔹 ТАЙМЗОНА
    scheduler.add_job(check_payment_timeout, 'date', run_date=run_date,
                      args=[bot, slot_id], id=f"to:{slot_id}", replace_existing=True)

    # 🔹 БЕЗОПАСНАЯ ОТПРАВКА КЛИЕНТУ
    try:
        await bot.send_message(user_id,
                               f"🎉 Ваша заявка одобрена!\nК оплате: {price_num} RUB.\nУ вас {timeout_hours} ч. на оплату.",
                               reply_markup=payment_kb(slot_id, payment_url))
    except TelegramAPIError:
        print(f"Не удалось отправить ссылку на оплату пользователю {user_id}")
        await bot.send_message(ADMIN_ID,
                               f"⚠️ Пользователь {user_id} заблокировал бота. Ссылка на оплату не доставлена.")

    await callback.answer()


@dp.callback_query(F.data.startswith("reject:"))
async def reject_booking(callback: CallbackQuery):
    _, slot_id, user_id = callback.data.split(":")
    slot_id, user_id = int(slot_id), int(user_id)
    await database.reject_slot_booking(slot_id)
    await callback.message.edit_text(f"❌ Заявка {slot_id} отклонена.", reply_markup=None)

    # 🔹 БЕЗОПАСНАЯ ОТПРАВКА
    try:
        await bot.send_message(user_id, "Ваша заявка отклонена.")
    except TelegramAPIError:
        pass

    await callback.answer()


@dp.message(F.text == "🔄 Копировать слоты", F.from_user.id == ADMIN_ID)
async def start_copy_schedule(message: Message, state: FSMContext):
    now = datetime.now(TZ)  # 🔹 ТАЙМЗОНА
    statuses = await database.get_date_statuses(now.year, now.month)
    await message.answer("📅 Выберите исходную дату, откуда копировать слоты:",
                         reply_markup=build_calendar(now.year, now.month, statuses, "admin"))
    await state.set_state(CopyScheduleState.picking_source)


@dp.callback_query(CopyScheduleState.picking_source, F.data.startswith("admin_day:"))
async def copy_pick_source(callback: CallbackQuery, state: FSMContext):
    _, date_iso = callback.data.split(":", maxsplit=1)  # Получаем '2024-04-14'
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    source_date_pretty = dt.strftime("%d.%m.%Y")

    # Передаем ISO-дату напрямую в функцию БД
    times = await database.get_free_times_for_date(date_iso)
    if not times:
        await callback.answer(f"❌ На {source_date_pretty} нет свободных слотов для копирования.", show_alert=True)
        return

    # Сохраняем именно ISO формат
    await state.update_data(source_date=date_iso)

    next_week = dt + timedelta(days=7)
    next_week_iso = next_week.strftime("%Y-%m-%d")
    next_week_pretty = next_week.strftime("%d.%m.%Y")

    # В callback_data кнопки тоже кладем ISO
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"➕ Копировать на {next_week_pretty}",
                              callback_data=f"copy_target:{next_week_iso}")]
    ])

    await callback.message.edit_text(
        f"📅 Исходная дата: {source_date_pretty}\n⏰ Найдено слотов: {len(times)} ({', '.join(times)})\n\n"
        f"Введите целевую дату (ДД.ММ.ГГГГ) или нажмите кнопку +7 дней:",
        reply_markup=kb
    )
    await state.set_state(CopyScheduleState.picking_target)
    await callback.answer()


@dp.message(CopyScheduleState.picking_target, F.from_user.id == ADMIN_ID)
async def copy_input_target(message: Message, state: FSMContext):
    target_date_str = message.text.strip()
    try:
        # Конвертируем ввод пользователя (ДД.ММ.ГГГГ) в ISO (ГГГГ-ММ-ДД)
        dt_target = datetime.strptime(target_date_str, "%d.%m.%Y")
        target_date_iso = dt_target.strftime("%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ (например, 25.04.2024)")
        return
    await execute_copy_schedule(message, state, target_date_iso)


@dp.callback_query(CopyScheduleState.picking_target, F.data.startswith("copy_target:"))
async def copy_select_target(callback: CallbackQuery, state: FSMContext):
    target_date_iso = callback.data.split(":", 1)[1]
    # Для визуального подтверждения конвертируем обратно в RU формат
    dt_target = datetime.strptime(target_date_iso, "%Y-%m-%d")
    await callback.message.edit_text(f"🔄 Копирую слоты на {dt_target.strftime('%d.%m.%Y')}...")
    await execute_copy_schedule(callback.message, state, target_date_iso)
    await callback.answer()


async def execute_copy_schedule(message: Message, state: FSMContext, target_date_iso: str):
    data = await state.get_data()
    source_date_iso = data.get("source_date")  # Это уже ISO строка

    # Отправляем в базу чистые ISO даты
    count = await database.copy_slots_to_date(source_date_iso, target_date_iso)

    # Форматируем даты для финального сообщения
    source_pretty = datetime.strptime(source_date_iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    target_pretty = datetime.strptime(target_date_iso, "%Y-%m-%d").strftime("%d.%m.%Y")

    await state.clear()
    await message.answer(f"✅ Успешно скопировано {count} слотов с {source_pretty} на {target_pretty}.",
                         reply_markup=admin_main_kb())


@dp.callback_query(F.data.startswith("paycheck:"))
async def check_payment(callback: CallbackQuery):
    slot_id = int(callback.data.split(":")[1])
    payment_id = await database.get_payment_id(slot_id)

    if not payment_id:
        await callback.answer("❌ Платеж не найден.", show_alert=True)
        return

    # 🔹 ПРОДАКШЕН: Проверка статуса в потоке
    try:
        payment = await asyncio.to_thread(Payment.find_one, payment_id)
    except Exception as e:
        await callback.answer("⚠️ Ошибка связи с банком.", show_alert=True)
        return

    if payment.status == 'succeeded':
        await database.confirm_payment(slot_id)
        # Чтобы избежать ошибки "Message is not modified", используем try
        try:
            await callback.message.edit_text("✅ Оплата успешно получена! Ждём вас 📸", reply_markup=None)
        except Exception:
            pass

        if scheduler.get_job(f"to:{slot_id}"):
            scheduler.remove_job(f"to:{slot_id}")
    elif payment.status == 'pending':
        await callback.answer(
            "⏳ Оплата ещё не поступила. Если вы уже оплатили, подождите пару минут и попробуйте снова.",
            show_alert=True)
    else:
        await callback.answer(f"❌ Статус платежа: {payment.status}. Возможно, он был отменен.", show_alert=True)


# 🔹 СТАТИСТИКА
@dp.message(F.text == "📊 Статистика", F.from_user.id == ADMIN_ID)
async def show_stats(message: Message, state: FSMContext):
    await state.clear() # 🔹 Очищаем состояние
    stats = await database.get_statistics()
    text = (f"📊 Статистика бота:\n"
            f"👤 Всего пользователей: {stats['users']}\n"
            f"🟢 Свободных слотов: {stats['free']}\n"
            f"🟡 Ожидает проверки админа: {stats['pending_admin']}\n"
            f"🟠 Ожидает оплаты: {stats['pending_pay']}\n"
            f"✅ Записано (оплачено): {stats['booked']}\n"
            f"💰 Текущая цена: {stats['price']}\n"
            f"💵 Потенциальная выручка: {stats['revenue']}₽")
    await message.answer(text)


# 🔹 НАСТРОЙКИ (Цена и Время)
@dp.message(F.text == "⚙️ Настройки", F.from_user.id == ADMIN_ID)
async def settings_menu(message: Message, state: FSMContext):
    await state.clear() # 🔹 Очищаем состояние
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Изменить цену", callback_data="set:price")],
        [InlineKeyboardButton(text="⏳ Изменить время оплаты (ч)", callback_data="set:timeout")]
    ])
    await message.answer("⚙️ Выберите настройку:", reply_markup=kb)


@dp.callback_query(F.data == "set:price", F.from_user.id == ADMIN_ID)
async def ask_price(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("💰 Введите новую цену (например, 5000₽ или 5000):")
    await state.set_state(AdminSettingsState.changing_price)
    await callback.answer()


@dp.message(AdminSettingsState.changing_price, F.from_user.id == ADMIN_ID)
async def save_price(message: Message, state: FSMContext):
    new_price = message.text.strip()
    if not new_price:
        await message.answer("❌ Цена не может быть пустой.")
        return
    await database.set_setting("price", new_price)
    await message.answer(f"✅ Цена изменена на: {new_price}", reply_markup=admin_main_kb())
    await state.clear()


@dp.callback_query(F.data == "set:timeout", F.from_user.id == ADMIN_ID)
async def ask_timeout(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("⏳ Введите количество часов для оплаты (целое число):")
    await state.set_state(AdminSettingsState.changing_timeout)
    await callback.answer()


@dp.message(AdminSettingsState.changing_timeout, F.from_user.id == ADMIN_ID)
async def save_timeout(message: Message, state: FSMContext):
    try:
        hours = int(message.text.strip())
        if hours <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное положительное число часов.")
        return
    await database.set_setting("payment_timeout_hours", str(hours))
    await message.answer(f"✅ Время на оплату изменено на {hours} ч.", reply_markup=admin_main_kb())
    await state.clear()


@dp.message(F.text == "📞 Контакты")
async def show_contacts(message: Message):
    contacts_text = (
        "📞 Связаться с фотографом: Олег\n\n"
        "Имя: Олег\n\n"
        "📱 Telegram: @Fastphoto4u\n"
        "⏰ Время работы:\n"
        "Пн–Вс: 10:00 – 22:00\n\n"
        "💬 Пишите в любое удобное время, отвечаем в течение часа!"
    )
    await message.answer(contacts_text, parse_mode="HTML")


@dp.message(F.text == "📋 Мои записи")
async def show_my_bookings(message: Message):
    bookings = await database.get_user_bookings(message.from_user.id)

    if not bookings:
        await message.answer("📭 У вас пока нет активных записей.\nНажмите 📅 «Записаться», чтобы выбрать время.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    text = "📅 Ваши текущие записи:\n\n"
    status_map = {
        "pending_admin": "🟡 Ожидает проверки админа",
        "pending_payment": "🟠 Ожидает оплаты",
        "booked": "✅ Подтверждено и оплачено"
    }

    for sid, dt_str, status, reserved_until in bookings:
        status_text = status_map.get(status, f"⚪ {status}")
        text += f"🔹 {dt_str}\n{status_text}\n\n"

        if status in ("pending_admin", "pending_payment"):
            kb.inline_keyboard.append([
                InlineKeyboardButton(text="❌ Отменить эту запись", callback_data=f"cancel:{sid}")
            ])

    await message.answer(text.strip(), reply_markup=kb if kb.inline_keyboard else None)


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_booking(callback: CallbackQuery):
    _, slot_id_str = callback.data.split(":")
    slot_id = int(slot_id_str)
    user_id = callback.from_user.id

    success = await database.cancel_user_booking(slot_id, user_id)

    if success:
        # 🔹 БЕЗОПАСНАЯ ОТПРАВКА АДМИНУ
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Пользователь {user_id} самостоятельно отменил запись на слот #{slot_id}."
            )
        except TelegramAPIError:
            pass

        if scheduler.get_job(f"to:{slot_id}"):
            scheduler.remove_job(f"to:{slot_id}")
        await callback.answer("✅ Ваша запись успешно отменена.", show_alert=True)
    else:
        await callback.answer("❌ Нельзя отменить оплаченную запись или запись уже не существует.", show_alert=True)

    bookings = await database.get_user_bookings(user_id)
    if not bookings:
        await callback.message.edit_text(
            "📭 У вас пока нет активных записей.\nНажмите 📅 «Записаться», чтобы выбрать время.", reply_markup=None)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    text = "📅 Ваши текущие записи:\n\n"
    status_map = {
        "pending_admin": "🟡 Ожидает проверки админа",
        "pending_payment": "🟠 Ожидает оплаты",
        "booked": "✅ Подтверждено и оплачено"
    }

    for sid, dt_str, status, reserved_until in bookings:
        status_text = status_map.get(status, f"⚪ {status}")
        text += f"🔹 {dt_str}\n{status_text}\n\n"

        if status in ("pending_admin", "pending_payment"):
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"❌ Отменить слот #{sid}", callback_data=f"cancel:{sid}")
            ])

    try:
        await callback.message.edit_text(text.strip(), reply_markup=kb if kb.inline_keyboard else None)
    except Exception:
        pass


async def yookassa_webhook(request):
    """Обработчик входящих запросов от ЮKassa"""
    try:
        event_json = await request.json()
    except Exception:
        return web.Response(status=400)

    event_type = event_json.get('event')
    payment_obj = event_json.get('object', {})
    payment_id = payment_obj.get('id')

    if not payment_id:
        return web.Response(status=400)

    if event_type == 'payment.succeeded':
        # 🔹 ИСПРАВЛЕНИЕ 1: Дополнительная валидация через API
        # Запрашиваем реальный статус у ЮKassa, чтобы исключить подделку запроса
        try:
            real_payment = await asyncio.to_thread(Payment.find_one, payment_id)
            if real_payment.status != 'succeeded':
                print(f"⚠️ Попытка обмана вебхука! Платеж {payment_id} по факту не оплачен.")
                return web.Response(status=200)
        except Exception as e:
            print(f"Ошибка проверки платежа {payment_id} в вебхуке: {e}")
            return web.Response(status=500) # В случае ошибки сети просим ЮКассу повторить запрос позже

        slot_id = await database.get_slot_by_payment_id(payment_id)

        if slot_id:
            # 1. Отмечаем слот как оплаченный в БД
            await database.confirm_payment(slot_id)

            # 2. Удаляем таймаут из планировщика
            if scheduler.get_job(f"to:{slot_id}"):
                scheduler.remove_job(f"to:{slot_id}")

            # 3. Уведомляем клиента
            details = await database.get_slot_details(slot_id)
            if details:
                user_id = details[4]
                if user_id:
                    try:
                        await bot.send_message(
                            user_id,
                            "✅ Оплата автоматически подтверждена! Ждём вас на фотосессию 📸"
                        )
                    except TelegramAPIError:
                        print(f"Не удалось отправить уведомление {user_id} (заблокировал бота)")

    # Всегда отвечаем 200 OK
    return web.Response(status=200)


async def start_webhook_server():
    """Запускает параллельный веб-сервер для прослушивания ЮKassa"""
    app = web.Application()
    app.router.add_post('/yookassa-webhook', yookassa_webhook)
    runner = web.AppRunner(app)
    await runner.setup()

    # Сервер будет слушать на порту 8080.
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    print("🌐 Webhook-сервер ЮKassa запущен на порту 8080")


async def main():
    await database.init_db()
    # 🔹 НОВАЯ ЗАДАЧА: Ежедневная очистка базы в 03:00 утра
    scheduler.add_job(
        database.delete_old_free_slots,
        'cron',
        hour=3,
        minute=0,
        id="daily_cleanup"
    )
    # 🔹 ИСПРАВЛЕНИЕ: Восстановление таймеров оплаты после перезапуска бота
    pending_slots = await database.get_pending_payment_slots()
    now = datetime.now(TZ)

    for sid, uid, reserved_until_str in pending_slots:
        if not reserved_until_str:
            continue

        try:
            # SQLite хранит объекты datetime как строки (формат ISO)
            # Переводим строку обратно в объект datetime и задаем таймзону
            reserved_until = datetime.fromisoformat(reserved_until_str)
            if reserved_until.tzinfo is None:
                reserved_until = reserved_until.replace(tzinfo=TZ)
        except ValueError:
            print(f"⚠️ Ошибка парсинга времени для слота #{sid}: {reserved_until_str}")
            continue

        if now >= reserved_until:
            # Время уже вышло (пока бот был выключен) — отменяем бронь сразу
            await check_payment_timeout(bot, sid)
        else:
            # Время еще есть — заново добавляем задачу в планировщик
            scheduler.add_job(
                check_payment_timeout,
                'date',
                run_date=reserved_until,
                args=[bot, sid],
                id=f"to:{sid}",
                replace_existing=True
            )
            print(f"🔄 Восстановлен таймер для слота #{sid} до {reserved_until.strftime('%H:%M')}")

    scheduler.start()

    # 🔹 Запускаем веб-сервер для приема вебхуков
    await start_webhook_server()
    print("🤖 Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

"""
Чтобы это заработало, ЮKassa должна знать, куда отправлять запросы.

Зайдите в личный кабинет ЮKassa.

Перейдите в раздел Интеграция -> HTTP-уведомления (Webhooks).

Добавьте URL вашего сервера: http://<IP-вашего-сервера>:8080/yookassa-webhook (или https://ваш-домен/yookassa-webhook, если настроен прокси).

Выберите событие для отправки: payment.succeeded (Платеж успешно завершен).
"""
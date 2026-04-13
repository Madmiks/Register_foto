import aiosqlite
import datetime
from zoneinfo import ZoneInfo

DB_NAME = "photo_bot.db"
TZ = ZoneInfo("Europe/Moscow")

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS Users (
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, role TEXT DEFAULT 'client')''')
        await db.execute('''CREATE TABLE IF NOT EXISTS Slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, datetime TEXT, status TEXT DEFAULT 'free',
            reserved_until TIMESTAMP, user_id INTEGER, payment_id TEXT,
            FOREIGN KEY(user_id) REFERENCES Users(id))''')
        await db.execute('''CREATE TABLE IF NOT EXISTS Settings (key TEXT PRIMARY KEY, value TEXT)''')
        await db.commit()

        defaults = [("price", "4500"), ("payment_timeout_hours", "3")]
        for k, v in defaults:
            await db.execute("INSERT OR IGNORE INTO Settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


async def add_user(user_id: int, username: str, first_name: str, role: str = 'client'):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO Users (id, username, first_name, role) VALUES (?, ?, ?, ?)",
                         (user_id, username, first_name, role))
        await db.commit()


async def add_slot_to_db(dt_obj: datetime):
    """Принимает объект datetime и сохраняет его в ISO формате"""
    # Сохраняем как 'YYYY-MM-DD HH:MM:SS'
    iso_dt = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO Slots (datetime) VALUES (?)", (iso_dt,))
        await db.commit()


async def get_free_slots():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, datetime FROM Slots WHERE status = 'free' ORDER BY datetime") as cursor:
            return await cursor.fetchall()


async def get_schedule():
    """Возвращает данные в сыром виде из БД. Сортировка теперь работает идеально."""
    async with aiosqlite.connect(DB_NAME) as db:
        # SQLite сравнивает такие строки корректно: 2024-01-01 < 2024-02-01
        query = """SELECT s.id, s.datetime, s.status, u.username, u.first_name
                   FROM Slots s LEFT JOIN Users u ON s.user_id = u.id 
                   ORDER BY s.datetime ASC"""
        async with db.execute(query) as cursor:
            return await cursor.fetchall()


async def delete_slot(slot_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM Slots WHERE id = ?", (slot_id,))
        await db.commit()


# Изменения в database.py

async def book_slot_pending_admin(slot_id: int, user_id: int) -> bool:
    """
    Бронирует слот, только если его текущий статус 'free'.
    Возвращает True при успехе, False если слот уже занят.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        # Добавляем условие WHERE status = 'free'
        cursor = await db.execute(
            "UPDATE Slots SET status = 'pending_admin', user_id = ? WHERE id = ? AND status = 'free'",
            (user_id, slot_id)
        )
        await db.commit()
        return cursor.rowcount > 0 # Если обновлено 0 строк, значит слот уже не free


async def approve_slot_booking(slot_id: int, payment_id: str, timeout_hours: int = 3):
    # 🔹 ИСПОЛЬЗУЕМ ТАЙМЗОНУ
    reserved_until = datetime.datetime.now(TZ) + datetime.timedelta(hours=timeout_hours)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE Slots SET status = 'pending_payment', reserved_until = ?, payment_id = ? WHERE id = ?",
                         (reserved_until, payment_id, slot_id))
        await db.commit()


async def reject_slot_booking(slot_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE Slots SET status = 'free', user_id = NULL, reserved_until = NULL WHERE id = ?",
                         (slot_id,))
        await db.commit()


async def cancel_unpaid_slot(slot_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT status, user_id FROM Slots WHERE id = ?", (slot_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] == 'pending_payment':
                await db.execute("UPDATE Slots SET status = 'free', user_id = NULL, reserved_until = NULL WHERE id = ?",
                                 (slot_id,))
                await db.commit()
                return row[1]
    return None


async def confirm_payment(slot_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE Slots SET status = 'booked', reserved_until = NULL WHERE id = ?", (slot_id,))
        await db.commit()


async def get_slot_details(slot_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        query = """SELECT s.datetime, s.status, u.username, u.first_name, u.id
                   FROM Slots s LEFT JOIN Users u ON s.user_id = u.id WHERE s.id = ?"""
        async with db.execute(query, (slot_id,)) as cursor:
            return await cursor.fetchone()


async def get_date_statuses(year: int, month: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT datetime, status, id FROM Slots") as cursor:
            rows = await cursor.fetchall()

    date_groups = {}
    for dt_iso, status, slot_id in rows:
        try:
            dt = datetime.datetime.strptime(dt_iso.split()[0], "%Y-%m-%d")
            if dt.year == year and dt.month == month:
                date_key = dt.strftime("%Y-%m-%d")
                if date_key not in date_groups:
                    date_groups[date_key] = {"statuses": [], "slots": []}
                date_groups[date_key]["statuses"].append(status)
                date_groups[date_key]["slots"].append(slot_id)
        except Exception:
            continue

    result = {}
    priority = ["free", "pending_payment", "pending_admin", "booked"]
    for date_key, data in date_groups.items():
        final_status = "empty"
        for p_status in priority:
            if p_status in data["statuses"]:
                final_status = p_status
                break
        result[date_key] = {"status": final_status, "count": len(data["slots"])}
    return result


async def get_slots_by_date(date_iso: str):
    """date_iso ожидается в формате 'YYYY-MM-DD'"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, datetime, status FROM Slots WHERE datetime LIKE ? ORDER BY datetime",
            (f"{date_iso}%",)
        ) as cursor:
            return await cursor.fetchall()

async def get_price_int() -> int:
    price_str = await get_setting("price") or "4500"
    return int(''.join(filter(str.isdigit, price_str))) or 4500

async def get_pending_payment_slots():
    """Возвращает все слоты, ожидающие оплаты, вместе с дедлайном (reserved_until)."""
    async with aiosqlite.connect(DB_NAME) as db:
        # Извлекаем id слота, user_id и время, до которого забронирован слот
        async with db.execute("SELECT id, user_id, reserved_until FROM Slots WHERE status = 'pending_payment'") as cur:
            return await cur.fetchall()

# 🔹 НОВЫЕ ФУНКЦИИ ДЛЯ АДМИНА
async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM Settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO Settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


async def get_booked_details():
    """Возвращает все не-свободные слоты с данными пользователей"""
    # COALESCE берет u.id, а если его нет, берет s.user_id напрямую из слота
    query = """SELECT s.id, s.datetime, s.status, u.username, u.first_name, COALESCE(u.id, s.user_id) as user_id
               FROM Slots s LEFT JOIN Users u ON s.user_id = u.id
               WHERE s.status != 'free' ORDER BY s.datetime"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(query) as cur:
            return await cur.fetchall()


async def get_statistics():
    """Собирает сводную статистику"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM Users") as c: users_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM Slots WHERE status='free'") as c: free = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM Slots WHERE status='pending_admin'") as c: p_admin = \
        (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM Slots WHERE status='pending_payment'") as c: p_pay = \
        (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM Slots WHERE status='booked'") as c: booked = (await c.fetchone())[0]

    price_str = await get_setting("price") or "4500₽"
    try:
        price_num = int("".join(filter(str.isdigit, price_str)))
    except ValueError:
        price_num = 0

    return {
        "users": users_count, "free": free, "pending_admin": p_admin,
        "pending_pay": p_pay, "booked": booked, "price": price_str, "revenue": booked * price_num
    }


async def get_user_bookings(user_id: int):
    """Возвращает активные записи конкретного пользователя"""
    query = """SELECT id, datetime, status, reserved_until 
               FROM Slots 
               WHERE user_id = ? AND status != 'free' 
               ORDER BY datetime ASC"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(query, (user_id,)) as cur:
            return await cur.fetchall()


async def cancel_user_booking(slot_id: int, user_id: int) -> bool:
    """Отменяет бронь (только pending_admin или pending_payment)"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "UPDATE Slots SET status = 'free', user_id = NULL, reserved_until = NULL "
            "WHERE id = ? AND user_id = ? AND status IN ('pending_admin', 'pending_payment')",
            (slot_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def admin_cancel_booking(slot_id: int) -> tuple[bool, str | None, int | None]:
    """Отменяет бронь админом. Возвращает (успех, старый_статус, user_id)"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT status, user_id FROM Slots WHERE id = ?", (slot_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False, None, None
        status, user_id = row
        await db.execute(
            "UPDATE Slots SET status = 'free', user_id = NULL, reserved_until = NULL WHERE id = ?",
            (slot_id,)
        )
        await db.commit()
        return True, status, user_id


async def get_free_times_for_date(date_iso: str) -> list[str]:
    """Принимает дату в формате YYYY-MM-DD"""
    async with aiosqlite.connect(DB_NAME) as db:
        # Теперь LIKE корректно найдет записи '2024-04-14 %'
        async with db.execute("SELECT datetime FROM Slots WHERE datetime LIKE ? AND status = 'free'",
                              (f"{date_iso} %",)) as cur:
            rows = await cur.fetchall()
    # Возвращаем только HH:MM (первые 5 символов после пробела)
    return [row[0].split(" ", 1)[1][:5] for row in rows if len(row[0].split()) > 1]


async def copy_slots_to_date(source_date_iso: str, target_date_iso: str) -> int:
    """Копирует свободные слоты между датами в ISO формате"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT datetime FROM Slots WHERE datetime LIKE ? AND status = 'free'",
                              (f"{source_date_iso} %",)) as cur:
            rows = await cur.fetchall()

        count = 0
        for row in rows:
            # row[0] это 'YYYY-MM-DD HH:MM:SS'
            time_part = row[0].split(" ", 1)[1]
            # Формируем новую строку в правильном ISO формате для вставки
            new_datetime = f"{target_date_iso} {time_part}"
            await db.execute("INSERT INTO Slots (datetime, status) VALUES (?, 'free')", (new_datetime,))
            count += 1
        await db.commit()
        return count

async def get_payment_id(slot_id: int) -> str | None:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT payment_id FROM Slots WHERE id = ?", (slot_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def get_slot_by_payment_id(payment_id: str) -> int | None:
    """Ищет ID слота по идентификатору платежа ЮKassa"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id FROM Slots WHERE payment_id = ?", (payment_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


# Добавьте в database.py

async def delete_old_free_slots():
    """Удаляет из базы все свободные слоты, время которых уже наступило или прошло."""
    # Получаем текущее время в МСК в формате ISO
    now_iso = datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        # Удаляем только те, что 'free'. Занятые или оплаченные лучше оставлять для истории/статистики
        await db.execute(
            "DELETE FROM Slots WHERE status = 'free' AND datetime < ?",
            (now_iso,)
        )
        await db.commit()
    print(f"[DB] Очистка старых свободных слотов завершена ({now_iso})")
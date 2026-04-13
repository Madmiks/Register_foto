from aiogram import Bot
from aiogram.exceptions import TelegramAPIError # 🔹 Импорт
import database

async def check_payment_timeout(bot: Bot, slot_id: int):
    """Проверяет, оплачен ли слот, и отменяет бронь при таймауте"""
    details = await database.get_slot_details(slot_id)
    if not details or details[1] != 'pending_payment':
        return

    user_id = await database.cancel_unpaid_slot(slot_id)

    if user_id:
        # 🔹 БЕЗОПАСНАЯ ОТПРАВКА СООБЩЕНИЯ
        try:
            await bot.send_message(
                user_id,
                "⏳ Время ожидания оплаты истекло. Ваша бронь аннулирована. Слот снова свободен."
            )
        except TelegramAPIError as e:
            print(f"Ошибка уведомления {user_id} о таймауте оплаты (возможно бот заблокирован): {e}")
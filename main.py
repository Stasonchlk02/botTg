import os
import time
import asyncio
import logging
import threading
from queue import Queue
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc  # вместо стандартного selenium webdriver

# --- Настройки ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SESSION_DIR = "/app/chrome_session"  # папка для сохранения сессии WhatsApp
WA_READY = False
driver = None
bot_app = None
main_loop = None
update_queue = Queue()  # очередь для обработки сообщений между потоками

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Функции WhatsApp ---
def start_whatsapp():
    """Запускает undetected Chrome и открывает WhatsApp Web"""
    global driver, WA_READY
    try:
        logger.info("Запуск undetected Chrome...")
        options = uc.ChromeOptions()
        options.add_argument(f"--user-data-dir={SESSION_DIR}")
        options.add_argument("--headless=new")       # headless режим
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-debugging-port=9222")

        driver = uc.Chrome(options=options)
        driver.get("https://web.whatsapp.com")
        logger.info("WhatsApp Web загружается, ожидание авторизации...")

        # Ждём появления панели чатов (признак успешного входа)
        WebDriverWait(driver, 120).until(
            EC.presence_of_element_located((By.XPATH, "//div[@data-testid='chat-list']"))
        )
        WA_READY = True
        logger.info("WhatsApp авторизован и готов к работе")
    except Exception as e:
        logger.error(f"Ошибка запуска WhatsApp: {e}")
        WA_READY = False

def send_whatsapp(phone: str, text: str):
    """Отправляет сообщение через WhatsApp Web"""
    global driver, WA_READY
    if not WA_READY or driver is None:
        logger.error("WhatsApp не готов")
        return False

    try:
        # Формируем ссылку для отправки сообщения конкретному номеру
        url = f"https://web.whatsapp.com/send?phone={phone}&text={text}"
        driver.get(url)
        logger.info(f"Открыт чат с {phone}")

        # Ожидаем поле ввода сообщения (более точный XPath)
        input_box = WebDriverWait(driver, 60).until(
            EC.element_to_be_clickable((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']"))
        )
        # Альтернативный XPath, если первый не работает:
        # input_box = WebDriverWait(driver, 60).until(
        #     EC.element_to_be_clickable((By.XPATH, "//div[@contenteditable='true']"))
        # )

        # Вводим текст и отправляем
        input_box.clear()
        input_box.send_keys(text)
        time.sleep(0.5)

        # Нажимаем Enter
        send_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//button[@data-testid='compose-btn-send']"))
        )
        send_button.click()

        logger.info(f"Сообщение отправлено {phone}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        return False

# --- Telegram обработчики ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Бот готов!\n"
        "Используй команду /send <номер> <текст>\n"
        "Пример: /send 79991234567 Привет!"
    )

async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /send <номер> <текст>")
        return

    phone = context.args[0]
    # Убираем все нецифровые символы из номера
    phone = ''.join(filter(str.isdigit, phone))
    if len(phone) < 10:
        await update.message.reply_text("❌ Некорректный номер телефона")
        return

    text = ' '.join(context.args[1:])
    await update.message.reply_text(f"📤 Отправляю {phone}...")

    # Запускаем отправку в отдельном потоке, чтобы не блокировать Telegram
    def do_send():
        result = send_whatsapp(phone, text)
        asyncio.run_coroutine_threadsafe(
            update.message.reply_text("✅ Отправлено!" if result else "❌ Ошибка отправки"),
            main_loop
        )

    threading.Thread(target=do_send, daemon=True).start()

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка обычных текстовых сообщений (например, можно парсить номер и текст)"""
    text = update.message.text
    # Можно реализовать свой формат, например "номер текст"
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[0].isdigit():
        phone = parts[0]
        msg = parts[1]
        await update.message.reply_text(f"📤 Отправляю {phone}...")
        threading.Thread(target=lambda: send_whatsapp(phone, msg), daemon=True).start()
    else:
        await update.message.reply_text("Отправь номер и сообщение через пробел, например:\n79991234567 Привет!")

# --- Запуск бота ---
def run_telegram():
    """Запускает Telegram бота (в основном потоке asyncio)"""
    global bot_app
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("send", send_command))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    bot_app.run_polling()

def main():
    global main_loop
    # Запускаем WhatsApp в отдельном потоке
    wa_thread = threading.Thread(target=start_whatsapp, daemon=True)
    wa_thread.start()

    # Ждём инициализации WhatsApp
    time.sleep(10)

    # Запускаем Telegram бота
    main_loop = asyncio.get_event_loop()
    run_telegram()

if __name__ == "__main__":
    main()

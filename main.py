import asyncio
import os
import re
import time
from urllib.parse import quote

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = os.path.join(os.getcwd(), "chrome_session")

driver = None
wa_ready = False
bot_app = None

async def init_whatsapp():
    global driver, wa_ready, bot_app

    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.binary_location = "/usr/bin/google-chrome"   # или /usr/bin/chromium, если Chrome не встанет

    # Если у тебя chromedriver не в PATH, укажи путь:
    service = Service("/usr/bin/chromedriver")  # или оставь пустым, если chromedriver в PATH

    driver = webdriver.Chrome(service=service, options=options)
    driver.get("https://web.whatsapp.com")
    print("WhatsApp Web загружен")

    # Ждём 10 секунд, чтобы страница отрисовалась
    await asyncio.sleep(10)

    # Проверяем, возможно уже авторизованы (есть чат-лист)
    if driver.find_elements(By.CSS_SELECTOR, "div[data-testid='chat-list']"):
        wa_ready = True
        print("✅ WhatsApp уже авторизован!")
        await bot_app.bot.send_message(OWNER_ID, "✅ WhatsApp Web уже авторизован! Бот готов.")
        return

    # Если нет, ищем QR
    qr_selector = "canvas[aria-label='QR code']"
    for attempt in range(10):  # пробуем 30 секунд
        qr = driver.find_elements(By.CSS_SELECTOR, qr_selector)
        if qr:
            print("📱 QR найден, отправляю в Telegram...")
            # Скриншот всей страницы
            png = driver.get_screenshot_as_png()
            await bot_app.bot.send_photo(OWNER_ID, photo=png, caption="🔐 Отсканируйте QR-код в WhatsApp Web")
            print("✅ QR отправлен в Telegram")
            # Ждём авторизации до 2 минут
            for _ in range(40):
                await asyncio.sleep(3)
                if driver.find_elements(By.CSS_SELECTOR, "div[data-testid='chat-list']"):
                    wa_ready = True
                    await bot_app.bot.send_message(OWNER_ID, "✅ WhatsApp Web авторизован! Бот готов.")
                    print("✅ Авторизация успешна!")
                    return
            # Если не дождались
            await bot_app.bot.send_message(OWNER_ID, "⚠️ Время ожидания сканирования истекло. Перезапустите бота.")
            return
        await asyncio.sleep(3)

    # Если QR не найден
    wa_ready = False
    print("❌ QR не найден. Возможно, страница не загрузилась.")
    png = driver.get_screenshot_as_png()
    await bot_app.bot.send_photo(OWNER_ID, photo=png, caption="❌ Не удалось найти QR-код. Проверьте логи.")

def send_whatsapp(phone: str, text: str):
    global driver
    phone = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}"
    driver.get(url)
    try:
        input_box = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[contenteditable='true']"))
        )
        time.sleep(2)
        input_box.send_keys("\n")
        time.sleep(2)
        if input_box.text.strip() == "":
            return
        send_selectors = [
            "button[aria-label='Отправить']", "button[aria-label='Send']",
            "span[data-testid='send']", "span[data-icon='send']",
            "div[data-testid='send']", "button[data-testid='compose-btn-send']"
        ]
        for selector in send_selectors:
            try:
                btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                return
            except:
                continue
        input_box.click()
        input_box.send_keys("\n")
        time.sleep(2)
        if input_box.text.strip() == "":
            return
        raise Exception("Не удалось отправить")
    except Exception as e:
        raise Exception(f"Ошибка: {e}")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Нет доступа")
        return
    await update.message.reply_text(
        f"Статус WhatsApp: {'✅ готов' if wa_ready else '⏳ ожидает авторизации (QR будет отправлен в Telegram)'}\n"
        "Формат сообщения: +79151234567 Текст"
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not wa_ready:
        await update.message.reply_text("⏳ WhatsApp не готов. Дождитесь авторизации (QR в Telegram).")
        return
    text = update.message.text.strip()
    match = re.match(r"^(\+\d{10,15})\s+(.+)$", text, re.S)
    if not match:
        await update.message.reply_text("Формат: +79151234567 Текст")
        return
    phone, message = match.groups()
    await update.message.reply_text(f"📤 Отправляю на {phone}...")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_whatsapp, phone, message)
        await update.message.reply_text("✅ Отправлено")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def post_init(app: Application):
    global bot_app
    bot_app = app
    asyncio.create_task(init_whatsapp())

def main():
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN не задан")
        return

    import requests
    requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
    print("Вебхук сброшен")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.post_init = post_init

    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

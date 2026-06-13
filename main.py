import asyncio
import os
import re
import time
import threading
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
main_loop = None

def start_whatsapp():
    global driver, wa_ready, bot_app, main_loop

    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.binary_location = "/usr/bin/google-chrome"
    service = Service("/usr/bin/chromedriver")

    driver = webdriver.Chrome(service=service, options=options)
    driver.get("https://web.whatsapp.com")
    print("WhatsApp загружен")

    def send_to_telegram(text, photo=None):
        if not bot_app or not main_loop:
            return
        if photo:
            coro = bot_app.bot.send_photo(OWNER_ID, photo, caption=text)
        else:
            coro = bot_app.bot.send_message(OWNER_ID, text)
        asyncio.run_coroutine_threadsafe(coro, main_loop)

    # Проверяем, возможно уже авторизованы (есть чат-лист)
    for _ in range(10):
        time.sleep(3)
        if driver.find_elements(By.CSS_SELECTOR, "div[data-testid='chat-list']"):
            wa_ready = True
            print("✅ WhatsApp уже авторизован")
            send_to_telegram("✅ WhatsApp готов!")
            return

    # Если нет — ищем QR
    print("Поиск QR-кода...")
    for _ in range(10):
        qr = driver.find_elements(By.CSS_SELECTOR, "canvas[aria-label='QR code']")
        if qr:
            png = driver.get_screenshot_as_png()
            send_to_telegram("🔐 Отсканируйте QR-код в WhatsApp Web", png)
            print("QR отправлен в Telegram")
            # Бесконечно ждём авторизации
            while True:
                time.sleep(5)
                if driver.find_elements(By.CSS_SELECTOR, "div[data-testid='chat-list']"):
                    wa_ready = True
                    print("✅ WhatsApp авторизован после QR")
                    send_to_telegram("✅ WhatsApp готов!")
                    return
        time.sleep(3)

    # Если за 30 секунд QR не появился
    png = driver.get_screenshot_as_png()
    send_to_telegram("❌ Не удалось найти QR-код. Проверьте логи.", png)
    wa_ready = False
    print("❌ WhatsApp не авторизован")

def send_whatsapp(phone: str, text: str):
    phone = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}"
    driver.get(url)
    input_box = WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "div[contenteditable='true']"))
    )
    time.sleep(2)
    input_box.send_keys("\n")
    time.sleep(2)
    if input_box.text.strip() == "":
        return
    for sel in ["button[aria-label='Отправить']", "button[aria-label='Send']", "span[data-testid='send']"]:
        try:
            btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
            driver.execute_script("arguments[0].click();", btn)
            return
        except:
            pass
    raise Exception("Не удалось отправить")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        f"Статус WhatsApp: {'✅ готов' if wa_ready else '⏳ ожидает авторизации'}\n"
        "Формат сообщения: +79151234567 Текст"
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not wa_ready:
        await update.message.reply_text("WhatsApp не готов. Дождитесь авторизации (QR придёт в Telegram).")
        return
    m = re.match(r"^(\+\d{10,15})\s+(.+)$", update.message.text.strip(), re.S)
    if not m:
        await update.message.reply_text("Формат: +79151234567 Текст")
        return
    phone, msg = m.groups()
    await update.message.reply_text(f"📤 Отправляю на {phone}...")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_whatsapp, phone, msg)
    await update.message.reply_text("✅ Отправлено")

def main():
    global bot_app, main_loop
    if not TOKEN:
        print("❌ Нет TELEGRAM_TOKEN")
        return
    import requests
    try:
        requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
        print("✅ Вебхук сброшен")
    except Exception as e:
        print(f"⚠️ Ошибка сброса вебхука: {e}")
    time.sleep(2)

    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    threading.Thread(target=start_whatsapp, daemon=True).start()

    print("🚀 Бот запущен")
    bot_app.run_polling(drop_pending_updates=True, allowed_updates=["message"])

if __name__ == "__main__":
    main()

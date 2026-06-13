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
    options.binary_location = "/usr/bin/google-chrome"

    service = Service("/usr/bin/chromedriver")

    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"Ошибка Chrome: {e}")
        await bot_app.bot.send_message(OWNER_ID, f"❌ Ошибка браузера: {e}")
        return

    driver.get("https://web.whatsapp.com")
    print("WhatsApp Web загружен")
    await asyncio.sleep(5)

    if driver.find_elements(By.CSS_SELECTOR, "div[data-testid='chat-list']"):
        wa_ready = True
        print("✅ Уже авторизован")
        await bot_app.bot.send_message(OWNER_ID, "✅ WhatsApp готов!")
        return

    for attempt in range(15):
        qr = driver.find_elements(By.CSS_SELECTOR, "canvas[aria-label='QR code']")
        if qr:
            png = driver.get_screenshot_as_png()
            await bot_app.bot.send_photo(OWNER_ID, photo=png, caption="🔐 Отсканируйте QR")
            print("QR отправлен")
            for _ in range(40):
                await asyncio.sleep(3)
                if driver.find_elements(By.CSS_SELECTOR, "div[data-testid='chat-list']"):
                    wa_ready = True
                    await bot_app.bot.send_message(OWNER_ID, "✅ WhatsApp готов!")
                    return
            await bot_app.bot.send_message(OWNER_ID, "⚠️ Время истекло")
            return
        await asyncio.sleep(3)

    png = driver.get_screenshot_as_png()
    await bot_app.bot.send_photo(OWNER_ID, photo=png, caption="❌ QR не найден")

def send_whatsapp(phone: str, text: str):
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
        for selector in ["button[aria-label='Отправить']", "button[aria-label='Send']", "span[data-testid='send']"]:
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
        f"Статус WhatsApp: {'✅ готов' if wa_ready else '⏳ ожидает QR'}\n"
        "Формат: +79151234567 Текст"
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not wa_ready:
        await update.message.reply_text("WhatsApp не готов, ждите QR")
        return
    text = update.message.text.strip()
    match = re.match(r"^(\+\d{10,15})\s+(.+)$", text, re.S)
    if not match:
        await update.message.reply_text("Формат: +79151234567 Текст")
        return
    phone, message = match.groups()
    await update.message.reply_text(f"📤 Отправляю {phone}...")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_whatsapp, phone, message)
        await update.message.reply_text("✅ Отправлено")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def post_init(app: Application):
    global bot_app
    bot_app = app
    await asyncio.sleep(2)
    asyncio.create_task(init_whatsapp())

def main():
    if not TOKEN:
        print("Нет токена")
        return
    import requests
    requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.post_init = post_init
    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

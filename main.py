import asyncio
import os
import re
import threading
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
from webdriver_manager.chrome import ChromeDriverManager


TOKEN = "8827507215:AAGCzPvre3sPFu4wcfR80EMANm-gAgRONlQ"
OWNER_ID = 1636373767
SESSION_DIR = os.path.join(os.getcwd(), "chrome_session")

driver = None
wa_ready = False


def start_whatsapp():
    global driver, wa_ready

    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--profile-directory=Default")

    # Эти строки нужны для Railway (сервер без экрана)
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.binary_location = "/usr/bin/chromium"   # путь к браузеру

    service = Service("/usr/bin/chromedriver")      # путь к драйверу
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    driver.get("https://web.whatsapp.com")

    while True:
        try:
            WebDriverWait(driver, 30).until(
    EC.presence_of_element_located((By.CSS_SELECTOR, "canvas[aria-label='QR code']"))
)
# Скриншот
driver.save_screenshot("qr.png")
# Отправить через бота (нужен доступ к application.bot)
await application.bot.send_photo(OWNER_ID, photo=open("qr.png", "rb"))
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='chat-list']"))
            )
            wa_ready = True
            print("✅ WhatsApp готов")
            return
        except Exception:
            wa_ready = False
            time.sleep(3)

def send_whatsapp(phone: str, text: str):
    phone = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}"
    driver.get(url)

    selectors = [
        "button[aria-label='Отправить']",
        "button[aria-label='Send']",
        "span[data-testid='send']",
        "span[data-icon='send']",
    ]

    for selector in selectors:
        try:
            btn = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(2)
            return
        except Exception:
            pass

    raise Exception("Не найдена кнопка отправки")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    await update.message.reply_text(
        "Отправь сообщение так:\n"
        "+79151234567 Привет"
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if not wa_ready:
        await update.message.reply_text(
            "WhatsApp ещё не готов.\n"
            "Если это первый запуск — открой Chrome и отсканируй QR."
        )
        return

    text = update.message.text.strip()
    match = re.match(r"^(\+\d{10,15})\s+(.+)$", text, re.S)

    if not match:
        await update.message.reply_text("Формат:\n+79151234567 Привет")
        return

    phone, message = match.groups()
    await update.message.reply_text(f"📤 Отправляю на {phone}...")

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_whatsapp, phone, message)
        await update.message.reply_text("✅ Отправлено")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def on_startup(app: Application):
    threading.Thread(target=start_whatsapp, daemon=True).start()


def main():
    app = Application.builder().token(TOKEN).build()
    app.post_init = on_startup

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("🚀 Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()

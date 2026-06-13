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


TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = "/app/chrome_session"

driver = None
wa_ready = False
driver_lock = threading.Lock()


def close_startup_dialog():
    """Закрывает типовой диалог WhatsApp, если он появился."""
    global driver
    selectors = [
        (By.CSS_SELECTOR, "button[aria-label='Закрыть']"),
        (By.CSS_SELECTOR, "button[aria-label='Close']"),
        (By.CSS_SELECTOR, "div[aria-label='Закрыть']"),
        (By.CSS_SELECTOR, "div[aria-label='Close']"),
        (By.XPATH, "//button[contains(., 'Понятно')]"),
        (By.XPATH, "//button[contains(., 'Got it')]"),
        (By.XPATH, "//button[contains(., 'Continue')]"),
        (By.XPATH, "//button[contains(., 'Продолжить')]"),
    ]

    for by, selector in selectors:
        try:
            elements = driver.find_elements(by, selector)
            for el in elements:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(1)
                    return
        except Exception:
            pass


def start_whatsapp():
    global driver, wa_ready

    os.makedirs(SESSION_DIR, exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--profile-directory=Default")

    # Railway / Docker
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    options.binary_location = "/usr/bin/google-chrome"

    driver = webdriver.Chrome(
        service=Service("/usr/bin/chromedriver"),
        options=options
    )

    with driver_lock:
        driver.get("https://web.whatsapp.com")

    print("🌐 WhatsApp Web загружен")

    while True:
        try:
            with driver_lock:
                close_startup_dialog()
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
    global driver

    phone = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}"

    with driver_lock:
        driver.get(url)

        # Даём странице догрузиться
        time.sleep(5)
        close_startup_dialog()

        selectors = [
            "button[aria-label='Отправить']",
            "button[aria-label='Send']",
            "span[data-testid='send']",
            "span[data-icon='send']",
            "button[data-testid='compose-btn-send']",
        ]

        last_error = None

        for selector in selectors:
            try:
                btn = WebDriverWait(driver, 25).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(2)
                return
            except Exception as e:
                last_error = e

        # Для отладки сохраним скриншот
        try:
            driver.save_screenshot("/app/send_error.png")
            print("📸 Скриншот сохранён: /app/send_error.png")
        except Exception:
            pass

        raise Exception(f"Не найдена кнопка отправки. Последняя ошибка: {last_error}")


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
            "Если это первый запуск — дождись загрузки и отсканируй QR."
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
    if not TOKEN:
        print("❌ Нет TELEGRAM_TOKEN")
        return

    app = Application.builder().token(TOKEN).build()
    app.post_init = on_startup

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("🚀 Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()

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

# ---------- КОНФИГУРАЦИЯ ----------
TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = "/app/chrome_session"

driver = None
wa_ready = False
bot_app = None
main_loop = None

# ---------- TELEGRAM HELPER ----------
def send_to_telegram(text, photo=None):
    if not bot_app or not main_loop:
        print(f"[TG] bot_app/main_loop не готовы: {text}")
        return
    try:
        if photo:
            coro = bot_app.bot.send_photo(OWNER_ID, photo, caption=text)
        else:
            coro = bot_app.bot.send_message(OWNER_ID, text)
        future = asyncio.run_coroutine_threadsafe(coro, main_loop)
        future.result(timeout=10)
    except Exception as e:
        print(f"[TG] Ошибка отправки: {e}")

# ---------- BROWSER ----------
def get_driver():
    """Создаёт headless Chrome, используя заранее установленные бинарники из Dockerfile."""
    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    # Пути к Chrome и chromedriver из Dockerfile
    options.binary_location = "/usr/bin/google-chrome"
    service = Service("/usr/bin/chromedriver")

    return webdriver.Chrome(service=service, options=options)

# ---------- WHATSAPP AUTH ----------
def is_authorized():
    try:
        driver.find_element(By.CSS_SELECTOR, "div[data-testid='chat-list']")
        return True
    except Exception:
        return False

def start_whatsapp():
    global driver, wa_ready

    # Ждём запуск event loop Telegram
    for _ in range(30):
        if main_loop and main_loop.is_running():
            break
        time.sleep(1)

    os.makedirs(SESSION_DIR, exist_ok=True)

    try:
        driver = get_driver()
        driver.get("https://web.whatsapp.com")
        print("WhatsApp Web открыт")
        time.sleep(5)

        if is_authorized():
            wa_ready = True
            print("✅ WhatsApp авторизован (сессия восстановлена)")
            send_to_telegram("✅ WhatsApp готов! Сессия восстановлена.")
            return

        # Ждём QR-код
        print("Ожидание QR-кода...")
        qr_found = False
        for _ in range(20):
            time.sleep(3)
            if is_authorized():
                wa_ready = True
                send_to_telegram("✅ WhatsApp авторизован!")
                return
            try:
                driver.find_element(By.CSS_SELECTOR, "canvas[aria-label='QR code']")
                qr_found = True
                break
            except Exception:
                pass

        if qr_found:
            png = driver.get_screenshot_as_png()
            send_to_telegram("Отсканируйте QR-код для входа в WhatsApp Web:", png)
            print("QR отправлен в Telegram, жду сканирования...")
            for i in range(60):  # до 5 минут
                time.sleep(5)
                if is_authorized():
                    wa_ready = True
                    send_to_telegram("✅ WhatsApp авторизован!")
                    return
            send_to_telegram("❌ QR не отсканирован за 5 минут.")
        else:
            send_to_telegram("❌ QR-код не появился на странице WhatsApp Web.")

    except Exception as e:
        print(f"❌ Ошибка WhatsApp: {e}")
        send_to_telegram(f"❌ Ошибка запуска WhatsApp: {e}")

# ---------- ОТПРАВКА СООБЩЕНИЯ ----------
def send_whatsapp(phone: str, text: str):
    phone_clean = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone_clean}&text={quote(text)}"
    driver.get(url)

    selectors = [
        "button[aria-label='Отправить']",
        "button[aria-label='Send']",
        "span[data-testid='send']",
        "span[data-icon='send']",
    ]

    for sel in selectors:
        try:
            btn = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            driver.execute_script("arguments[0].click();", btn)
            print("[WA] Сообщение отправлено")
            return
        except Exception:
            continue

    # Fallback: Enter в поле ввода
    try:
        composer = driver.find_element(By.CSS_SELECTOR, "div[contenteditable='true'][role='textbox']")
        composer.send_keys("\n")
        print("[WA] Отправлено через Enter")
    except Exception:
        raise Exception("Не удалось отправить сообщение")

# ---------- TELEGRAM HANDLERS ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    status = "✅ готов" if wa_ready else "⏳ ожидает авторизации"
    await update.message.reply_text(
        f"Статус WhatsApp: {status}\n\n"
        "Формат: +79151234567 Привет\n"
        "/restart — перезапустить WhatsApp"
    )

async def restart_wa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    global driver, wa_ready
    wa_ready = False
    if driver:
        try:
            driver.quit()
        except Exception:
            pass
    threading.Thread(target=start_whatsapp, daemon=True).start()
    await update.message.reply_text("🔄 Перезапуск WhatsApp...")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not wa_ready:
        await update.message.reply_text("⏳ WhatsApp не готов. /restart")
        return

    text = update.message.text.strip()
    match = re.match(r"^(\+\d{10,15})\s+(.+)$", text, re.S)
    if not match:
        await update.message.reply_text("Формат: +79151234567 Привет")
        return

    phone, message = match.groups()
    await update.message.reply_text(f"📤 Отправляю на {phone}...")

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_whatsapp, phone, message)
        await update.message.reply_text("✅ Отправлено")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ---------- MAIN ----------
def main():
    global bot_app, main_loop

    if not TOKEN:
        print("❌ Не задан TELEGRAM_TOKEN")
        return

    import requests as req
    for attempt in range(3):
        try:
            r = req.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=10)
            if r.status_code == 200:
                print("✅ Вебхук сброшен")
                break
        except Exception as e:
            print(f"⚠️ Попытка {attempt+1}: {e}")
            time.sleep(3)

    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_cmd))
    bot_app.add_handler(CommandHandler("restart", restart_wa_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)
    threading.Thread(target=start_whatsapp, daemon=True).start()

    print("🚀 Бот запущен")
    bot_app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

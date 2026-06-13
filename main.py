import asyncio
import os
import re
import time
import base64
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

def start_whatsapp():
    global driver, wa_ready

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
    driver = webdriver.Chrome(options=options)
    driver.get("https://web.whatsapp.com")
    print("WhatsApp Web загружен, делаем скриншоты каждые 10 секунд...")

    for attempt in range(30):  # 5 минут
        time.sleep(10)
        # Делаем скриншот всей страницы
        png = driver.get_screenshot_as_png()
        b64 = base64.b64encode(png).decode('utf-8')
        print(f"\n===== Скриншот {attempt+1} (base64) =====")
        # Выводим первые 100 символов для информации, а полный вывод - по желанию
        # Чтобы не засорять логи, выводим сокращённо, но можно вывести полностью
        # Для диагностики нужно полное изображение, поэтому выводим весь base64
        print(b64)
        print("===== Конец скриншота =====\n")

        try:
            # Проверяем наличие чатов
            WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='chat-list']"))
            )
            wa_ready = True
            print("✅ WhatsApp готов!")
            return
        except:
            pass

        try:
            # Проверяем наличие QR
            driver.find_element(By.CSS_SELECTOR, "canvas[aria-label='QR code']")
            print("QR код обнаружен на странице")
        except:
            pass

    print("❌ WhatsApp не авторизован")

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
        f"Статус WhatsApp: {'✅ готов' if wa_ready else '⏳ ожидает QR (смотри логи)'}\n"
        "Формат: +79151234567 Текст"
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not wa_ready:
        await update.message.reply_text("⏳ WhatsApp не готов, скриншоты в логах Railway")
        return
    text = update.message.text.strip()
    match = re.match(r"^(\+\d{10,15})\s+(.+)$", text, re.S)
    if not match:
        await update.message.reply_text("Формат: +79151234567 Текст")
        return
    phone, message = match.groups()
    await update.message.reply_text(f"Отправляю на {phone}...")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_whatsapp, phone, message)
        await update.message.reply_text("✅ Отправлено")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def on_startup(app):
    import threading
    threading.Thread(target=start_whatsapp, daemon=True).start()

def main():
    if not TOKEN:
        print("❌ Нет TELEGRAM_TOKEN")
        return

    import requests
    try:
        requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
        print("Вебхук сброшен")
    except Exception as e:
        print(f"Ошибка сброса вебхука: {e}")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(on_startup(app))

    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])

if __name__ == "__main__":
    main()

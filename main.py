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
SESSION_DIR = os.path.join(os.getcwd(), "chrome_session")

driver = None
wa_ready = False
application = None  # для доступа к боту из потоков


def start_whatsapp():
    global driver, wa_ready, application

    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--profile-directory=Default")

    # Для Railway (без экрана)
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    
    # Пути для Railway
    options.binary_location = "/usr/bin/chromium"
    service = Service("/usr/bin/chromedriver")

    driver = webdriver.Chrome(service=service, options=options)
    driver.get("https://web.whatsapp.com")

    # Ждём загрузки и отправляем QR если нужно
    for _ in range(20):  # ждём до 60 секунд
        time.sleep(3)
        try:
            # Проверяем готов ли WhatsApp
            WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='chat-list']"))
            )
            wa_ready = True
            print("✅ WhatsApp готов")
            
            # Отправляем уведомление владельцу
            if application:
                asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(OWNER_ID, "✅ WhatsApp Web готов к работе!"),
                    asyncio.get_event_loop()
                )
            return
        except:
            pass
        
        # Если не готов - возможно нужен QR
        try:
            qr_element = driver.find_element(By.CSS_SELECTOR, "canvas[aria-label='QR code']")
            if qr_element:
                print("📱 QR код обнаружен, сохраняем...")
                driver.save_screenshot("qr.png")
                print("📱 QR сохранён как qr.png")
                
                # Отправляем QR в Telegram
                if application:
                    with open("qr.png", "rb") as f:
                        asyncio.run_coroutine_threadsafe(
                            application.bot.send_photo(OWNER_ID, photo=f, caption="Отсканируй QR код в WhatsApp"),
                            asyncio.get_event_loop()
                        )
                # Ждём сканирования
                time.sleep(20)
        except:
            pass
    
    wa_ready = True  # если ничего не получилось - всё равно работаем
    print("⚠️ WhatsApp статус не определён")

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
            return True
        except Exception:
            pass

    raise Exception("Не найдена кнопка отправки")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Нет доступа")
        return

    await update.message.reply_text(
        "🤖 Бот готов!\n\n"
        "Отправь сообщение в формате:\n"
        "+79151234567 Текст сообщения\n\n"
        f"WhatsApp статус: {'✅ Готов' if wa_ready else '⏳ Загружается...'}"
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if not wa_ready:
        await update.message.reply_text(
            "⏳ WhatsApp ещё не готов. Подожди 20 секунд..."
        )
        return

    text = update.message.text.strip()
    match = re.match(r"^(\+\d{10,15})\s+(.+)$", text, re.S)

    if not match:
        await update.message.reply_text("❌ Формат: +79151234567 Текст сообщения")
        return

    phone, message = match.groups()
    await update.message.reply_text(f"📤 Отправляю на {phone}...")

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_whatsapp, phone, message)
        await update.message.reply_text("✅ Отправлено!")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def on_startup(app: Application):
    global application
    application = app
    print("🚀 Запускаю WhatsApp Web...")
    threading.Thread(target=start_whatsapp, daemon=True).start()


def main():
    global application
    
    if not TOKEN:
        print("❌ Ошибка: TELEGRAM_TOKEN не задан в переменных окружения")
        return
    
    app = Application.builder().token(TOKEN).build()
    application = app
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    # Запускаем инициализацию при старте
    asyncio.get_event_loop().run_until_complete(on_startup(app))
    
    print("🚀 Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()

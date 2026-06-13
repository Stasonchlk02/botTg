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

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = os.getenv("TELEGRAM_TOKEN")          # Обязательно задать в Railway Variables
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = os.path.join(os.getcwd(), "chrome_session")

driver = None
wa_ready = False
application = None   # для доступа к боту из потока

# ========== ИНИЦИАЛИЗАЦИЯ WHATSAPP ==========
def start_whatsapp():
    global driver, wa_ready, application

    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--headless=new")          # без GUI
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.binary_location = "/usr/bin/chromium"   # путь к браузеру в Railway

    # ChromeDriver тоже ставится через chromium-driver в Railway
    service = Service("/usr/bin/chromedriver")

    driver = webdriver.Chrome(service=service, options=options)
    driver.get("https://web.whatsapp.com")

    # Пытаемся дождаться входа и, если нужно, отправить QR в Telegram
    for _ in range(30):   # максимум 90 секунд ожидания
        time.sleep(3)
        try:
            # Если чат-лист загрузился – вход выполнен
            WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='chat-list']"))
            )
            wa_ready = True
            print("✅ WhatsApp готов")
            if application:
                asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(OWNER_ID, "✅ WhatsApp Web успешно авторизован!"),
                    asyncio.get_event_loop()
                )
            return
        except:
            pass

        # Если не готов – возможно, висит QR‑код
        try:
            qr_canvas = driver.find_element(By.CSS_SELECTOR, "canvas[aria-label='QR code']")
            if qr_canvas:
                print("📱 QR-код обнаружен, сохраняем скриншот...")
                driver.save_screenshot("/tmp/qr.png")
                if application:
                    with open("/tmp/qr.png", "rb") as f:
                        asyncio.run_coroutine_threadsafe(
                            application.bot.send_photo(OWNER_ID, f, caption="🔐 Отсканируйте QR-код в WhatsApp Web"),
                            asyncio.get_event_loop()
                        )
                # Ждём 20 секунд, чтобы пользователь успел отсканировать
                time.sleep(20)
        except:
            continue

    # Если за 90 секунд так и не вошли – продолжаем (но, возможно, проблемы)
    wa_ready = True
    print("⚠️ Статус WhatsApp не определён, но бот продолжает работу")

# ========== ОТПРАВКА СООБЩЕНИЯ (улучшенная) ==========
def send_whatsapp(phone: str, text: str):
    phone = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}"
    driver.get(url)

    try:
        # Ждём появления поля ввода
        input_box = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[contenteditable='true']"))
        )
        time.sleep(2)   # даём WhatsApp активировать кнопку

        # ----- Способ 1: Enter -----
        input_box.send_keys("\n")
        time.sleep(2)
        if input_box.text.strip() == "":
            return

        # ----- Способ 2: поиск кнопки отправки -----
        send_selectors = [
            "button[aria-label='Отправить']",
            "button[aria-label='Send']",
            "span[data-testid='send']",
            "span[data-icon='send']",
            "div[data-testid='send']",
            "button[data-testid='compose-btn-send']"
        ]
        for selector in send_selectors:
            try:
                send_btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                driver.execute_script("arguments[0].click();", send_btn)
                time.sleep(2)
                return
            except:
                continue

        # ----- Способ 3: клик по полю + Enter -----
        input_box.click()
        input_box.send_keys("\n")
        time.sleep(2)
        if input_box.text.strip() == "":
            return

        raise Exception("Не удалось отправить сообщение (кнопка не найдена, Enter не сработал)")

    except Exception as e:
        raise Exception(f"Ошибка при отправке: {e}")

# ========== КОМАНДЫ ТЕЛЕГРАМ ==========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Доступ запрещён")
        return
    await update.message.reply_text(
        "🤖 Бот готов!\n\n"
        "Отправь сообщение в формате:\n"
        "+79151234567 Текст сообщения\n\n"
        f"Статус WhatsApp: {'✅ Готов' if wa_ready else '⏳ Загружается...'}"
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    if not wa_ready:
        await update.message.reply_text("⏳ WhatsApp ещё не готов, подожди 10–20 секунд...")
        return

    text = update.message.text.strip()
    match = re.match(r"^(\+\d{10,15})\s+(.+)$", text, re.S)
    if not match:
        await update.message.reply_text("❌ Неверный формат. Используй: +79151234567 Текст")
        return

    phone, message = match.groups()
    await update.message.reply_text(f"📤 Отправляю на {phone}...")

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_whatsapp, phone, message)
        await update.message.reply_text("✅ Отправлено успешно!")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ========== ЗАПУСК ==========
async def on_startup(app: Application):
    global application
    application = app
    print("🚀 Запускаем WhatsApp Web...")
    threading.Thread(target=start_whatsapp, daemon=True).start()

def main():
    global application

    if not TOKEN:
        print("❌ Ошибка: переменная TELEGRAM_TOKEN не установлена!")
        return

    # Принудительно сбрасываем вебхук, чтобы избежать конфликта getUpdates
    import requests
    try:
        requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
        print("✅ Вебхук сброшен")
    except Exception as e:
        print(f"⚠️ Не удалось сбросить вебхук: {e}")

    app = Application.builder().token(TOKEN).build()
    application = app

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Инициализация WhatsApp (асинхронная)
    asyncio.get_event_loop().run_until_complete(on_startup(app))

    print("🚀 Бот запущен и слушает сообщения")
    time.sleep(5)   # небольшая пауза, чтобы старый процесс успел завершиться
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

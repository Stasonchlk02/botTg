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
TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = os.path.join(os.getcwd(), "chrome_session")

driver = None
wa_ready = False
application = None

# ========== ИНИЦИАЛИЗАЦИЯ WHATSAPP С ОТЛАДКОЙ ==========
def start_whatsapp():
    global driver, wa_ready, application

    print("🟡 Настройка Chrome options...")
    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.binary_location = "/usr/bin/chromium"

    service = Service("/usr/bin/chromedriver")

    print("🟡 Запуск Chrome...")
    driver = webdriver.Chrome(service=service, options=options)
    driver.get("https://web.whatsapp.com")
    print("🟡 Страница WhatsApp Web загружена")

    # Отправляем первый скриншот для диагностики
    driver.save_screenshot("/tmp/after_load.png")
    if application:
        with open("/tmp/after_load.png", "rb") as f:
            asyncio.run_coroutine_threadsafe(
                application.bot.send_photo(OWNER_ID, f, caption="📸 Скриншот после загрузки страницы"),
                asyncio.get_event_loop()
            )

    # Ждём до 60 секунд появления либо чатов, либо QR-кода
    for attempt in range(20):  # 20 * 3 = 60 секунд
        time.sleep(3)
        print(f"🟡 Попытка {attempt+1}/20...")
        # Проверяем наличие чат-листа (успешный вход)
        try:
            WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='chat-list']"))
            )
            wa_ready = True
            print("✅ WhatsApp готов!")
            if application:
                asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(OWNER_ID, "✅ WhatsApp Web авторизован!"),
                    asyncio.get_event_loop()
                )
            return
        except:
            pass

        # Проверяем наличие QR-кода
        try:
            qr = driver.find_element(By.CSS_SELECTOR, "canvas[aria-label='QR code']")
            if qr:
                print("📱 Обнаружен QR-код, сохраняем скриншот...")
                driver.save_screenshot("/tmp/qr.png")
                if application:
                    with open("/tmp/qr.png", "rb") as f:
                        asyncio.run_coroutine_threadsafe(
                            application.bot.send_photo(OWNER_ID, f, caption="🔐 Отсканируйте QR-код в WhatsApp Web"),
                            asyncio.get_event_loop()
                        )
                # Даём время на сканирование
                time.sleep(20)
                continue
        except:
            pass

        # Если ничего не нашли — делаем скриншот и отправляем раз в 30 секунд
        if attempt % 10 == 0:  # раз в ~30 секунд
            driver.save_screenshot("/tmp/debug.png")
            if application:
                with open("/tmp/debug.png", "rb") as f:
                    asyncio.run_coroutine_threadsafe(
                        application.bot.send_photo(OWNER_ID, f, caption=f"⚠️ Не могу войти (попытка {attempt+1})"),
                        asyncio.get_event_loop()
                    )

    # Если вышли из цикла — что-то пошло не так
    wa_ready = False
    print("❌ WhatsApp не готов после 60 секунд!")
    if application:
        asyncio.run_coroutine_threadsafe(
            application.bot.send_message(OWNER_ID, "❌ Не удалось авторизовать WhatsApp. Проверь логи."),
            asyncio.get_event_loop()
        )

# ========== ОТПРАВКА СООБЩЕНИЯ ==========
def send_whatsapp(phone: str, text: str):
    phone = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}"
    driver.get(url)

    try:
        input_box = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[contenteditable='true']"))
        )
        time.sleep(2)

        # Способ 1: Enter
        input_box.send_keys("\n")
        time.sleep(2)
        if input_box.text.strip() == "":
            return

        # Способ 2: кнопка отправки
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

        # Способ 3: клик + Enter
        input_box.click()
        input_box.send_keys("\n")
        time.sleep(2)
        if input_box.text.strip() == "":
            return

        raise Exception("Не удалось отправить")
    except Exception as e:
        raise Exception(f"Ошибка отправки: {e}")

# ========== КОМАНДЫ ТЕЛЕГРАМ ==========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Доступ запрещён")
        return
    await update.message.reply_text(
        "🤖 Бот готов!\n"
        "Формат: +79151234567 Текст\n"
        f"Статус WhatsApp: {'✅ Готов' if wa_ready else '⏳ Ожидание авторизации'}"
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not wa_ready:
        await update.message.reply_text("⏳ WhatsApp ещё не готов, подожди... (бот отправит QR в Telegram)")
        return

    text = update.message.text.strip()
    match = re.match(r"^(\+\d{10,15})\s+(.+)$", text, re.S)
    if not match:
        await update.message.reply_text("❌ Формат: +79151234567 Текст")
        return

    phone, message = match.groups()
    await update.message.reply_text(f"📤 Отправляю на {phone}...")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_whatsapp, phone, message)
        await update.message.reply_text("✅ Отправлено!")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ========== ЗАПУСК ==========
async def on_startup(app: Application):
    global application
    application = app
    print("🚀 Запуск WhatsApp в отдельном потоке...")
    threading.Thread(target=start_whatsapp, daemon=True).start()

def main():
    global application
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN не задан")
        return

    # Сброс вебхука
    import requests
    try:
        requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=True")
        print("✅ Вебхук сброшен")
    except Exception as e:
        print(f"⚠️ Ошибка сброса вебхука: {e}")

    app = Application.builder().token(TOKEN).build()
    application = app
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Запускаем инициализацию
    asyncio.get_event_loop().run_until_complete(on_startup(app))

    print("🚀 Бот запущен, ожидаем команды...")
    time.sleep(5)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

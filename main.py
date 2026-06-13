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
SESSION_DIR = "/app/chrome_session"  # Railway Volume mount path

driver = None
wa_ready = False
bot_app = None
main_loop = None


def get_driver():
    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-setuid-sandbox")
    # Важно: имитируем реальный браузер
    options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0.0.0 Safari/537.36")
    options.binary_location = "/usr/bin/google-chrome"
    
    service = Service(
        "/usr/bin/chromedriver",
        service_args=["--log-level=WARNING"]
    )
    return webdriver.Chrome(service=service, options=options)


def send_to_telegram(text, photo=None):
    """Потокобезопасная отправка сообщений в Telegram."""
    if not bot_app or not main_loop:
        print(f"[WA→TG] bot_app или main_loop не готов: {text}")
        return
    try:
        if photo:
            coro = bot_app.bot.send_photo(OWNER_ID, photo, caption=text)
        else:
            coro = bot_app.bot.send_message(OWNER_ID, text)
        future = asyncio.run_coroutine_threadsafe(coro, main_loop)
        future.result(timeout=10)  # ждём результат
    except Exception as e:
        print(f"[send_to_telegram] Ошибка: {e}")


def is_authorized():
    """Проверяем авторизацию в WhatsApp Web."""
    try:
        # Несколько вариантов селекторов на случай обновления WA
        selectors = [
            "div[data-testid='chat-list']",
            "div[aria-label='Список чатов']",
            "div[aria-label='Chat list']",
            "#pane-side",
        ]
        for sel in selectors:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return True
        return False
    except Exception:
        return False


def wait_for_qr():
    """Ищем QR-код и отправляем скриншот."""
    qr_selectors = [
        "canvas[aria-label='QR code']",
        "div[data-testid='qrcode']",
        "div[aria-label='QR code']",
        "canvas",  # fallback
    ]
    
    for attempt in range(20):  # ждём до 60 секунд
        time.sleep(3)
        
        # Может уже авторизован (сессия восстановлена)
        if is_authorized():
            return "authorized"
        
        for sel in qr_selectors:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
            if elements:
                print(f"✅ QR найден по селектору: {sel}")
                return "qr_found"
        
        print(f"[{attempt+1}/20] Ожидание QR или авторизации...")
    
    return "timeout"

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет скриншот текущего состояния браузера."""
    if update.effective_user.id != OWNER_ID:
        return
    if not driver:
        await update.message.reply_text("❌ Браузер не запущен")
        return
    try:
        png = driver.get_screenshot_as_png()
        url = driver.current_url
        await update.message.reply_photo(
            png, 
            caption=f"🔍 Текущий URL:\n{url}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка скриншота: {e}")
        
def start_whatsapp():
    global driver, wa_ready

    # Ждём пока main_loop будет готов
    for _ in range(30):
        if main_loop and main_loop.is_running():
            break
        time.sleep(1)
    
    print(f"📁 SESSION_DIR: {SESSION_DIR}")
    os.makedirs(SESSION_DIR, exist_ok=True)
    
    try:
        driver = get_driver()
        driver.get("https://web.whatsapp.com")
        print("🌐 WhatsApp Web загружен")
        
        # Ждём загрузки страницы
        time.sleep(5)
        
        result = wait_for_qr()
        
        if result == "authorized":
            wa_ready = True
            print("✅ WhatsApp авторизован (сессия восстановлена)")
            send_to_telegram("✅ WhatsApp готов! Сессия восстановлена автоматически.")
            return
        
        elif result == "qr_found":
            # Делаем скриншот только QR области
            png = driver.get_screenshot_as_png()
            send_to_telegram("📱 Отсканируйте QR-код в WhatsApp Web:", png)
            print("📤 QR отправлен в Telegram")
            
            # Ждём авторизации после сканирования
            print("⏳ Ожидаю сканирования QR...")
            for i in range(60):  # ждём до 5 минут
                time.sleep(5)
                if is_authorized():
                    wa_ready = True
                    print("✅ WhatsApp авторизован после QR!")
                    send_to_telegram("✅ WhatsApp авторизован! Можете отправлять сообщения.")
                    return
                if i % 6 == 5:
                    print(f"⏳ Всё ещё жду... ({(i+1)*5}с)")
            
            send_to_telegram("❌ QR не был отсканирован за 5 минут. Перезапустите бота.")
            print("❌ Таймаут ожидания QR")
        
        else:  # timeout
            png = driver.get_screenshot_as_png()
            send_to_telegram(
                "❌ Не удалось найти QR-код. Скриншот страницы:", 
                png
            )
            print("❌ QR не найден, скриншот отправлен")
            
    except Exception as e:
        print(f"❌ Ошибка WhatsApp: {e}")
        send_to_telegram(f"❌ Ошибка запуска WhatsApp: {e}")


def send_whatsapp(phone: str, text: str):
    """Отправка сообщения через WhatsApp Web."""
    from selenium.webdriver.common.keys import Keys
    import time
    
    phone = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}"
    
    print(f"[WA] Открываю URL: {url}")
    driver.get(url)
    
    # Ждём загрузки страницы
    time.sleep(5)
    
    # Делаем скриншот для отладки
    try:
        driver.save_screenshot("/app/debug_send.png")
        print("[WA] Скриншот сохранён: /app/debug_send.png")
    except Exception:
        pass
    
    # Проверяем — нет ли ошибки "номер не найден"
    page_source = driver.page_source
    if "phone number shared via url is invalid" in page_source.lower():
        raise Exception(f"Номер {phone} не найден в WhatsApp")
    
    # Список возможных селекторов поля ввода
    input_selectors = [
        "div[contenteditable='true'][data-tab='10']",
        "div[contenteditable='true'][data-tab='1']",
        "footer div[contenteditable='true']",
        "div[role='textbox']",
        "div[contenteditable='true']",
    ]
    
    input_box = None
    for sel in input_selectors:
        try:
            input_box = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            print(f"[WA] Поле ввода найдено: {sel}")
            break
        except Exception:
            print(f"[WA] Селектор не сработал: {sel}")
            continue
    
    if not input_box:
        # Финальный скриншот с ошибкой
        png = driver.get_screenshot_as_png()
        send_to_telegram("❌ Не найдено поле ввода WhatsApp:", png)
        raise Exception("Поле ввода не найдено ни по одному селектору")
    
    # Кликаем и отправляем
    try:
        input_box.click()
        time.sleep(1)
        input_box.send_keys(Keys.ENTER)
        time.sleep(2)
        print("[WA] Сообщение отправлено через Enter")
        return
    except Exception as e:
        print(f"[WA] Ошибка Enter: {e}")
    
    # Fallback: кнопка отправки
    send_selectors = [
        "button[data-testid='compose-btn-send']",
        "button[aria-label='Отправить']",
        "button[aria-label='Send']",
        "span[data-testid='send']",
        "button[data-tab='11']",
    ]
    
    for sel in send_selectors:
        try:
            btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            driver.execute_script("arguments[0].click();", btn)
            print(f"[WA] Отправлено кнопкой: {sel}")
            time.sleep(2)
            return
        except Exception:
            continue
    
    raise Exception("Не удалось отправить: ни Enter, ни кнопка не сработали")
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    status = "✅ готов" if wa_ready else "⏳ ожидает авторизации"
    await update.message.reply_text(
        f"Статус WhatsApp: {status}\n\n"
        "Формат: +79151234567 Текст сообщения"
    )


async def restart_wa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для перезапуска WhatsApp сессии."""
    if update.effective_user.id != OWNER_ID:
        return
    global driver, wa_ready
    await update.message.reply_text("🔄 Перезапускаю WhatsApp...")
    
    wa_ready = False
    if driver:
        try:
            driver.quit()
        except Exception:
            pass
    
    threading.Thread(target=start_whatsapp, daemon=True).start()


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not wa_ready:
        await update.message.reply_text(
            "⏳ WhatsApp не готов. Дождитесь авторизации.\n"
            "Используйте /restart для повторной попытки."
        )
        return
    
    m = re.match(r"^(\+\d{10,15})\s+(.+)$", update.message.text.strip(), re.S)
    if not m:
        await update.message.reply_text("Формат: +79151234567 Текст сообщения")
        return
    
    phone, msg = m.groups()
    await update.message.reply_text(f"📤 Отправляю на {phone}...")
    
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_whatsapp, phone, msg)
        await update.message.reply_text("✅ Отправлено!")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


def main():
    global bot_app, main_loop

    if not TOKEN:
        print("❌ Нет TELEGRAM_TOKEN")
        return

    # Сбрасываем вебхук
    import requests as req
    for attempt in range(3):
        try:
            r = req.get(
                f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"
                f"?drop_pending_updates=True",
                timeout=10
            )
            if r.status_code == 200:
                print("✅ Вебхук сброшен")
                break
        except Exception as e:
            print(f"⚠️ Попытка {attempt+1}: {e}")
            time.sleep(3)

    # Строим приложение с увеличенными таймаутами
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
    )
    
    bot_app = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .build()
    )
    
    bot_app.add_handler(CommandHandler("start", start_cmd))
    bot_app.add_handler(CommandHandler("restart", restart_wa_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Получаем event loop ДО запуска polling
    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    # Запускаем WhatsApp в отдельном потоке
    threading.Thread(target=start_whatsapp, daemon=True).start()
    
    print("🚀 Бот запущен")
    bot_app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
        # Увеличиваем таймауты polling
        poll_interval=2.0,
        timeout=20,
    )


if __name__ == "__main__":
    main()

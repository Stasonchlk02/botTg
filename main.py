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
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException

TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = "/app/chrome_session"

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
    options.add_argument("--lang=en-US")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    options.binary_location = "/usr/bin/google-chrome"
    service = Service(
        "/usr/bin/chromedriver",
        service_args=["--log-level=WARNING"]
    )
    return webdriver.Chrome(service=service, options=options)


def send_to_telegram(text, photo=None):
    if not bot_app or not main_loop:
        print(f"[WA→TG] bot_app или main_loop не готов: {text}")
        return
    try:
        if photo:
            coro = bot_app.bot.send_photo(OWNER_ID, photo, caption=text)
        else:
            coro = bot_app.bot.send_message(OWNER_ID, text)
        future = asyncio.run_coroutine_threadsafe(coro, main_loop)
        future.result(timeout=10)
    except Exception as e:
        print(f"[send_to_telegram] Ошибка: {e}")


def close_dialog_if_exists():
    """Закрывает div[role='dialog'] если он есть."""
    try:
        dialogs = driver.find_elements(By.CSS_SELECTOR, "div[role='dialog']")
        for dialog in dialogs:
            if not dialog.is_displayed():
                continue
            print("⚠️ Найден dialog, пробую закрыть")
            buttons = dialog.find_elements(
                By.XPATH, ".//button | .//div[@role='button']"
            )
            for btn in buttons:
                text = (btn.text or "").strip().lower()
                aria = (btn.get_attribute("aria-label") or "").strip().lower()
                keywords = [
                    "continue", "продолжить", "ok", "ок",
                    "понятно", "close", "закрыть", "not now",
                    "сейчас не надо", "got it"
                ]
                if any(w in text for w in keywords) or any(w in aria for w in keywords):
                    driver.execute_script("arguments[0].click();", btn)
                    print(f"✅ Dialog закрыт кнопкой: text='{btn.text}' aria='{aria}'")
                    time.sleep(1)
                    return
    except Exception as e:
        print(f"[close_dialog] Ошибка: {e}")


def close_blocking_popups():
    """Закрывает мешающие окна WhatsApp Web: 'Что нового', onboarding и т.п."""
    if not driver:
        return

    popup_selectors = [
        # Кнопки закрытия
        (By.CSS_SELECTOR, "button[aria-label='Close']"),
        (By.CSS_SELECTOR, "button[aria-label='Закрыть']"),
        (By.CSS_SELECTOR, "div[aria-label='Close']"),
        (By.CSS_SELECTOR, "div[aria-label='Закрыть']"),
        (By.CSS_SELECTOR, "span[data-testid='x']"),
        (By.CSS_SELECTOR, "span[data-testid='x-alt']"),

        # Кнопки подтверждения / продолжения
        (By.XPATH, "//button[contains(., 'Continue')]"),
        (By.XPATH, "//button[contains(., 'Продолжить')]"),
        (By.XPATH, "//button[contains(., 'OK')]"),
        (By.XPATH, "//button[contains(., 'ОК')]"),
        (By.XPATH, "//button[contains(., 'Понятно')]"),
        (By.XPATH, "//button[contains(., 'Got it')]"),
        (By.XPATH, "//button[contains(., 'Not now')]"),
        (By.XPATH, "//button[contains(., 'Сейчас не надо')]"),

        (By.XPATH, "//div[@role='button'][contains(., 'Continue')]"),
        (By.XPATH, "//div[@role='button'][contains(., 'Продолжить')]"),
        (By.XPATH, "//div[@role='button'][contains(., 'OK')]"),
        (By.XPATH, "//div[@role='button'][contains(., 'ОК')]"),
        (By.XPATH, "//div[@role='button'][contains(., 'Понятно')]"),
        (By.XPATH, "//div[@role='button'][contains(., 'Got it')]"),
        (By.XPATH, "//div[@role='button'][contains(., 'Not now')]"),
        (By.XPATH, "//div[@role='button'][contains(., 'Сейчас не надо')]"),
    ]

    for attempt in range(5):
        closed_any = False

        # Сначала проверяем диалоги
        close_dialog_if_exists()

        for by, selector in popup_selectors:
            try:
                elements = driver.find_elements(by, selector)
                for el in elements:
                    if el.is_displayed():
                        try:
                            driver.execute_script("arguments[0].click();", el)
                            print(f"✅ Закрыта всплывашка [{attempt+1}]: {selector}")
                            time.sleep(1)
                            closed_any = True
                        except Exception:
                            pass
            except Exception:
                pass

        # Fallback — Esc
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.ESCAPE)
            time.sleep(0.5)
        except Exception:
            pass

        if not closed_any:
            break


def is_authorized():
    """Проверяем авторизацию в WhatsApp Web."""
    try:
        close_blocking_popups()
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
        "canvas",
    ]

    for attempt in range(20):
        time.sleep(3)

        # Закрываем всплывашки перед каждой проверкой
        close_blocking_popups()

        # Может уже авторизован
        if is_authorized():
            return "authorized"

        for sel in qr_selectors:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
            if elements:
                print(f"✅ QR найден по селектору: {sel}")
                return "qr_found"

        print(f"[{attempt+1}/20] Ожидание QR или авторизации...")

    return "timeout"


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

        # Ждём загрузки страницы и закрываем всплывашки
        time.sleep(5)
        close_blocking_popups()

        result = wait_for_qr()

        if result == "authorized":
            wa_ready = True
            print("✅ WhatsApp авторизован (сессия восстановлена)")
            send_to_telegram("✅ WhatsApp готов! Сессия восстановлена автоматически.")
            return

        elif result == "qr_found":
            png = driver.get_screenshot_as_png()
            send_to_telegram("📱 Отсканируйте QR-код в WhatsApp Web:", png)
            print("📤 QR отправлен в Telegram")

            print("⏳ Ожидаю сканирования QR...")
            for i in range(60):
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

        else:
            png = driver.get_screenshot_as_png()
            send_to_telegram("❌ Не удалось найти QR-код. Скриншот страницы:", png)
            print("❌ QR не найден, скриншот отправлен")

    except Exception as e:
        print(f"❌ Ошибка WhatsApp: {e}")
        send_to_telegram(f"❌ Ошибка запуска WhatsApp: {e}")


def send_whatsapp(phone: str, text: str):
    """Отправка сообщения через WhatsApp Web."""
    phone = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}"

    print(f"[WA] Открываю URL: {url}")
    driver.get(url)
    time.sleep(5)

    # Закрываем всплывашки
    close_blocking_popups()
    time.sleep(2)

    # Проверяем ошибку номера
    page_source = driver.page_source
    if "phone number shared via url is invalid" in page_source.lower():
        raise Exception(f"Номер {phone} не найден в WhatsApp")

    # Ждём появления кнопки "Отправить" — она появляется когда текст вставлен
    send_selectors = [
        "button[data-testid='compose-btn-send']",
        "span[data-testid='send']",
        "button[aria-label='Send']",
        "button[aria-label='Отправить']",
        "button[data-tab='11']",
    ]

    # Сначала ждём пока текст подгрузится в поле ввода (URL уже содержит text=)
    time.sleep(3)

    # Пробуем найти и нажать кнопку отправки
    for sel in send_selectors:
        try:
            btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            driver.execute_script("arguments[0].click();", btn)
            print(f"[WA] ✅ Отправлено кнопкой: {sel}")
            time.sleep(2)
            return
        except Exception:
            print(f"[WA] Кнопка не найдена: {sel}")
            continue

    # Fallback: ищем кнопку через XPATH
    xpath_selectors = [
        "//button[@data-testid='compose-btn-send']",
        "//span[@data-testid='send']/ancestor::button",
        "//button[.//span[@data-icon='send']]",
    ]

    for sel in xpath_selectors:
        try:
            btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, sel))
            )
            driver.execute_script("arguments[0].click();", btn)
            print(f"[WA] ✅ Отправлено XPATH: {sel}")
            time.sleep(2)
            return
        except Exception:
            print(f"[WA] XPATH не сработал: {sel}")
            continue

    # Fallback 2: кликаем по span[data-icon='send'] напрямую
    try:
        send_icon = driver.find_element(By.CSS_SELECTOR, "span[data-icon='send']")
        parent = send_icon.find_element(By.XPATH, "./..")
        driver.execute_script("arguments[0].click();", parent)
        print("[WA] ✅ Отправлено через data-icon='send'")
        time.sleep(2)
        return
    except Exception:
        print("[WA] data-icon='send' не найден")

    # Последний fallback: ищем поле ввода и жмём Enter
    try:
        input_box = driver.find_element(By.CSS_SELECTOR, "div[contenteditable='true']")
        input_box.click()
        time.sleep(0.5)

        # Используем ActionChains для правильного Enter
        from selenium.webdriver.common.action_chains import ActionChains
        actions = ActionChains(driver)
        actions.send_keys(Keys.ENTER)
        actions.perform()
        print("[WA] ✅ Отправлено через ActionChains Enter")
        time.sleep(2)
        return
    except Exception as e:
        print(f"[WA] ActionChains тоже не сработал: {e}")

    # Если ничего не помогло — скриншот
    png = driver.get_screenshot_as_png()
    send_to_telegram("❌ Не удалось нажать кнопку отправки. Скриншот:", png)
    raise Exception("Не удалось отправить: ни одна кнопка/метод не сработали")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    status = "✅ готов" if wa_ready else "⏳ ожидает авторизации"
    await update.message.reply_text(
        f"Статус WhatsApp: {status}\n\n"
        "Формат: +79151234567 Текст сообщения\n\n"
        "Команды:\n"
        "/start — статус\n"
        "/debug — скриншот браузера\n"
        "/restart — перезапуск WhatsApp"
    )


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
        await update.message.reply_photo(png, caption=f"🔍 URL: {url}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка скриншота: {e}")


async def restart_wa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перезапуск WhatsApp сессии."""
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
            "⏳ WhatsApp не готов.\n"
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
    bot_app.add_handler(CommandHandler("debug", debug_cmd))
    bot_app.add_handler(CommandHandler("restart", restart_wa_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    threading.Thread(target=start_whatsapp, daemon=True).start()

    print("🚀 Бот запущен")
    bot_app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
        poll_interval=2.0,
        timeout=20,
    )


if __name__ == "__main__":
    main()

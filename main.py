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
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException

TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = "/app/chrome_session"

driver = None
wa_ready = False
bot_app = None
main_loop = None


# ==================== BROWSER ====================

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
    options.add_argument("--lang=ru-RU")
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


# ==================== TELEGRAM HELPER ====================

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


# ==================== POPUP / DIALOG HELPERS ====================

def get_element_text(el):
    """Надёжно получает текст элемента через JS."""
    try:
        txt = driver.execute_script("""
            return (arguments[0].innerText || arguments[0].textContent || '').trim();
        """, el)
        return txt or ""
    except Exception:
        try:
            return el.text or ""
        except Exception:
            return ""


def click_visible(el):
    """Надёжный клик по элементу."""
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", el
        )
        time.sleep(0.3)
    except Exception:
        pass
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)


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
                    print(f"✅ Dialog закрыт: text='{btn.text}' aria='{aria}'")
                    time.sleep(1)
                    return
    except Exception as e:
        print(f"[close_dialog] Ошибка: {e}")


def close_blocking_popups():
    """Закрывает мешающие окна WhatsApp Web."""
    if not driver:
        return

    popup_selectors = [
        (By.CSS_SELECTOR, "button[aria-label='Close']"),
        (By.CSS_SELECTOR, "button[aria-label='Закрыть']"),
        (By.CSS_SELECTOR, "div[aria-label='Close']"),
        (By.CSS_SELECTOR, "div[aria-label='Закрыть']"),
        (By.CSS_SELECTOR, "span[data-testid='x']"),
        (By.CSS_SELECTOR, "span[data-testid='x-alt']"),
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

        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.ESCAPE)
            time.sleep(0.5)
        except Exception:
            pass

        if not closed_any:
            break


def click_continue_screens():
    """Закрывает промежуточные экраны типа Continue to chat."""
    texts = [
        "Continue to chat",
        "Продолжить чат",
        "Use WhatsApp Web",
        "Использовать WhatsApp Web",
        "Continue",
        "Продолжить",
        "OK",
        "ОК",
        "Open",
        "Открыть",
    ]

    clicked = False
    for txt in texts:
        xpath = (
            f"//button[contains(., '{txt}')] | "
            f"//div[@role='button'][contains(., '{txt}')] | "
            f"//a[contains(., '{txt}')]"
        )
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                if el.is_displayed():
                    click_visible(el)
                    print(f"✅ Нажата промежуточная кнопка: {txt}")
                    time.sleep(2)
                    clicked = True
        except Exception:
            pass

    return clicked


# ==================== WHATSAPP AUTH ====================

def is_authorized():
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
    qr_selectors = [
        "canvas[aria-label='QR code']",
        "div[data-testid='qrcode']",
        "div[aria-label='QR code']",
        "canvas",
    ]

    for attempt in range(20):
        time.sleep(3)
        close_blocking_popups()

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

        time.sleep(5)
        close_blocking_popups()

        result = wait_for_qr()

        if result == "authorized":
            wa_ready = True
            print("✅ WhatsApp авторизован (сессия восстановлена)")
            send_to_telegram("✅ WhatsApp готов! Сессия восстановлена.")
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
                    send_to_telegram("✅ WhatsApp авторизован!")
                    return
                if i % 6 == 5:
                    print(f"⏳ Всё ещё жду... ({(i+1)*5}с)")

            send_to_telegram("❌ QR не отсканирован за 5 минут. /restart")
            print("❌ Таймаут QR")

        else:
            png = driver.get_screenshot_as_png()
            send_to_telegram("❌ QR не найден. Скриншот:", png)
            print("❌ QR не найден")

    except Exception as e:
        print(f"❌ Ошибка WhatsApp: {e}")
        send_to_telegram(f"❌ Ошибка запуска WhatsApp: {e}")


# ==================== SEND MESSAGE HELPERS ====================

def find_composer():
    """Ищет поле ввода сообщения в открытом чате."""
    selectors = [
        "footer div[contenteditable='true'][role='textbox']",
        "footer div[contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
    ]
    for sel in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in elements:
                if el.is_displayed():
                    return el
        except Exception:
            pass
    return None


def wait_for_composer(timeout=20):
    """Ждём появления поля ввода в открытом чате."""
    end_time = time.time() + timeout
    while time.time() < end_time:
        close_blocking_popups()
        click_continue_screens()

        composer = find_composer()
        if composer:
            return composer

        time.sleep(1)
    return None


def open_draft_chat(phone: str, text: str):
    """Ищет нужный черновик слева и открывает его."""
    phone_digits = re.sub(r"\D", "", phone)
    phone_tail10 = phone_digits[-10:] if len(phone_digits) >= 10 else phone_digits
    phone_tail7 = phone_digits[-7:] if len(phone_digits) >= 7 else phone_digits

    msg_hint = (text or "").strip().lower()
    msg_hint_short = msg_hint[:20] if msg_hint else ""

    try:
        rows = driver.find_elements(
            By.CSS_SELECTOR,
            "#pane-side [role='listitem'], "
            "#pane-side [data-testid='cell-frame-container']"
        )
    except Exception:
        rows = []

    print(f"[WA] Найдено строк чатов слева: {len(rows)}")

    visible_rows = []

    for idx, row in enumerate(rows):
        try:
            if not row.is_displayed():
                continue

            txt = get_element_text(row).strip().lower()
            digits = re.sub(r"\D", "", txt)

            if idx < 10:
                print(f"[WA] row[{idx}] = {txt[:120]!r}")

            visible_rows.append((row, txt, digits))
        except Exception:
            pass

    # 1. Идеальный вариант: строка с черновиком и нашим текстом/номером
    for row, txt, digits in visible_rows:
        if "черновик" in txt or "draft" in txt:
            if (msg_hint_short and msg_hint_short in txt) or \
               (phone_tail10 and phone_tail10 in digits) or \
               (phone_tail7 and phone_tail7 in digits):
                print(f"[WA] ✅ Нашёл нужный черновик: {txt[:120]}")
                click_visible(row)
                time.sleep(2)
                return True

    # 2. Поиск по номеру
    for row, txt, digits in visible_rows:
        if (phone_tail10 and phone_tail10 in digits) or \
           (phone_tail7 and phone_tail7 in digits):
            print(f"[WA] ✅ Нашёл чат по номеру: {txt[:120]}")
            click_visible(row)
            time.sleep(2)
            return True

    # 3. Поиск по фрагменту текста сообщения
    if msg_hint_short:
        for row, txt, digits in visible_rows:
            if msg_hint_short in txt:
                print(f"[WA] ✅ Нашёл чат по тексту: {txt[:120]}")
                click_visible(row)
                time.sleep(2)
                return True

    # 4. Просто любой черновик
    for row, txt, digits in visible_rows:
        if "черновик" in txt or "draft" in txt:
            print(f"[WA] ✅ Нашёл хотя бы черновик: {txt[:120]}")
            click_visible(row)
            time.sleep(2)
            return True

    print("[WA] ❌ Не удалось найти нужный черновик/чат")
    return False


def click_send_button(timeout=15):
    """Ищет и нажимает кнопку отправки в футере чата."""
    end_time = time.time() + timeout

    while time.time() < end_time:
        close_blocking_popups()

        # 1. Селекторы внутри footer
        selectors = [
            "footer button[aria-label='Send']",
            "footer button[aria-label='Отправить']",
            "footer [data-testid='compose-btn-send']",
            "footer button[data-testid='compose-btn-send']",
            "footer span[data-testid='send']",
            "footer span[data-icon='send']",
        ]

        for sel in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in elements:
                    if not el.is_displayed():
                        continue
                    try:
                        parent_btn = el.find_element(
                            By.XPATH,
                            "./ancestor::button[1] | ./ancestor::*[@role='button'][1]"
                        )
                        if parent_btn.is_displayed():
                            click_visible(parent_btn)
                            print(f"[WA] ✅ Send через parent: {sel}")
                            time.sleep(2)
                            return True
                    except Exception:
                        pass

                    click_visible(el)
                    print(f"[WA] ✅ Send напрямую: {sel}")
                    time.sleep(2)
                    return True
            except Exception:
                pass

        # 2. Без footer — глобальный поиск
        global_selectors = [
            "button[data-testid='compose-btn-send']",
            "span[data-testid='send']",
            "span[data-icon='send']",
            "button[aria-label='Send']",
            "button[aria-label='Отправить']",
        ]

        for sel in global_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in elements:
                    if not el.is_displayed():
                        continue
                    try:
                        parent_btn = el.find_element(
                            By.XPATH,
                            "./ancestor::button[1] | ./ancestor::*[@role='button'][1]"
                        )
                        if parent_btn.is_displayed():
                            click_visible(parent_btn)
                            print(f"[WA] ✅ Global send через parent: {sel}")
                            time.sleep(2)
                            return True
                    except Exception:
                        pass

                    click_visible(el)
                    print(f"[WA] ✅ Global send: {sel}")
                    time.sleep(2)
                    return True
            except Exception:
                pass

        # 3. JS fallback
        try:
            result = driver.execute_script("""
                var icons = document.querySelectorAll('span[data-icon="send"]');
                for (var i = 0; i < icons.length; i++) {
                    var btn = icons[i].closest('button');
                    if (btn && btn.offsetParent !== null) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            """)
            if result:
                print("[WA] ✅ JS клик по send")
                time.sleep(2)
                return True
        except Exception:
            pass

        # 4. Последняя кнопка в footer
        try:
            footer = driver.find_element(By.TAG_NAME, "footer")
            btns = footer.find_elements(By.CSS_SELECTOR, "button, [role='button']")
            visible_btns = [b for b in btns if b.is_displayed()]
            if visible_btns:
                last_btn = visible_btns[-1]
                click_visible(last_btn)
                print("[WA] ✅ Последняя кнопка в footer")
                time.sleep(2)
                return True
        except Exception:
            pass

        time.sleep(1)

    return False


# ==================== SEND MESSAGE ====================

def send_whatsapp(phone: str, text: str):
    """Отправка сообщения через WhatsApp Web."""
    phone_clean = re.sub(r"\D", "", phone)
    url = (
        f"https://web.whatsapp.com/send"
        f"?phone={phone_clean}"
        f"&text={quote(text)}"
        f"&type=phone_number"
        f"&app_absent=0"
    )

    print(f"[WA] Открываю URL: {url}")
    driver.get(url)

    # Даём странице загрузиться
    time.sleep(8)
    close_blocking_popups()
    click_continue_screens()
    time.sleep(2)

    # Проверка на ошибку номера
    page_source = driver.page_source.lower()
    if "phone number shared via url is invalid" in page_source:
        raise Exception(f"Номер {phone_clean} не найден в WhatsApp")

    # Скриншот для отладки
    png_step1 = driver.get_screenshot_as_png()
    send_to_telegram("🔍 Шаг 1: после загрузки URL", png_step1)

    # Ждём открытия чата (поле ввода справа)
    composer = wait_for_composer(timeout=10)

    # Если чат не открылся — пробуем кликнуть по черновику слева
    if not composer:
        print("[WA] Чат справа не открылся, ищу черновик слева...")
        png_step2 = driver.get_screenshot_as_png()
        send_to_telegram("🔍 Шаг 2: чат не открылся, ищу черновик", png_step2)

        opened = open_draft_chat(phone_clean, text)
        if opened:
            time.sleep(2)
            close_blocking_popups()
            composer = wait_for_composer(timeout=10)

    # Если всё ещё нет — сдаёмся
    if not composer:
        png_fail = driver.get_screenshot_as_png()
        send_to_telegram("❌ Чат не открылся. Скриншот:", png_fail)
        raise Exception("Чат не открылся для отправки")

    print("[WA] ✅ Поле ввода найдено")
    png_step3 = driver.get_screenshot_as_png()
    send_to_telegram("🔍 Шаг 3: поле ввода найдено", png_step3)

    # Нажимаем кнопку отправки
    sent = click_send_button(timeout=15)
    if sent:
        print("[WA] ✅ Сообщение отправлено!")
        return

    # Fallback: фокус на composer + Enter
    print("[WA] Кнопка send не найдена, пробую Enter...")
    try:
        click_visible(composer)
        time.sleep(1)
        composer.send_keys(Keys.ENTER)
        print("[WA] ⚠️ Fallback: Enter в composer")
        time.sleep(2)

        png_step4 = driver.get_screenshot_as_png()
        send_to_telegram("🔍 Шаг 4: после Enter", png_step4)
        return
    except Exception as e:
        print(f"[WA] Enter тоже не сработал: {e}")

    png_final = driver.get_screenshot_as_png()
    send_to_telegram("❌ Не удалось отправить. Скриншот:", png_final)
    raise Exception("Не удалось отправить сообщение")


# ==================== TELEGRAM HANDLERS ====================

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


# ==================== MAIN ====================

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

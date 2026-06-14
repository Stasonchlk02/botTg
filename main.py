import asyncio
import os
import re
import time
import threading
import sqlite3
from urllib.parse import quote
from io import BytesIO

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

from pypdf import PdfReader

# -------------------- КОНФИГУРАЦИЯ --------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = "/app/chrome_session"
DB_PATH = "/app/data/phones.db"

driver = None
wa_ready = False
bot_app = None
main_loop = None

# -------------------- БАЗА ДАННЫХ --------------------
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS phones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE NOT NULL,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent INTEGER DEFAULT 0,
                sent_at TIMESTAMP
            )
        """)
        conn.commit()

def normalize_phone(raw: str) -> str | None:
    """Приводит российский номер к формату 79XXXXXXXXX (11 цифр)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        if digits[0] == "8":
            digits = "7" + digits[1:]
        return digits
    return None

def extract_phones(text: str) -> list[str]:
    """Извлекает все российские номера из текста (разные форматы)."""
    pattern = r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'
    raw_matches = re.findall(pattern, text)
    phones = []
    for raw in raw_matches:
        norm = normalize_phone(raw)
        if norm:
            phones.append(norm)
    return list(dict.fromkeys(phones))

def add_phones(phones: list[str], user_id: int) -> tuple[int, int]:
    """
    Добавляет номера в базу.
    Возвращает (добавлено новых, всего уникальных передано).
    """
    unique_phones = list(dict.fromkeys(phones))
    new_count = 0
    with sqlite3.connect(DB_PATH) as conn:
        for phone in unique_phones:
            try:
                conn.execute(
                    "INSERT INTO phones (phone, added_by) VALUES (?, ?)",
                    (phone, user_id)
                )
                new_count += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    return new_count, len(unique_phones)

def delete_phones(phones: list[str]) -> int:
    """Удаляет указанные номера из базы. Возвращает количество удалённых."""
    if not phones:
        return 0
    with sqlite3.connect(DB_PATH) as conn:
        placeholders = ','.join('?' for _ in phones)
        cur = conn.execute(
            f"DELETE FROM phones WHERE phone IN ({placeholders})",
            phones
        )
        conn.commit()
        return cur.rowcount

def delete_sent_phones() -> int:
    """Удаляет все номера с sent=1. Возвращает количество удалённых."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM phones WHERE sent = 1")
        conn.commit()
        return cur.rowcount

def delete_all_phones() -> int:
    """Удаляет все номера. Возвращает количество удалённых."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM phones")
        conn.commit()
        return cur.rowcount

def get_unsent_phones() -> list[str]:
    """Номера, для которых ещё не было отправки (sent=0)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT phone FROM phones WHERE sent = 0 ORDER BY added_at"
        ).fetchall()
        return [row[0] for row in rows]

def mark_sent(phone: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE phones SET sent = 1, sent_at = CURRENT_TIMESTAMP WHERE phone = ?",
            (phone,)
        )
        conn.commit()

def get_stats() -> tuple[int, int, int]:
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM phones").fetchone()[0]
        sent = conn.execute("SELECT COUNT(*) FROM phones WHERE sent = 1").fetchone()[0]
        unsent = total - sent
        return total, sent, unsent

def reset_sent(phone: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("UPDATE phones SET sent = 0, sent_at = NULL WHERE phone = ?", (phone,))
        conn.commit()
        return cur.rowcount > 0

def reset_all_sent():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE phones SET sent = 0, sent_at = NULL")
        conn.commit()

# -------------------- BROWSER / SELENIUM (ВАШ ОРИГИНАЛЬНЫЙ КОД БЕЗ ИЗМЕНЕНИЙ) --------------------
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

def get_element_text(el):
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

def find_composer():
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

def send_whatsapp(phone: str, text: str):
    phone_clean = re.sub(r"\D", "", phone)
    url = f"https://web.whatsapp.com/send?phone={phone_clean}&text={quote(text)}"
    print(f"[WA] Открываю URL: {url}")
    driver.get(url)

    time.sleep(10)

    # Простой метод – ждём кнопку Отправить
    selectors = [
        "button[aria-label='Отправить']",
        "button[aria-label='Send']",
        "span[data-testid='send']",
        "span[data-icon='send']",
    ]
    for sel in selectors:
        try:
            btn = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            driver.execute_script("arguments[0].click();", btn)
            print("[WA] ✅ Отправлено простым методом")
            return
        except TimeoutException:
            continue

    print("[WA] Простой метод не сработал, перехожу к расширенному алгоритму...")
    close_blocking_popups()
    click_continue_screens()
    time.sleep(2)

    page_source = driver.page_source.lower()
    if "phone number shared via url is invalid" in page_source:
        raise Exception(f"Номер {phone_clean} не найден в WhatsApp")

    png_step1 = driver.get_screenshot_as_png()
    send_to_telegram("Шаг 1: после загрузки URL", png_step1)

    composer = wait_for_composer(timeout=10)

    if not composer:
        print("[WA] Чат справа не открылся, ищу черновик слева...")
        png_step2 = driver.get_screenshot_as_png()
        send_to_telegram("Шаг 2: чат не открылся, ищу черновик", png_step2)
        opened = open_draft_chat(phone_clean, text)
        if opened:
            time.sleep(2)
            close_blocking_popups()
            composer = wait_for_composer(timeout=10)

    if not composer:
        png_fail = driver.get_screenshot_as_png()
        send_to_telegram("❌ Чат не открылся. Скриншот:", png_fail)
        raise Exception("Чат не открылся для отправки")

    print("[WA] ✅ Поле ввода найдено")
    png_step3 = driver.get_screenshot_as_png()
    send_to_telegram("Шаг 3: поле ввода найдено", png_step3)

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
        send_to_telegram("Шаг 4: после Enter", png_step4)
        return
    except Exception as e:
        print(f"[WA] Enter тоже не сработал: {e}")
        png_final = driver.get_screenshot_as_png()
        send_to_telegram("❌ Не удалось отправить. Скриншот:", png_final)
        raise Exception("Не удалось отправить сообщение")

# -------------------- TELEGRAM HANDLERS --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    status = "✅ готов" if wa_ready else "⏳ ожидает авторизации"
    await update.message.reply_text(
        f"Статус WhatsApp: {status}\n\n"
        "📱 Отправьте номер или PDF с номерами — они сохранятся.\n\n"
        "👑 Команды владельца:\n"
        "/send номера Текст — отправка конкретным\n"
        "/broadcast Текст — рассылка всем неотправленным\n"
        "/stats — статистика\n"
        "/list — неотправленные номера\n"
        "/reset номер — сбросить статус\n"
        "/reset_all — сбросить статус всем\n"
        "/delete номера — удалить номера (через запятую)\n"
        "/delete_sent — удалить отправленные номера\n"
        "/delete_all confirm — удалить ВСЕ номера (осторожно!)\n"
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

async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /send номер1,номер2 Текст сообщения")
        return

    raw_phones = context.args[0].split(",")
    text = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    if not text:
        await update.message.reply_text("Укажите текст сообщения после номеров")
        return

    phones = []
    for rp in raw_phones:
        norm = normalize_phone(rp.strip())
        if norm:
            phones.append(norm)
    if not phones:
        await update.message.reply_text("Не найдено ни одного корректного российского номера")
        return

    added, total = add_phones(phones, OWNER_ID)
    await update.message.reply_text(
        f"📤 Начинаю отправку на {len(phones)} номер(ов)\n"
        f"(➕ новых в базе: {added}, уже было: {total - added})"
    )

    for phone in phones:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, send_whatsapp, phone, text)
            mark_sent(phone)
            send_to_telegram(f"✅ Отправлено на {phone}")
        except Exception as e:
            send_to_telegram(f"❌ Ошибка при отправке на {phone}: {e}")
        await asyncio.sleep(15)
    await update.message.reply_text("✅ Индивидуальная рассылка завершена")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /broadcast Текст сообщения")
        return

    text = " ".join(context.args)
    phones = get_unsent_phones()
    if not phones:
        await update.message.reply_text("Нет неотправленных номеров")
        return

    await update.message.reply_text(f"📣 Начинаю рассылку на {len(phones)} номеров...")
    for i, phone in enumerate(phones, 1):
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, send_whatsapp, phone, text)
            mark_sent(phone)
            send_to_telegram(f"✅ [{i}/{len(phones)}] {phone}")
        except Exception as e:
            send_to_telegram(f"❌ [{i}/{len(phones)}] {phone}: {e}")
        await asyncio.sleep(15)
    await update.message.reply_text("✅ Массовая рассылка завершена")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    total, sent, unsent = get_stats()
    pct = (sent / total * 100) if total > 0 else 0
    await update.message.reply_text(
        f"📊 Статистика номеров:\n"
        f"• Всего в базе: {total}\n"
        f"• Отправлено: {sent} ({pct:.1f}%)\n"
        f"• Осталось отправить: {unsent}"
    )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    total, sent, unsent = get_stats()
    if unsent == 0:
        await update.message.reply_text("✅ Все номера уже отправлены!")
        return
    phones = get_unsent_phones()
    text = f"📋 Неотправленные номера (всего {unsent}):\n" + "\n".join(phones[:50])
    if len(phones) > 50:
        text += f"\n\n... и ещё {len(phones) - 50} (показаны первые 50)"
    await update.message.reply_text(text)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /reset 79161234567")
        return
    phone = normalize_phone(context.args[0])
    if not phone:
        await update.message.reply_text("Некорректный номер")
        return
    if reset_sent(phone):
        await update.message.reply_text(f"✅ Статус для {phone} сброшен (теперь не отправлен)")
    else:
        await update.message.reply_text("Номер не найден в базе")

async def reset_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    reset_all_sent()
    total, _, _ = get_stats()
    await update.message.reply_text(f"✅ Статус отправки сброшен для всех {total} номеров")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет указанные номера (через запятую)."""
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Формат: /delete 79161234567,79261234567")
        return
    raw_phones = context.args[0].split(",")
    phones_to_delete = []
    for rp in raw_phones:
        norm = normalize_phone(rp.strip())
        if norm:
            phones_to_delete.append(norm)
    if not phones_to_delete:
        await update.message.reply_text("Не найдено корректных номеров для удаления")
        return
    deleted = delete_phones(phones_to_delete)
    await update.message.reply_text(f"🗑 Удалено номеров: {deleted} из {len(phones_to_delete)} указанных")

async def delete_sent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет все номера, которые уже были отправлены."""
    if update.effective_user.id != OWNER_ID:
        return
    deleted = delete_sent_phones()
    await update.message.reply_text(f"🗑 Удалено отправленных номеров: {deleted}")

async def delete_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет всю базу номеров. Требует подтверждения через аргумент 'confirm'."""
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or context.args[0].lower() != "confirm":
        await update.message.reply_text(
            "⚠️ Вы собираетесь удалить ВСЕ номера из базы.\n"
            "Для подтверждения введите: /delete_all confirm"
        )
        return
    deleted = delete_all_phones()
    await update.message.reply_text(f"🗑 База очищена. Удалено номеров: {deleted}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает входящий текст: извлечение номеров ИЛИ быстрая отправка для владельца."""
    msg_text = update.message.text.strip()
    # Быстрая отправка для владельца: +79161234567 Текст
    if update.effective_user.id == OWNER_ID:
        m = re.match(r"^(\+\d{10,15})\s+(.+)$", msg_text, re.S)
        if m:
            phone_raw, text = m.groups()
            norm = normalize_phone(phone_raw)
            if norm:
                add_phones([norm], OWNER_ID)
                await update.message.reply_text(f"📤 Отправляю на {norm}...")
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, send_whatsapp, norm, text)
                    mark_sent(norm)
                    await update.message.reply_text("✅ Отправлено!")
                except Exception as e:
                    await update.message.reply_text(f"❌ Ошибка: {e}")
                return
            else:
                await update.message.reply_text("Некорректный номер телефона")
                return

    # Для всех: извлечение номеров из текста
    phones = extract_phones(msg_text)
    if not phones:
        await update.message.reply_text("❌ Российские номера не найдены.")
        return
    new, total = add_phones(phones, update.effective_user.id)
    await update.message.reply_text(
        f"✅ Найдено номеров: {total}. "
        f"Добавлено новых: {new}" +
        (f" (уже были в базе: {total - new})" if total - new > 0 else "")
    )

async def pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает PDF-файл, извлекает номера."""
    if not update.message.document:
        return
    if update.message.document.mime_type != "application/pdf":
        await update.message.reply_text("Пожалуйста, пришлите PDF-файл.")
        return

    try:
        file = await update.message.document.get_file()
        buf = BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)

        reader = PdfReader(buf)
        full_text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                full_text += page_text + "\n"

        if not full_text.strip():
            await update.message.reply_text("Не удалось извлечь текст из PDF.")
            return

        phones = extract_phones(full_text)
        if not phones:
            await update.message.reply_text("❌ Российские номера в PDF не найдены.")
            return

        new, total = add_phones(phones, update.effective_user.id)
        await update.message.reply_text(
            f"✅ Из PDF извлечено номеров: {total}. "
            f"Добавлено новых: {new}" +
            (f" (уже были в базе: {total - new})" if total - new > 0 else "")
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при обработке PDF: {e}")

# -------------------- MAIN --------------------
def main():
    global bot_app, main_loop

    if not TOKEN:
        print("❌ Нет TELEGRAM_TOKEN")
        return

    init_db()

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

    # Старые команды
    bot_app.add_handler(CommandHandler("start", start_cmd))
    bot_app.add_handler(CommandHandler("debug", debug_cmd))
    bot_app.add_handler(CommandHandler("restart", restart_wa_cmd))

    # Новые команды рассылки и управления
    bot_app.add_handler(CommandHandler("send", send_cmd))
    bot_app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    bot_app.add_handler(CommandHandler("stats", stats_cmd))
    bot_app.add_handler(CommandHandler("list", list_cmd))
    bot_app.add_handler(CommandHandler("reset", reset_cmd))
    bot_app.add_handler(CommandHandler("reset_all", reset_all_cmd))
    bot_app.add_handler(CommandHandler("delete", delete_cmd))
    bot_app.add_handler(CommandHandler("delete_sent", delete_sent_cmd))
    bot_app.add_handler(CommandHandler("delete_all", delete_all_cmd))

    # Приём номеров: текст и PDF
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    bot_app.add_handler(MessageHandler(filters.Document.PDF, pdf_handler))

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

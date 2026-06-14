import asyncio
import os
import re
import time
import threading
import io
import sqlite3
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

from PyPDF2 import PdfReader

# ==================== CONFIG ====================
TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = "/app/chrome_session"
DB_PATH = "numbers.db"

driver = None
wa_ready = False
bot_app = None
main_loop = None
db_lock = threading.Lock()

# ==================== DATABASE ====================
def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE NOT NULL,
                added_by INTEGER NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

def add_number(phone: str, user_id: int) -> bool:
    """Добавляет номер, если его ещё нет. Возвращает True, если добавлен."""
    with db_lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        try:
            conn.execute("INSERT INTO numbers (phone, added_by) VALUES (?, ?)", (phone, user_id))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

def get_unsent_numbers():
    """Возвращает список номеров, где sent=0."""
    with db_lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = conn.execute("SELECT phone FROM numbers WHERE sent = 0")
        numbers = [row[0] for row in cur.fetchall()]
        conn.close()
        return numbers

def mark_sent(phone: str):
    with db_lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("UPDATE numbers SET sent = 1 WHERE phone = ?", (phone,))
        conn.commit()
        conn.close()

def reset_all_sent():
    with db_lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("UPDATE numbers SET sent = 0")
        conn.commit()
        conn.close()

def get_stats():
    with db_lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        total = conn.execute("SELECT COUNT(*) FROM numbers").fetchone()[0]
        unsent = conn.execute("SELECT COUNT(*) FROM numbers WHERE sent = 0").fetchone()[0]
        conn.close()
        return total, unsent

def get_all_numbers():
    with db_lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = conn.execute("SELECT phone, sent FROM numbers ORDER BY id")
        rows = cur.fetchall()
        conn.close()
        return rows

# ==================== PHONE VALIDATION ====================
def clean_russian_phone(raw: str) -> str | None:
    """
    Приводит российский номер к виду +7XXXXXXXXXX.
    Возвращает None, если номер невалидный.
    """
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 11 and digits[0] in ('7', '8'):
        return '+7' + digits[1:]
    elif len(digits) == 10 and digits[0] == '9':
        return '+7' + digits
    return None

def extract_russian_phones(text: str) -> list[str]:
    """Извлекает все уникальные валидные российские номера из текста."""
    # Ищем потенциальные номера: +7/8 с кодом и номером, допуская разделители
    pattern = r'(\+?[78][\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2})'
    candidates = re.findall(pattern, text)
    cleaned = set()
    for c in candidates:
        phone = clean_russian_phone(c)
        if phone:
            cleaned.add(phone)
    return list(cleaned)

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
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
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
                By.XPATH,
                ".//button | .//div[@role='button']"
            )
            for btn in buttons:
                text = (btn.text or "").lower()
                if "close" in text or "cancel" in text or "not now" in text:
                    click_visible(btn)
                    time.sleep(0.5)
                    break
    except Exception as e:
        print(f"close_dialog_if_exists: {e}")

def is_modal_present():
    try:
        modals = driver.find_elements(By.CSS_SELECTOR, "[role='dialog'], .modal, .popup")
        for modal in modals:
            if modal.is_displayed():
                return True
    except Exception:
        pass
    return False

def ensure_wa_ready():
    global wa_ready
    if wa_ready:
        return True

    print("🔄 Ожидание полной загрузки WhatsApp Web...")
    try:
        wait = WebDriverWait(driver, 120)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='chat-list'], div[data-testid='list']")))
        time.sleep(3)
        close_dialog_if_exists()
        time.sleep(1)

        if is_modal_present():
            print("⚠️ Обнаружен popup после закрытия диалогов, нажимаем ESC")
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            time.sleep(1)

        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='chat-list-search'], div[role='textbox']")))
        close_dialog_if_exists()

        print("✅ WhatsApp Web полностью загружен")
        wa_ready = True
        return True

    except Exception as e:
        print(f"❌ Ошибка загрузки WhatsApp Web: {e}")
        send_to_telegram(f"❌ Ошибка загрузки WhatsApp Web: {e}")
        return False

# ==================== WHATSAPP SENDER ====================
def send_whatsapp(phone, message):
    if not driver or not wa_ready:
        if not ensure_wa_ready():
            return False, "WhatsApp Web не готов"

    try:
        phone = re.sub(r'[^\d+]', '', phone.strip())
        if not phone.startswith('+'):
            phone = '+' + phone

        encoded_phone = quote(phone)
        url = f"https://web.whatsapp.com/send?phone={encoded_phone}"
        driver.get(url)
        time.sleep(5)
        close_dialog_if_exists()

        wait = WebDriverWait(driver, 60)
        msg_box = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[contenteditable='true'][data-testid='conversation-compose-box-input'], div[contenteditable='true'][data-testid='conversation-compose-box-input'] p"))
        )

        click_visible(msg_box)
        time.sleep(0.5)
        msg_box.send_keys(message)
        time.sleep(0.5)

        try:
            send_btn = driver.find_element(By.CSS_SELECTOR, "button[data-testid='send-button']")
            if send_btn.is_enabled():
                click_visible(send_btn)
            else:
                raise Exception("Кнопка неактивна")
        except Exception:
            msg_box.send_keys(Keys.RETURN)

        time.sleep(2)

        try:
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "span[data-testid='msg-dblcheck'], span[data-testid='msg-check']")))
            send_to_telegram(f"✅ Сообщение успешно отправлено на {phone}")
            return True, "Сообщение отправлено"
        except TimeoutException:
            if "не зарегистрирован" in driver.page_source.lower():
                return False, "Номер не зарегистрирован в WhatsApp"
            return False, "Не удалось подтвердить отправку"

    except Exception as e:
        error_msg = str(e)
        print(f"Ошибка отправки: {error_msg}")
        if "not registered" in error_msg.lower():
            return False, "Номер не зарегистрирован в WhatsApp"
        return False, f"Ошибка: {error_msg}"

# ==================== BACKGROUND WA CHECK ====================
def wa_session_worker():
    global driver, wa_ready
    while True:
        try:
            if not driver:
                print("🚀 Запуск браузера для WhatsApp...")
                driver = get_driver()
                driver.get("https://web.whatsapp.com")
                ensure_wa_ready()
                send_to_telegram("✅ WhatsApp Web сессия активна")
            else:
                try:
                    driver.current_url
                    if not wa_ready:
                        ensure_wa_ready()
                    else:
                        if "web.whatsapp.com" not in driver.current_url:
                            driver.get("https://web.whatsapp.com")
                            time.sleep(5)
                            ensure_wa_ready()
                except Exception:
                    print("⚠️ Браузер умер, перезапуск...")
                    driver.quit()
                    driver = None
                    wa_ready = False
                    driver = get_driver()
                    driver.get("https://web.whatsapp.com")
                    ensure_wa_ready()
                    send_to_telegram("🔄 WhatsApp Web перезапущен")
        except Exception as e:
            print(f"Ошибка в wa_session_worker: {e}")
        time.sleep(30)

# ==================== TELEGRAM HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text(
        "🤖 WhatsApp Bot активен\n"
        "📌 Команды владельца:\n"
        "/wa 79123456789 Текст — быстрое сообщение\n"
        "/list — список всех номеров (файл)\n"
        "/unsent — количество неотправленных\n"
        "/broadcast Текст — рассылка по всем неотправленным\n"
        "/reset — сбросить статусы отправки\n\n"
        "📥 Любой пользователь может прислать номер или PDF с номерами — они сохранятся."
    )

async def wa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    if not context.args:
        await update.message.reply_text("❌ Пример: /wa 79123456789 Текст сообщения")
        return

    phone = context.args[0]
    message = " ".join(context.args[1:])
    if not message:
        await update.message.reply_text("❌ Введите текст сообщения")
        return

    await update.message.reply_text(f"📤 Отправляю сообщение на {phone}...")

    def send_in_thread():
        success, result = send_whatsapp(phone, message)
        if success:
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(f"✅ {result} на {phone}"),
                main_loop
            )
        else:
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(f"❌ {result}"),
                main_loop
            )

    thread = threading.Thread(target=send_in_thread)
    thread.start()

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста от любого пользователя."""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text.startswith('/'):
        return

    # 1. Проверяем старый формат для владельца: "номер текст"
    if user_id == OWNER_ID:
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            phone_candidate = parts[0]
            msg_text = parts[1]
            phone = clean_russian_phone(phone_candidate)
            if phone:
                # Отправляем сразу
                await update.message.reply_text(f"📤 Отправляю на {phone}...")
                def send_old():
                    success, result = send_whatsapp(phone, msg_text)
                    if success:
                        asyncio.run_coroutine_threadsafe(
                            update.message.reply_text(f"✅ {result} на {phone}"),
                            main_loop
                        )
                    else:
                        asyncio.run_coroutine_threadsafe(
                            update.message.reply_text(f"❌ {result}"),
                            main_loop
                        )
                threading.Thread(target=send_old).start()
                return

    # 2. Для всех (и владельца тоже) извлекаем номера из текста
    numbers = extract_russian_phones(text)
    if numbers:
        saved = 0
        for num in numbers:
            if add_number(num, user_id):
                saved += 1
        reply = f"✅ Сохранено номеров: {saved}"
        if saved < len(numbers):
            reply += " (дубликаты пропущены)"
        await update.message.reply_text(reply)
    else:
        # Нет номеров — для не-владельца молчим, владельцу подскажем
        if user_id == OWNER_ID:
            await update.message.reply_text("❌ Не найдено российских номеров в сообщении.")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка PDF-файлов от любого пользователя."""
    user_id = update.effective_user.id
    document = update.message.document
    if not document.file_name.lower().endswith('.pdf'):
        return

    try:
        file = await document.get_file()
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)

        reader = PdfReader(buf)
        full_text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                full_text += page_text + "\n"

        numbers = extract_russian_phones(full_text)
        if numbers:
            saved = 0
            for num in numbers:
                if add_number(num, user_id):
                    saved += 1
            reply = f"✅ Из PDF сохранено номеров: {saved}"
            if saved < len(numbers):
                reply += " (дубликаты пропущены)"
            await update.message.reply_text(reply)
        else:
            await update.message.reply_text("❌ В PDF не найдено российских номеров.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка обработки PDF: {e}")

# ---------- команды владельца для рассылки ----------
async def cmd_unsent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    total, unsent = get_stats()
    await update.message.reply_text(f"📊 Всего номеров: {total}\n📬 Ещё не отправлено: {unsent}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    rows = get_all_numbers()
    if not rows:
        await update.message.reply_text("База номеров пуста.")
        return
    lines = ["phone,sent"]
    for phone, sent in rows:
        lines.append(f"{phone},{sent}")
    csv_data = "\n".join(lines)
    buf = io.BytesIO(csv_data.encode('utf-8'))
    buf.name = "numbers.csv"
    await update.message.reply_document(buf, caption=f"Всего номеров: {len(rows)}")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Укажите текст рассылки.\nПример: /broadcast Здравствуйте, это тест.")
        return
    message_text = " ".join(context.args)
    unsent = get_unsent_numbers()
    if not unsent:
        await update.message.reply_text("Все номера уже обработаны. /reset для сброса.")
        return

    await update.message.reply_text(f"🚀 Запущена рассылка на {len(unsent)} номеров...")
    success_count = 0
    fail_count = 0

    def broadcast_thread():
        nonlocal success_count, fail_count
        for phone in unsent:
            success, result = send_whatsapp(phone, message_text)
            if success:
                mark_sent(phone)
                success_count += 1
            else:
                fail_count += 1
            # Отправляем прогресс каждые 10 номеров
            if (success_count + fail_count) % 10 == 0:
                asyncio.run_coroutine_threadsafe(
                    update.message.reply_text(
                        f"⏳ Прогресс: отправлено {success_count}, ошибок {fail_count} из {len(unsent)}"
                    ),
                    main_loop
                )
        # Финальное сообщение
        asyncio.run_coroutine_threadsafe(
            update.message.reply_text(
                f"✅ Рассылка завершена.\nУспешно: {success_count}\nОшибок: {fail_count}\nВсего: {len(unsent)}"
            ),
            main_loop
        )
    threading.Thread(target=broadcast_thread).start()

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    reset_all_sent()
    await update.message.reply_text("♻️ Статусы отправки сброшены. Все номера снова доступны для рассылки.")

# ==================== MAIN ====================
def main():
    global bot_app, main_loop

    init_db()

    wa_thread = threading.Thread(target=wa_session_worker, daemon=True)
    wa_thread.start()

    bot_app = Application.builder().token(TOKEN).build()
    main_loop = asyncio.get_event_loop()

    # Регистрируем обработчики
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("wa", wa_command))
    bot_app.add_handler(CommandHandler("unsent", cmd_unsent))
    bot_app.add_handler(CommandHandler("list", cmd_list))
    bot_app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    bot_app.add_handler(CommandHandler("reset", cmd_reset))

    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    bot_app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    print("🤖 Бот запущен")
    bot_app.run_polling()

if __name__ == "__main__":
    main()

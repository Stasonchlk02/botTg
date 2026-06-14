import asyncio
import os
import re
import time
import threading
import sqlite3
import pdfplumber
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

# ==================== CONFIG ====================
TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = "/app/chrome_session"
DB_PATH = "bot_data.db"

driver = None
wa_ready = False
bot_app = None
main_loop = None

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contacts (
            phone TEXT PRIMARY KEY,
            status INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_phones(phones):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    added_count = 0
    for p in phones:
        try:
            cursor.execute("INSERT INTO contacts (phone, status) VALUES (?, 0)", (p,))
            added_count += 1
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    conn.close()
    return added_count

def get_pending_phones():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT phone FROM contacts WHERE status = 0")
    phones = [row[0] for row in cursor.fetchall()]
    conn.close()
    return phones

def mark_as_sent(phone):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE contacts SET status = 1 WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()

# ==================== HELPERS ====================
def normalize_ru_phone(text):
    """Находит российские номера и приводит их к формату 79XXXXXXXXX."""
    # Удаляем всё кроме цифр
    digits = re.sub(r'\D', '', text)
    
    # Ищем последовательности, похожие на мобильные РФ (11 цифр, начинается с 7 или 8 и потом 9)
    # Или 10 цифр, начинающихся с 9
    results = []
    
    # Регулярка для поиска потенциальных номеров в тексте
    potential = re.findall(r'(?:[78])?9\d{9}', digits)
    
    for p in potential:
        if len(p) == 10:
            results.append("7" + p)
        elif len(p) == 11:
            if p.startswith('8'):
                results.append("7" + p[1:])
            else:
                results.append(p)
    return list(set(results))

# ==================== BROWSER (БЕЗ ИЗМЕНЕНИЙ) ====================
def get_driver():
    options = Options()
    options.add_argument(f"--user-data-dir={SESSION_DIR}")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.binary_location = "/usr/bin/google-chrome"
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

def ensure_wa_ready():
    global wa_ready
    if wa_ready: return True
    try:
        wait = WebDriverWait(driver, 60)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='chat-list']")))
        wa_ready = True
        return True
    except:
        return False

# ==================== WHATSAPP SENDER ====================
def send_whatsapp(phone, message):
    if not driver or not wa_ready:
        if not ensure_wa_ready(): return False, "WhatsApp не загружен"

    try:
        url = f"https://web.whatsapp.com/send?phone={phone}"
        driver.get(url)
        wait = WebDriverWait(driver, 30)
        
        # Ждем поле ввода
        msg_box = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "div[contenteditable='true'][data-testid='conversation-compose-box-input']")
        ))
        
        msg_box.send_keys(message)
        time.sleep(1)
        msg_box.send_keys(Keys.RETURN)
        time.sleep(3) # Даем время на отправку
        
        mark_as_sent(phone)
        return True, "Успешно"
    except Exception as e:
        return False, str(e)

# ==================== TELEGRAM HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Отправь мне номер телефона или PDF файл с номерами.")

async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик для всех пользователей: сбор номеров."""
    text = update.message.text or update.message.caption or ""
    phones = normalize_ru_phone(text)
    
    if phones:
        added = save_phones(phones)
        await update.message.reply_text(f"✅ Найдено номеров: {len(phones)}\nНовых сохранено: {added}")
    elif update.message.document and update.message.document.mime_type == 'application/pdf':
        await handle_pdf(update, context)
    else:
        if update.effective_user.id == OWNER_ID:
            await update.message.reply_text("Номера не найдены. Для рассылки используйте /broadcast текст")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await context.bot.get_file(update.message.document.file_id)
    file_path = f"temp_{update.message.document.file_id}.pdf"
    await file.download_to_drive(file_path)
    
    try:
        content = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                content += (page.extract_text() or "")
        
        phones = normalize_ru_phone(content)
        if phones:
            added = save_phones(phones)
            await update.message.reply_text(f"📄 Из PDF извлечено: {len(phones)}\nНовых сохранено: {added}")
        else:
            await update.message.reply_text("❌ В PDF не найдено российских номеров.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при чтении PDF: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для владельца: /broadcast Текст сообщения"""
    if update.effective_user.id != OWNER_ID:
        return

    message_text = " ".join(context.args)
    if not message_text:
        await update.message.reply_text("Использование: /broadcast Привет, это рассылка!")
        return

    pending = get_pending_phones()
    if not pending:
        await update.message.reply_text("Нет новых номеров для рассылки.")
        return

    await update.message.reply_text(f"Начинаю рассылку на {len(pending)} номеров...")

    def run_broadcast():
        for phone in pending:
            success, res = send_whatsapp(phone, message_text)
            status_text = "✅" if success else "❌"
            asyncio.run_coroutine_threadsafe(
                bot_app.bot.send_message(OWNER_ID, f"{status_text} {phone}: {res}"),
                main_loop
            )
            time.sleep(5) # Задержка между сообщениями для безопасности аккаунта
        
        asyncio.run_coroutine_threadsafe(
            bot_app.bot.send_message(OWNER_ID, "🏁 Рассылка завершена!"),
            main_loop
        )

    threading.Thread(target=run_broadcast).start()

async def send_to_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /send 79123456789 Текст"""
    if update.effective_user.id != OWNER_ID: return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /send 79001112233 Текст")
        return
    
    phone = context.args[0]
    msg = " ".join(context.args[1:])
    
    await update.message.reply_text(f"Отправляю на {phone}...")
    
    def single_send():
        success, res = send_whatsapp(phone, msg)
        text = f"Результат для {phone}: {res}"
        asyncio.run_coroutine_threadsafe(update.message.reply_text(text), main_loop)

    threading.Thread(target=single_send).start()

# ==================== WORKER & MAIN ====================
def wa_session_worker():
    global driver
    while True:
        try:
            if not driver:
                driver = get_driver()
                driver.get("https://web.whatsapp.com")
            else:
                driver.current_url
        except:
            driver = None
        time.sleep(30)

def main():
    global bot_app, main_loop
    init_db()
    
    threading.Thread(target=wa_session_worker, daemon=True).start()

    bot_app = Application.builder().token(TOKEN).build()
    main_loop = asyncio.get_event_loop()

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
    bot_app.add_handler(CommandHandler("send", send_to_one))
    
    # Обрабатываем и текст, и документы (PDF)
    bot_app.add_handler(MessageHandler(filters.TEXT | filters.Document.ALL, handle_any_message))

    print("🤖 Бот запущен и готов собирать номера")
    bot_app.run_polling()

if __name__ == "__main__":
    main()

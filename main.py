import asyncio
import os
import re
import time
import threading
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException

from database import (
    init_db, add_phone, get_all_phones, get_unsent_phones,
    mark_as_sent, get_phone_by_number, get_stats, delete_phone
)
from phone_parser import extract_phones_from_text, is_single_phone
from pdf_parser import extract_phones_from_pdf


# ==================== CONFIG ====================
TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1636373767"))
SESSION_DIR = "/app/chrome_session"

driver = None
wa_ready = False
bot_app = None
main_loop = None

# Состояние для ожидания текста рассылки
# pending_broadcast[user_id] = {"type": "all"|"unsent"|"single", "phone": "..."|None}
pending_broadcast = {}


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
        txt = driver.execute_script(
            "return (arguments[0].innerText || arguments[0].textContent || '').trim();",
            el
        )
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
                By.XPATH, ".//button | .//div[@role='button']"
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
        modals = driver.find_elements(
            By.CSS_SELECTOR, "[role='dialog'], .modal, .popup"
        )
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
        wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "div[data-testid='chat-list'], div[data-testid='list']")
        ))
        time.sleep(3)

        close_dialog_if_exists()
        time.sleep(1)

        if is_modal_present():
            print("⚠️ Обнаружен popup, нажимаем ESC")
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            time.sleep(1)

        wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "div[data-testid='chat-list-search'], div[role='textbox']")
        ))
        close_dialog_if_exists()

        print("✅ WhatsApp Web полностью загружен")
        wa_ready = True
        return True

    except Exception as e:
        print(f"❌ Ошибка загрузки WhatsApp Web: {e}")
        send_to_telegram(f"❌ Ошибка загрузки WhatsApp Web: {e}")
        return False


# ==================== WHATSAPP SENDER ====================
def send_whatsapp(phone: str, message: str) -> tuple[bool, str]:
    """Отправка сообщения через WhatsApp Web."""
    if not driver or not wa_ready:
        if not ensure_wa_ready():
            return False, "WhatsApp Web не готов"

    try:
        phone_clean = re.sub(r'[^\d+]', '', phone.strip())
        if not phone_clean.startswith('+'):
            phone_clean = '+' + phone_clean

        encoded_phone = quote(phone_clean)
        url = f"https://web.whatsapp.com/send?phone={encoded_phone}"
        driver.get(url)
        time.sleep(5)

        close_dialog_if_exists()

        wait = WebDriverWait(driver, 60)
        msg_box = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR,
            "div[contenteditable='true'][data-testid='conversation-compose-box-input']"
        )))

        click_visible(msg_box)
        time.sleep(0.5)
        msg_box.send_keys(message)
        time.sleep(0.5)

        try:
            send_btn = driver.find_element(
                By.CSS_SELECTOR, "button[data-testid='send-button']"
            )
            if send_btn.is_enabled():
                click_visible(send_btn)
            else:
                raise Exception("Кнопка неактивна")
        except Exception:
            msg_box.send_keys(Keys.RETURN)

        time.sleep(2)

        try:
            wait.until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                "span[data-testid='msg-dblcheck'], span[data-testid='msg-check']"
            )))
            return True, "Сообщение отправлено"
        except TimeoutException:
            if "не зарегистрирован" in driver.page_source.lower():
                return False, "Номер не зарегистрирован в WhatsApp"
            return False, "Не удалось подтвердить отправку"

    except Exception as e:
        error_msg = str(e)
        print(f"Ошибка отправки на {phone}: {error_msg}")
        if "not registered" in error_msg.lower():
            return False, "Номер не зарегистрирован в WhatsApp"
        return False, f"Ошибка: {error_msg}"


# ==================== BROADCAST ====================
def run_broadcast(
    phones: list,        # список (id, phone) из БД
    message: str,
    progress_callback,   # callable(current, total, phone, success, reason)
    delay: float = 3.0   # задержка между отправками (сек)
):
    """
    Выполняет рассылку по списку номеров.
    Вызывается в отдельном потоке.
    """
    total = len(phones)
    ok_count = 0
    fail_count = 0

    for idx, (phone_id, phone) in enumerate(phones, 1):
        success, reason = send_whatsapp(phone, message)

        if success:
            mark_as_sent(phone_id)
            ok_count += 1
        else:
            fail_count += 1

        progress_callback(idx, total, phone, success, reason)

        # Пауза между отправками чтобы не получить бан
        if idx < total:
            time.sleep(delay)

    return ok_count, fail_count


# ==================== BACKGROUND WA ====================
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
                    elif "web.whatsapp.com" not in driver.current_url:
                        driver.get("https://web.whatsapp.com")
                        time.sleep(5)
                        ensure_wa_ready()
                except Exception:
                    print("⚠️ Браузер умер, перезапуск...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
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

    if user_id == OWNER_ID:
        await update.message.reply_text(
            "🤖 *WhatsApp Broadcast Bot*\n\n"
            "👑 Вы — администратор\n\n"
            "📋 *Команды:*\n"
            "/wa `номер текст` — отправить сообщение\n"
            "/broadcast — начать рассылку\n"
            "/phones — список всех номеров\n"
            "/stats — статистика\n"
            "/delete `номер` — удалить номер\n\n"
            "📥 Любой пользователь может отправить номер телефона или PDF",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "📱 Привет! Отправь мне номер телефона или PDF-файл с номерами,\n"
            "и я их сохраню."
        )


# ---------- Приём номеров от любых пользователей ----------

async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает текстовые сообщения от ЛЮБОГО пользователя.
    Для владельца — также обрабатывает команды рассылки.
    """
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Если владелец в режиме ожидания текста рассылки
    if user_id == OWNER_ID and user_id in pending_broadcast:
        await process_broadcast_message(update, context, text)
        return

    # Пробуем извлечь номера из текста
    phones = extract_phones_from_text(text)

    if not phones:
        # Если это владелец — может быть это команда /wa формата
        if user_id == OWNER_ID:
            await update.message.reply_text(
                "❓ Не найдено номеров телефонов.\n"
                "Отправьте номер (например: +7 999 123-45-67) или PDF-файл."
            )
        else:
            await update.message.reply_text(
                "❓ Не найдено российских номеров телефонов.\n"
                "Попробуйте отправить в формате: +7 999 123-45-67"
            )
        return

    # Сохраняем найденные номера
    added = []
    duplicates = []
    for phone in phones:
        if add_phone(phone, user_id):
            added.append(phone)
        else:
            duplicates.append(phone)

    # Формируем ответ
    response_parts = []
    if added:
        response_parts.append(f"✅ Сохранено: {', '.join(added)}")
    if duplicates:
        response_parts.append(f"ℹ️ Уже в базе: {', '.join(duplicates)}")

    await update.message.reply_text("\n".join(response_parts))


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает PDF-файлы от любого пользователя."""
    user_id = update.effective_user.id

    await update.message.reply_text("📄 Обрабатываю PDF, подождите...")

    try:
        # Скачиваем файл
        file = await update.message.document.get_file()
        pdf_bytes = await file.download_as_bytearray()

        # Извлекаем номера
        phones = extract_phones_from_pdf(bytes(pdf_bytes))

        if not phones:
            await update.message.reply_text(
                "❌ В PDF не найдено российских номеров телефонов."
            )
            return

        # Сохраняем
        added = []
        duplicates = []
        for phone in phones:
            if add_phone(phone, user_id):
                added.append(phone)
            else:
                duplicates.append(phone)

        response_parts = [f"📄 Обработан PDF, найдено номеров: {len(phones)}"]
        if added:
            response_parts.append(f"✅ Новых: {len(added)}")
            # Показываем первые 10
            preview = added[:10]
            response_parts.append("\n".join(preview))
            if len(added) > 10:
                response_parts.append(f"... и ещё {len(added) - 10}")
        if duplicates:
            response_parts.append(f"ℹ️ Дублей: {len(duplicates)}")

        await update.message.reply_text("\n".join(response_parts))

    except ImportError:
        await update.message.reply_text(
            "❌ Обработка PDF недоступна. Обратитесь к администратору."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка обработки PDF: {e}")


# ---------- Команды для владельца ----------

async def phones_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список всех сохранённых номеров."""
    if update.effective_user.id != OWNER_ID:
        return

    phones = get_all_phones()
    if not phones:
        await update.message.reply_text("📭 База номеров пуста.")
        return

    # Разбиваем на части по 50 номеров
    CHUNK = 50
    chunks = [phones[i:i+CHUNK] for i in range(0, len(phones), CHUNK)]

    for chunk_idx, chunk in enumerate(chunks):
        lines = [f"📋 *Номера ({chunk_idx*CHUNK+1}–{chunk_idx*CHUNK+len(chunk)}):*\n"]
        for row in chunk:
            phone_id, phone, added_by, added_at, sent, sent_at = row
            status = "✅" if sent else "⏳"
            date = added_at[:10] if added_at else "?"
            lines.append(f"{status} `{phone}` (добавлен {date})")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown"
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика базы номеров."""
    if update.effective_user.id != OWNER_ID:
        return

    stats = get_stats()
    await update.message.reply_text(
        f"📊 *Статистика:*\n"
        f"Всего номеров: {stats['total']}\n"
        f"Отправлено: {stats['sent']} ✅\n"
        f"Не отправлено: {stats['unsent']} ⏳",
        parse_mode="Markdown"
    )


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет номер из базы."""
    if update.effective_user.id != OWNER_ID:
        return

    if not context.args:
        await update.message.reply_text("❌ Пример: /delete +79991234567")
        return

    from phone_parser import normalize_phone
    raw = context.args[0]
    phone = normalize_phone(raw) or raw

    if delete_phone(phone):
        await update.message.reply_text(f"🗑️ Номер {phone} удалён.")
    else:
        await update.message.reply_text(f"❌ Номер {phone} не найден в базе.")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню рассылки."""
    if update.effective_user.id != OWNER_ID:
        return

    stats = get_stats()
    unsent = stats['unsent']
    total = stats['total']

    keyboard = [
        [
            InlineKeyboardButton(
                f"📤 Новым ({unsent} шт.)",
                callback_data="broadcast:unsent"
            )
        ],
        [
            InlineKeyboardButton(
                f"📢 Всем ({total} шт.)",
                callback_data="broadcast:all"
            )
        ],
        [
            InlineKeyboardButton(
                "📱 Конкретному номеру",
                callback_data="broadcast:single"
            )
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📣 *Рассылка WhatsApp*\n\n"
        f"Всего номеров: {total}\n"
        f"Ещё не получили: {unsent}\n\n"
        f"Выберите тип рассылки:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие кнопок рассылки."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != OWNER_ID:
        await query.answer("⛔ Доступ запрещён")
        return

    await query.answer()
    data = query.data  # "broadcast:unsent" / "broadcast:all" / "broadcast:single"
    action = data.split(":")[1]

    if action == "single":
        pending_broadcast[user_id] = {"type": "single", "phone": None, "step": "phone"}
        await query.message.reply_text(
            "📱 Введите номер телефона, которому хотите отправить рассылку:"
        )
    elif action in ("unsent", "all"):
        phones = get_unsent_phones() if action == "unsent" else [
            (row[0], row[1]) for row in get_all_phones()
        ]
        if not phones:
            await query.message.reply_text("📭 Нет номеров для рассылки.")
            return

        pending_broadcast[user_id] = {
            "type": action,
            "phone": None,
            "phones": phones,
            "step": "message"
        }
        await query.message.reply_text(
            f"✏️ Введите текст сообщения для рассылки\n"
            f"(будет отправлено на {len(phones)} номеров):"
        )


async def process_broadcast_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str
):
    """Обрабатывает ввод текста/номера для рассылки."""
    user_id = update.effective_user.id
    state = pending_broadcast.get(user_id)

    if not state:
        return

    # Шаг 1: ввод номера для single-рассылки
    if state["type"] == "single" and state["step"] == "phone":
        from phone_parser import normalize_phone
        phone = normalize_phone(text)
        if not phone:
            await update.message.reply_text(
                "❌ Некорректный номер. Попробуйте ещё раз (например: +79991234567):"
            )
            return

        # Проверяем, есть ли в базе
        row = get_phone_by_number(phone)
        if not row:
            await update.message.reply_text(
                f"⚠️ Номер {phone} не найден в базе.\n"
                f"Всё равно отправить? Введите текст сообщения\n"
                f"или /cancel для отмены."
            )
            # Сохраняем телефон, переходим к вводу сообщения
            state["phone"] = phone
            # Создаём временную запись
            state["phones"] = [(None, phone)]
            state["step"] = "message"
        else:
            state["phone"] = phone
            state["phones"] = [(row[0], phone)]
            state["step"] = "message"
            await update.message.reply_text(
                f"✅ Номер найден: {phone}\n"
                f"✏️ Введите текст сообщения:"
            )
        return

    # Шаг 2: ввод текста сообщения
    if state["step"] == "message":
        message_text = text
        phones = state["phones"]

        del pending_broadcast[user_id]  # Очищаем состояние

        count = len(phones)
        await update.message.reply_text(
            f"🚀 Запускаю рассылку на {count} номеров...\n"
            f"Это может занять некоторое время."
        )

        # Запускаем рассылку в отдельном потоке
        def do_broadcast():
            results = {"ok": 0, "fail": 0}
            fail_list = []

            def on_progress(current, total, phone, success, reason):
                results["ok" if success else "fail"] += 1
                if not success:
                    fail_list.append(f"{phone}: {reason}")
                # Отправляем прогресс каждые 5 сообщений
                if current % 5 == 0 or current == total:
                    send_to_telegram(
                        f"📤 Прогресс: {current}/{total}\n"
                        f"✅ {results['ok']} | ❌ {results['fail']}"
                    )

            run_broadcast(phones, message_text, on_progress)

            # Финальный отчёт
            report = [
                f"🏁 *Рассылка завершена*",
                f"Всего: {count}",
                f"✅ Успешно: {results['ok']}",
                f"❌ Ошибок: {results['fail']}",
            ]
            if fail_list:
                report.append("\n*Не доставлено:*")
                report.extend(fail_list[:10])  # Первые 10
                if len(fail_list) > 10:
                    report.append(f"...и ещё {len(fail_list) - 10}")

            send_to_telegram("\n".join(report))

        thread = threading.Thread(target=do_broadcast, daemon=True)
        thread.start()


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет текущую операцию."""
    user_id = update.effective_user.id
    if user_id in pending_broadcast:
        del pending_broadcast[user_id]
        await update.message.reply_text("❌ Операция отменена.")
    else:
        await update.message.reply_text("Нет активных операций.")


async def wa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прямая отправка сообщения на номер."""
    if update.effective_user.id != OWNER_ID:
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Формат: /wa 79123456789 Текст сообщения"
        )
        return

    from phone_parser import normalize_phone
    raw_phone = context.args[0]
    phone = normalize_phone(raw_phone) or raw_phone
    message = " ".join(context.args[1:])

    await update.message.reply_text(f"📤 Отправляю на {phone}...")

    def send_thread():
        success, result = send_whatsapp(phone, message)
        status = f"✅ {result}" if success else f"❌ {result}"
        asyncio.run_coroutine_threadsafe(
            update.message.reply_text(f"{status}\nНомер: {phone}"),
            main_loop
        )

    threading.Thread(target=send_thread, daemon=True).start()


# ==================== MAIN ====================
def main():
    global bot_app, main_loop

    # Инициализируем базу данных
    init_db()
    print("✅ База данных инициализирована")

    # Запускаем фоновый поток WhatsApp
    wa_thread = threading.Thread(target=wa_session_worker, daemon=True)
    wa_thread.start()

    # Создаём приложение Telegram
    bot_app = Application.builder().token(TOKEN).build()
    main_loop = asyncio.get_event_loop()

    # ---- Команды ----
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("wa", wa_command))
    bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
    bot_app.add_handler(CommandHandler("phones", phones_command))
    bot_app.add_handler(CommandHandler("stats", stats_command))
    bot_app.add_handler(CommandHandler("delete", delete_command))
    bot_app.add_handler(CommandHandler("cancel", cancel_command))

    # ---- Inline кнопки ----
    bot_app.add_handler(CallbackQueryHandler(broadcast_callback, pattern="^broadcast:"))

    # ---- Сообщения (для всех пользователей) ----
    # PDF от любого пользователя
    bot_app.add_handler(MessageHandler(
        filters.Document.MimeType("application/pdf"),
        handle_pdf
    ))
    # Текст от любого пользователя
    bot_app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_phone_input
    ))

    print("🤖 Бот запущен")
    bot_app.run_polling()


if __name__ == "__main__":
    main()

import sqlite3
import os
from datetime import datetime

DB_PATH = "/app/data/phones.db"

def init_db():
    """Инициализация базы данных."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS phones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE NOT NULL,
            added_by INTEGER,
            added_at TEXT,
            sent INTEGER DEFAULT 0,
            sent_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

def add_phone(phone: str, user_id: int) -> bool:
    """
    Добавляет номер в базу.
    Возвращает True если добавлен, False если уже существует.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO phones (phone, added_by, added_at) VALUES (?, ?, ?)",
            (phone, user_id, datetime.now().isoformat())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # Уже есть в базе
    finally:
        conn.close()

def get_all_phones() -> list:
    """Возвращает все номера."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, phone, added_by, added_at, sent, sent_at FROM phones ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows

def get_unsent_phones() -> list:
    """Возвращает номера, которым ещё не делали рассылку."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, phone FROM phones WHERE sent = 0 ORDER BY id"
    )
    rows = c.fetchall()
    conn.close()
    return rows

def mark_as_sent(phone_id: int):
    """Помечает номер как отправленный."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE phones SET sent = 1, sent_at = ? WHERE id = ?",
        (datetime.now().isoformat(), phone_id)
    )
    conn.commit()
    conn.close()

def get_phone_by_number(phone: str):
    """Ищет номер в базе."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, phone, sent FROM phones WHERE phone = ?", (phone,))
    row = c.fetchone()
    conn.close()
    return row

def get_stats() -> dict:
    """Статистика базы."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM phones")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM phones WHERE sent = 1")
    sent = c.fetchone()[0]
    conn.close()
    return {"total": total, "sent": sent, "unsent": total - sent}

def delete_phone(phone: str) -> bool:
    """Удаляет номер из базы."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM phones WHERE phone = ?", (phone,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

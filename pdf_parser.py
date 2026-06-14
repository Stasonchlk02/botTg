import io
import re

try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

from phone_parser import extract_phones_from_text


def extract_phones_from_pdf(pdf_bytes: bytes) -> list[str]:
    """
    Извлекает все российские номера телефонов из PDF-файла.
    
    Args:
        pdf_bytes: содержимое PDF-файла в байтах
        
    Returns:
        Список нормализованных номеров телефонов
    """
    if not PDF_AVAILABLE:
        raise ImportError("pdfplumber не установлен. Добавьте его в requirements.txt")

    all_text = []

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                try:
                    text = page.extract_text()
                    if text:
                        all_text.append(text)
                        print(f"[PDF] Страница {page_num}: извлечено {len(text)} символов")
                except Exception as e:
                    print(f"[PDF] Ошибка на странице {page_num}: {e}")
    except Exception as e:
        raise Exception(f"Не удалось открыть PDF: {e}")

    full_text = "\n".join(all_text)
    phones = extract_phones_from_text(full_text)
    print(f"[PDF] Найдено номеров: {len(phones)}")
    return phones

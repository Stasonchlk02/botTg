import re

# Все возможные российские форматы
RUSSIAN_PHONE_PATTERNS = [
    # +7 (999) 123-45-67
    r'\+7[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}',
    # 8 (999) 123-45-67
    r'8[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}',
    # 79991234567 или 89991234567
    r'[78]\d{10}',
    # +79991234567
    r'\+7\d{10}',
]

COMBINED_PATTERN = re.compile(
    r'(?:'
    r'\+7[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'
    r'|8[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'
    r'|\+7\d{10}'
    r'|[78]\d{10}'
    r')',
    re.IGNORECASE
)


def normalize_phone(raw: str) -> str | None:
    """
    Нормализует российский номер телефона к формату +7XXXXXXXXXX.
    Возвращает None если номер невалидный.
    """
    # Убираем всё кроме цифр и +
    digits = re.sub(r'[^\d+]', '', raw.strip())

    # Убираем ведущий +
    if digits.startswith('+'):
        digits = digits[1:]

    # Должно остаться 11 цифр
    if len(digits) != 11:
        return None

    # Российские номера начинаются с 7 или 8
    if digits[0] == '8':
        digits = '7' + digits[1:]

    if digits[0] != '7':
        return None

    # Проверяем что это реальный российский мобильный
    # Коды операторов: 9xx
    area_code = digits[1:4]
    if not (digits[1] == '9' or digits[1:3] in ['30', '31', '40', '42', '49']):
        # Разрешаем также городские +7 (например 495, 812 и т.д.)
        pass

    return f"+{digits}"


def extract_phones_from_text(text: str) -> list[str]:
    """
    Извлекает все российские номера из текста и нормализует их.
    Возвращает список уникальных нормализованных номеров.
    """
    found = COMBINED_PATTERN.findall(text)
    result = []
    seen = set()

    for raw in found:
        normalized = normalize_phone(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)

    return result


def is_single_phone(text: str) -> str | None:
    """
    Проверяет, является ли весь текст одним номером телефона.
    Возвращает нормализованный номер или None.
    """
    text = text.strip()
    # Убираем всё лишнее — должны остаться только цифры, +, пробелы, скобки, дефисы
    cleaned = re.sub(r'[^\d+\s\-\(\)]', '', text).strip()
    if not cleaned:
        return None
    return normalize_phone(cleaned)

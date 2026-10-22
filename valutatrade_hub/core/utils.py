"""Вспомогательные функции: валидация, хеширование паролей, форматирование, разбор аргументов.

Модуль зависит только от ``exceptions`` и стандартной библиотеки, поэтому его
используют модели, валюты, бизнес-логика и CLI. Персистентность (пути к
файлам, чтение/запись JSON) живёт в ``infra/database.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .exceptions import CurrencyNotFoundError, ValidationError

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

#: Допустимый формат кода валюты: 2–5 заглавных букв/цифр, без пробелов.
CURRENCY_CODE_RE = re.compile(r"^[A-Z0-9]{2,5}$")

#: Валюты, суммы в которых показываем с 4 знаками после запятой.
CRYPTO_CURRENCIES = {"BTC", "ETH", "USDT", "TON", "SOL", "XRP", "DOGE", "BNB"}

#: Заглушка курсов Parser Service: сколько USD стоит 1 единица валюты.
#: Ключи должны покрывать весь реестр ``currencies._REGISTRY_DATA``.
STUB_USD_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.0786,
    "GBP": 1.27,
    "RUB": 0.01016,
    "JPY": 0.0067,
    "CNY": 0.1386,
    "KZT": 0.00205,
    "BTC": 59337.21,
    "ETH": 3720.0,
    "USDT": 1.0,
    "TON": 5.25,
}


# ---------------------------------------------------------------------------
# Работа со временем
# ---------------------------------------------------------------------------


def now() -> datetime:
    """Текущее локальное время."""
    return datetime.now()


def to_iso(dt: datetime) -> str:
    """Datetime → строка ISO 8601 без микросекунд (для хранения в JSON)."""
    return dt.replace(microsecond=0).isoformat()


def parse_dt(value: datetime | str | None) -> datetime | None:
    """Строка ISO 8601 → datetime; при ошибке возвращает None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def format_dt(dt: datetime) -> str:
    """Datetime → «ГГГГ-ММ-ДД ЧЧ:ММ:СС» для вывода пользователю."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Хеширование паролей (односторонний псевдо-хеш sha256(password + salt))
# ---------------------------------------------------------------------------


def generate_salt() -> str:
    """Сгенерировать уникальную соль для пользователя."""
    return secrets.token_hex(8)


def hash_password(password: str, salt: str) -> str:
    """Зашифровать пароль: sha256 от конкатенации пароля и соли."""
    return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()


def verify_password(password: str, salt: str, hashed_password: str) -> bool:
    """Сравнить хеш введённого пароля с сохранённым (устойчиво к timing-атакам)."""
    return hmac.compare_digest(hash_password(password, salt), hashed_password)


# ---------------------------------------------------------------------------
# Валидаторы
# ---------------------------------------------------------------------------


def validate_username(value: str) -> str:
    """Имя пользователя: непустая строка (после отбрасывания пробелов)."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Имя пользователя не может быть пустым")
    username = value.strip()
    if len(username) > 32:
        raise ValidationError("Имя пользователя не может быть длиннее 32 символов")
    return username


def validate_password(value: str) -> str:
    """Пароль: строка не короче 4 символов."""
    if not isinstance(value, str) or len(value) < 4:
        raise ValidationError("Пароль должен быть не короче 4 символов")
    return value


def validate_currency_code(value: str) -> str:
    """Код валюты: верхний регистр, 2–5 символов, без пробелов (USD, BTC...)."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Код валюты не может быть пустым")
    code = value.strip().upper()
    if not CURRENCY_CODE_RE.match(code):
        raise ValidationError(
            f"Некорректный код валюты: '{value}' (ожидается 2–5 заглавных символов, например USD или BTC)"
        )
    return code


def validate_non_empty_str(value: str, label: str) -> str:
    """Непустая строка после отбрасывания пробелов (для имён и описаний)."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} не может быть пустым")
    return value.strip()


def validate_amount(value: Any, name: str = "amount") -> float:
    """Сумма: положительное конечное число (строки тоже принимаются)."""
    message = f"'{name}' должен быть положительным числом"
    if isinstance(value, bool):
        raise ValidationError(message)
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValidationError(message) from None
    if not math.isfinite(amount) or amount <= 0:
        raise ValidationError(message)
    return amount


# ---------------------------------------------------------------------------
# Форматирование чисел
# ---------------------------------------------------------------------------


def format_amount(value: float, currency: str | None = None) -> str:
    """Сумма с разделителем тысяч: 2 знака для фиата, 4 — для криптовалют."""
    decimals = 4 if isinstance(currency, str) and currency.upper() in CRYPTO_CURRENCIES else 2
    return f"{float(value):,.{decimals}f}"


def format_rate(value: float) -> str:
    """Курс без лишних нулей: 59337.21, 1.0786, 0.01016, 0.00001685."""
    text = f"{float(value):.8f}".rstrip("0").rstrip(".")
    return text or "0"


def format_market_cap(value: float) -> str:
    """Капитализация в экспоненциальной форме: 1.12e12."""
    return f"{float(value):.2e}".replace("e+", "e")


# ---------------------------------------------------------------------------
# Конвертация курсов
# ---------------------------------------------------------------------------


def cross_rate(usd_rates: Mapping[str, float], from_currency: str, to_currency: str) -> float:
    """Кросс-курс from→to через USD: «сколько единиц to стоит 1 единица from».

    :param usd_rates: словарь «код валюты → сколько USD стоит 1 единица»;
    :raises CurrencyNotFoundError: один из кодов отсутствует в курсах;
    :raises ValidationError: некорректный курс в данных (нулевое значение).
    """
    if from_currency not in usd_rates:
        raise CurrencyNotFoundError(from_currency)
    if to_currency not in usd_rates:
        raise CurrencyNotFoundError(to_currency)
    divider = float(usd_rates[to_currency])
    if divider <= 0:
        raise ValidationError(f"Некорректный курс для '{to_currency}'")
    return float(usd_rates[from_currency]) / divider


# ---------------------------------------------------------------------------
# Разбор аргументов команд вида --flag value / --flag=value
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagSpec:
    """Описание одного именованного аргумента CLI-команды."""

    flag: str
    required: bool = False
    default: str | None = None


def parse_flags(tokens: list[str], spec: dict[str, FlagSpec]) -> dict[str, str]:
    """Разобрать аргументы ``--flag value`` / ``--flag=value`` по описанию.

    :param tokens: аргументы команды без имени самой команды;
    :param spec: словарь «имя параметра → FlagSpec»;
    :return: словарь «имя параметра → строковое значение».
    """
    values: dict[str, str] = {
        dest: flag.default for dest, flag in spec.items() if flag.default is not None
    }
    by_flag = {flag_spec.flag: dest for dest, flag_spec in spec.items()}
    seen: set[str] = set()

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            raise ValidationError(f"Неизвестный аргумент: {token}")
        if "=" in token:
            flag, _, value = token.partition("=")
        else:
            flag = token
            if i + 1 >= len(tokens):
                raise ValidationError(f"Ожидалось значение после {flag}")
            value = tokens[i + 1]
            i += 1
        dest = by_flag.get(flag)
        if dest is None:
            raise ValidationError(f"Неизвестный аргумент: {flag}")
        if dest in seen:
            raise ValidationError(f"Аргумент {flag} указан дважды")
        seen.add(dest)
        values[dest] = value
        i += 1

    missing = [
        flag_spec.flag
        for dest, flag_spec in spec.items()
        if flag_spec.required and dest not in values
    ]
    if missing:
        raise ValidationError(f"Отсутствует обязательный аргумент: {missing[0]}")
    return values

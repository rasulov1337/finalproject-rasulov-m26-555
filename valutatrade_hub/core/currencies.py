"""Иерархия валют: абстрактный ``Currency`` и наследники Fiat/Crypto.

Реестр валют закрытый: операции (сделки, курсы, кошельки) возможны только
с валютами из ``_REGISTRY_DATA``. Новая валюта добавляется одной записью
(и, при необходимости, курсом в ``utils.STUB_USD_RATES``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .exceptions import CurrencyNotFoundError, ValidationError
from .utils import format_market_cap, validate_currency_code, validate_non_empty_str


class Currency(ABC):
    """Базовый класс валюты с единым интерфейсом для фиата и крипты.

    Инварианты:
    - ``code`` — верхний регистр, 2–5 символов, без пробелов;
    - ``name`` — непустая строка.
    """

    def __init__(self, code: str, name: str) -> None:
        self.code = validate_currency_code(code)
        self.name = validate_non_empty_str(name, "Имя валюты")

    @abstractmethod
    def get_display_info(self) -> str:
        """Строковое представление для UI и логов."""


class FiatCurrency(Currency):
    """Фиатная валюта (доллар, евро, рубль...)."""

    def __init__(self, code: str, name: str, issuing_country: str) -> None:
        super().__init__(code, name)
        self.issuing_country = validate_non_empty_str(issuing_country, "Страна эмиссии")

    def get_display_info(self) -> str:
        return f"[FIAT] {self.code} — {self.name} (Issuing: {self.issuing_country})"


class CryptoCurrency(Currency):
    """Криптовалюта."""

    def __init__(self, code: str, name: str, algorithm: str, market_cap: float) -> None:
        super().__init__(code, name)
        self.algorithm = validate_non_empty_str(algorithm, "Алгоритм")
        self.market_cap = _validate_market_cap(market_cap)

    def get_display_info(self) -> str:
        return (
            f"[CRYPTO] {self.code} — {self.name} "
            f"(Algo: {self.algorithm}, MCAP: {format_market_cap(self.market_cap)})"
        )


def _validate_market_cap(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < 0:
        raise ValidationError("Капитализация должна быть неотрицательным числом")
    return float(value)


# ---------------------------------------------------------------------------
# Реестр валют
# ---------------------------------------------------------------------------

#: Описание всех поддерживаемых валют (kind: fiat | crypto).
_REGISTRY_DATA: dict[str, dict[str, Any]] = {
    "USD": {"kind": "fiat", "name": "US Dollar", "issuing_country": "United States"},
    "EUR": {"kind": "fiat", "name": "Euro", "issuing_country": "Eurozone"},
    "GBP": {"kind": "fiat", "name": "British Pound", "issuing_country": "United Kingdom"},
    "RUB": {"kind": "fiat", "name": "Russian Ruble", "issuing_country": "Russia"},
    "JPY": {"kind": "fiat", "name": "Japanese Yen", "issuing_country": "Japan"},
    "CNY": {"kind": "fiat", "name": "Chinese Yuan", "issuing_country": "China"},
    "KZT": {"kind": "fiat", "name": "Kazakhstani Tenge", "issuing_country": "Kazakhstan"},
    "BTC": {"kind": "crypto", "name": "Bitcoin", "algorithm": "SHA-256", "market_cap": 1.12e12},
    "ETH": {"kind": "crypto", "name": "Ethereum", "algorithm": "Ethash", "market_cap": 3.8e11},
    "USDT": {"kind": "crypto", "name": "Tether", "algorithm": "ERC-20", "market_cap": 1.1e11},
    "TON": {
        "kind": "crypto",
        "name": "Toncoin",
        "algorithm": "Proof-of-Stake",
        "market_cap": 1.5e10,
    },
    "SOL": {
        "kind": "crypto",
        "name": "Solana",
        "algorithm": "Proof-of-History",
        "market_cap": 6.0e10,
    },
}


def _build_registry() -> dict[str, Currency]:
    registry: dict[str, Currency] = {}
    for code, data in _REGISTRY_DATA.items():
        if data["kind"] == "fiat":
            registry[code] = FiatCurrency(code, data["name"], data["issuing_country"])
        else:
            registry[code] = CryptoCurrency(
                code, data["name"], data["algorithm"], data["market_cap"]
            )
    return registry


#: Готовые объекты валют (создаётся один раз при импорте модуля).
_REGISTRY: dict[str, Currency] = _build_registry()


def get_currency(code: str) -> Currency:
    """Вернуть объект валюты по коду (регистр не важен).

    :raises CurrencyNotFoundError: кода нет в реестре (в том числе
        некорректно отформатированный).
    """
    if not isinstance(code, str):
        raise CurrencyNotFoundError(str(code))
    normalized = code.strip().upper()
    try:
        validated = validate_currency_code(normalized)
    except ValidationError:
        raise CurrencyNotFoundError(code.strip()) from None
    currency = _REGISTRY.get(validated)
    if currency is None:
        raise CurrencyNotFoundError(validated)
    return currency


def list_currencies() -> list[Currency]:
    """Все валюты реестра: сначала фиат, затем крипта (по алфавиту внутри групп)."""
    return sorted(
        _REGISTRY.values(),
        key=lambda item: (not isinstance(item, FiatCurrency), item.code),
    )

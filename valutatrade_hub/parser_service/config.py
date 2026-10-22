"""Конфигурация Parser Service: все изменяемые параметры в одном месте.

Правила:

- API-ключ ExchangeRate-API загружается **только** из переменной окружения
  ``EXCHANGERATE_API_KEY`` (в коде и git не хранится);
- списки валют по умолчанию берутся из реестра ``core/currencies.py``,
  чтобы реестр и парсер не расходились;
- пути к файлам строятся от ``SettingsLoader().data_dir``, поэтому
  переопределение ``VALUTATRADE_DATA_DIR`` работает и для парсера.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..core import currencies
from ..infra.settings import SettingsLoader


def _default_fiat_currencies() -> tuple[str, ...]:
    """Фиатные коды реестра (кроме базовой USD)."""
    return tuple(
        sorted(
            item.code
            for item in currencies.list_currencies()
            if isinstance(item, currencies.FiatCurrency) and item.code != "USD"
        )
    )


def _default_crypto_currencies() -> tuple[str, ...]:
    """Крипто-коды реестра."""
    return tuple(
        sorted(
            item.code
            for item in currencies.list_currencies()
            if isinstance(item, currencies.CryptoCurrency)
        )
    )


def _default_crypto_id_map() -> dict[str, str]:
    """Соответствие «тикер → ID монеты в CoinGecko» для всего реестра."""
    base_map = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "USDT": "tether",
        "TON": "the-open-network",
        "SOL": "solana",
    }
    crypto_codes = _default_crypto_currencies()
    return {code: coin_id for code, coin_id in base_map.items() if code in crypto_codes}


@dataclass(frozen=True)
class ParserConfig:
    """Структурированные настройки Parser Service.

    Имена полей соответствуют разделу 4 ТЗ (UPPER_SNAKE_CASE).
    """

    # Ключ загружается из переменной окружения (в коде не хранится).
    EXCHANGERATE_API_KEY: str | None = field(
        default_factory=lambda: os.getenv("EXCHANGERATE_API_KEY")
    )

    # Эндпоинты внешних API.
    COINGECKO_URL: str = "https://api.coingecko.com/api/v3/simple/price"
    EXCHANGERATE_API_URL: str = "https://v6.exchangerate-api.com/v6"

    # Списки валют (по умолчанию — из реестра core/currencies.py).
    BASE_CURRENCY: str = "USD"
    FIAT_CURRENCIES: tuple[str, ...] = field(default_factory=_default_fiat_currencies)
    CRYPTO_CURRENCIES: tuple[str, ...] = field(default_factory=_default_crypto_currencies)
    CRYPTO_ID_MAP: dict[str, str] = field(default_factory=_default_crypto_id_map)

    # Пути к файлам данных (от каталога data/ из SettingsLoader).
    RATES_FILE_PATH: Path = field(default_factory=lambda: SettingsLoader().data_dir / "rates.json")
    HISTORY_FILE_PATH: Path = field(
        default_factory=lambda: SettingsLoader().data_dir / "exchange_rates.json"
    )

    # Сетевые параметры и планировщик.
    REQUEST_TIMEOUT: int = 10
    UPDATE_INTERVAL_SECONDS: int = 300

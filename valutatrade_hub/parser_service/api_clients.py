"""Клиенты внешних API: CoinGecko (криптовалюты) и ExchangeRate-API (фиат).

Каждый клиент изолирует детали работы со «своим» сервисом (URL, формат
ответа) и приводит данные к единому формату: ``dict[пара → RateEntry]``,
где пара имеет вид ``BTC_USD`` (сколько USD стоит 1 единица валюты).

Важно: ExchangeRate-API возвращает курсы вида «1 USD = 0.927 EUR», поэтому
клиент инвертирует их (1 / rate), чтобы в кеше хранилось ``EUR_USD``.

Все сетевые ошибки и некорректные ответы оборачиваются в ``ApiRequestError``
с понятной причиной (reason) на английском — как в примерах вывода ТЗ.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

import requests

from ..core.exceptions import ApiRequestError
from ..core.utils import now_utc, to_iso_utc
from .config import ParserConfig

_USER_AGENT = "valutatrade-hub/0.1 (study project)"


@dataclass(frozen=True)
class RateEntry:
    """Один измеренный курс пары валют — для кеша и журнала измерений."""

    pair: str  # например, "BTC_USD"
    rate: float
    timestamp: datetime  # UTC
    source: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        """Уникальный id записи журнала: <FROM>_<TO>_<ISO-UTC timestamp>."""
        return f"{self.pair}_{to_iso_utc(self.timestamp)}"


class BaseApiClient(ABC):
    """Единый интерфейс источника курсов."""

    name: str = "BaseApiClient"

    @abstractmethod
    def fetch_rates(self) -> dict[str, RateEntry]:
        """Вернуть словарь «пара (BTC_USD) → RateEntry»."""
        raise NotImplementedError


class CoinGeckoClient(BaseApiClient):
    """Клиент CoinGecko: курсы криптовалют к USD (публичный доступ без ключа)."""

    name = "CoinGecko"

    def __init__(self, config: ParserConfig | None = None) -> None:
        self._config = config if config is not None else ParserConfig()

    def fetch_rates(self) -> dict[str, RateEntry]:
        cfg = self._config
        id_to_code = {coin_id: code for code, coin_id in cfg.CRYPTO_ID_MAP.items()}
        tracked = [code for code in cfg.CRYPTO_CURRENCIES if code in cfg.CRYPTO_ID_MAP]
        if not tracked:
            return {}

        started = time.monotonic()
        try:
            response = requests.get(
                cfg.COINGECKO_URL,
                params={
                    "ids": ",".join(cfg.CRYPTO_ID_MAP[code] for code in tracked),
                    "vs_currencies": cfg.BASE_CURRENCY.lower(),
                },
                timeout=cfg.REQUEST_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
            )
        except requests.exceptions.RequestException as exc:
            raise ApiRequestError(f"Network error ({exc.__class__.__name__})") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            raise ApiRequestError(f"HTTP {response.status_code} {response.reason}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiRequestError("invalid response format") from exc
        if not isinstance(payload, Mapping):
            raise ApiRequestError("invalid response format")

        # CoinGecko simple/price не отдаёт время — фиксируем момент запроса.
        fetched_at = now_utc()
        base = cfg.BASE_CURRENCY.lower()
        entries: dict[str, RateEntry] = {}
        for coin_id, prices in payload.items():
            code = id_to_code.get(str(coin_id))
            if code is None or not isinstance(prices, Mapping):
                continue
            rate = prices.get(base)
            if not isinstance(rate, int | float) or rate <= 0:
                continue
            pair = f"{code}_{cfg.BASE_CURRENCY}"
            entries[pair] = RateEntry(
                pair=pair,
                rate=float(rate),
                timestamp=fetched_at,
                source=self.name,
                meta={
                    "raw_id": str(coin_id),
                    "request_ms": elapsed_ms,
                    "status_code": response.status_code,
                    "etag": response.headers.get("ETag", ""),
                },
            )
        if not entries:
            raise ApiRequestError("no rates in response")
        return entries


class ExchangeRateApiClient(BaseApiClient):
    """Клиент ExchangeRate-API: курсы фиатных валют (нужен API-ключ)."""

    name = "ExchangeRate-API"

    #: Сообщения об ошибках из поля "error-type" ответа API.
    _ERROR_REASONS: ClassVar[dict[str, str]] = {
        "unsupported-code": "invalid API key",
        "quota-reached": "request limit exceeded (429)",
        "malformed-request": "malformed request",
        "invalid-key": "invalid API key",
    }

    def __init__(self, config: ParserConfig | None = None) -> None:
        self._config = config if config is not None else ParserConfig()

    def fetch_rates(self) -> dict[str, RateEntry]:
        cfg = self._config
        key = cfg.EXCHANGERATE_API_KEY
        if not key:
            raise ApiRequestError("API key not set (EXCHANGERATE_API_KEY)")

        started = time.monotonic()
        try:
            response = requests.get(
                f"{cfg.EXCHANGERATE_API_URL}/{key}/latest/{cfg.BASE_CURRENCY}",
                timeout=cfg.REQUEST_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
            )
        except requests.exceptions.RequestException as exc:
            raise ApiRequestError(f"Network error ({exc.__class__.__name__})") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            reason = self._ERROR_REASONS.get(self._error_type(response), "")
            detail = f": {reason}" if reason else ""
            raise ApiRequestError(f"HTTP {response.status_code} {response.reason}{detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiRequestError("invalid response format") from exc
        if not isinstance(payload, Mapping) or payload.get("result") != "success":
            if isinstance(payload, Mapping):
                error_type = str(payload.get("error-type", "unknown"))
            else:
                error_type = "unknown"
            reason = self._ERROR_REASONS.get(error_type, f"API error ({error_type})")
            raise ApiRequestError(reason)

        rates = payload.get("rates")
        if not isinstance(rates, Mapping):
            raise ApiRequestError("invalid response format")

        updated_at = self._parse_updated_at(payload)
        entries: dict[str, RateEntry] = {}
        for code in cfg.FIAT_CURRENCIES:
            if code == cfg.BASE_CURRENCY:
                continue
            usd_to_code = rates.get(code)
            if not isinstance(usd_to_code, int | float) or usd_to_code <= 0:
                continue
            # API отвечает «1 USD = 0.927 EUR» — инвертируем, чтобы хранить EUR_USD.
            pair = f"{code}_{cfg.BASE_CURRENCY}"
            entries[pair] = RateEntry(
                pair=pair,
                rate=round(1.0 / float(usd_to_code), 8),
                timestamp=updated_at,
                source=self.name,
                meta={
                    "raw_id": code,
                    "request_ms": elapsed_ms,
                    "status_code": response.status_code,
                    "etag": response.headers.get("ETag", ""),
                },
            )
        if not entries:
            raise ApiRequestError("no requested currencies in response")
        return entries

    @staticmethod
    def _error_type(response: requests.Response) -> str:
        """Вытащить error-type из тела ошибки (если он там есть)."""
        try:
            payload = response.json()
        except ValueError:
            return ""
        return str(payload.get("error-type", "")) if isinstance(payload, Mapping) else ""

    @staticmethod
    def _parse_updated_at(payload: Mapping[str, Any]) -> datetime:
        """Время обновления из time_last_update_utc; при ошибке — момент запроса."""
        raw = payload.get("time_last_update_utc")
        try:
            return parsedate_to_datetime(str(raw))
        except (TypeError, ValueError, IndexError):
            return now_utc()

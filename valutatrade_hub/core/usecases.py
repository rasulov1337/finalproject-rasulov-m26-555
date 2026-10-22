"""Бизнес-логика приложения: сессия пользователя, портфель, сделки и курсы валют.

Ключевые изменения раздела 3:

- валидация валют через реестр ``currencies.get_currency()``;
- пользовательские исключения из ``core/exceptions.py``
  (``CurrencyNotFoundError``, ``ApiRequestError``, ``InsufficientFundsError``);
- хранилище и конфигурация — через синглтоны ``DatabaseManager``
  и ``SettingsLoader`` (безопасное «чтение → модификация → запись»);
- операции ``register``/``login``/``buy``/``sell`` логируются декоратором
  ``@log_action`` (``logs/actions.log``).

CLI (``valutatrade_hub/cli/interface.py``) не дублирует бизнес-логику —
он лишь разбирает аргументы и вызывает методы ``AppService``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..decorators import log_action
from ..infra.database import DatabaseManager
from ..infra.settings import SettingsLoader
from ..logging_config import setup_logging
from . import currencies
from .currencies import get_currency
from .exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    InsufficientFundsError,
    NotLoggedInError,
    UnknownBaseCurrencyError,
    UserNotFoundError,
    UsernameTakenError,
    ValidationError,
    WalletNotFoundError,
    WrongPasswordError,
)
from .models import Portfolio, User
from .utils import (
    STUB_USD_RATES,
    cross_rate,
    format_amount,
    format_dt,
    format_rate,
    now,
    parse_dt,
    to_iso,
    validate_amount,
    validate_currency_code,
    validate_password,
    validate_username,
)

# ---------------------------------------------------------------------------
# Хранилище (users.json / portfolios.json) — через синглтон DatabaseManager
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Единый контракт сервиса парсинга (Parser Service)
# ---------------------------------------------------------------------------


class ParserClient(Protocol):
    """Контракт источника курсов: свежие курсы всех валют к USD.

    Чтобы подключить реальный Parser Service, достаточно реализовать этот
    протокол (например, HTTP-клиентом) и передать его в ``RateService``.
    """

    source: str

    def fetch_rates(self) -> dict[str, float]:
        """Вернуть словарь «код валюты → сколько USD стоит 1 единица»."""
        ...


class StubParserClient:
    """Заглушка Parser Service: фиксированные курсы из ``utils.STUB_USD_RATES``."""

    source = "ParserService"

    def fetch_rates(self) -> dict[str, float]:
        return dict(STUB_USD_RATES)


# ---------------------------------------------------------------------------
# Сервис курсов: кеш data/rates.json + обращение к Parser Service
# ---------------------------------------------------------------------------

_PAIR_SUFFIX = "_USD"


@dataclass(frozen=True)
class RateInfo:
    """Результат получения курса пары валют."""

    from_currency: str
    to_currency: str
    rate: float
    updated_at: datetime
    source: str

    @property
    def reverse_rate(self) -> float:
        """Обратный курс to_currency→from_currency."""
        return 1.0 / self.rate if self.rate else 0.0


@dataclass
class _CacheSnapshot:
    """Снимок кеша курсов из rates.json."""

    rates: dict[str, float]
    timestamps: dict[str, datetime]
    source: str
    last_refresh: datetime | None


class RateService:
    """Получение курсов: сначала кеш ``data/rates.json``, затем Parser Service.

    TTL свежести кеша и пути к файлам берутся из ``SettingsLoader`` и
    ``DatabaseManager``. Курсы хранятся как «1 единица валюты → USD»;
    курс произвольной пары A→B считается кросс-курсом через USD.

    Ошибки: ``ApiRequestError`` — сбой Parser Service, ``CurrencyNotFoundError`` —
    код валюты отсутствует в полученных курсах.
    """

    def __init__(self, client: ParserClient | None = None, ttl: timedelta | None = None) -> None:
        self._client: ParserClient = client if client is not None else StubParserClient()
        if ttl is None:
            ttl = timedelta(seconds=SettingsLoader().rates_ttl_seconds)
        self._ttl = ttl

    # -- публичный API -----------------------------------------------------

    def get_usd_rates(self) -> dict[str, float]:
        """Свежие курсы всех валют к USD (из кеша или после обновления)."""
        return self._snapshot().rates

    def get_pair_rate(self, from_currency: str, to_currency: str) -> RateInfo:
        """Получить курс пары валют, при необходимости обновив кеш."""
        from_c = validate_currency_code(from_currency)
        to_c = validate_currency_code(to_currency)
        snapshot = self._snapshot()  # ApiRequestError при полном сбое
        rate = cross_rate(snapshot.rates, from_c, to_c)  # CurrencyNotFoundError
        stamps = [snapshot.timestamps.get(code) for code in (from_c, to_c)]
        stamps = [stamp for stamp in stamps if stamp is not None]
        updated_at = min(stamps) if stamps else now()
        return RateInfo(from_c, to_c, rate, updated_at, snapshot.source)

    def refresh(self, force: bool = False) -> str:
        """Обновить кеш курсов через Parser Service."""
        cached = self._load_cache()
        if not force and cached is not None and self._is_fresh(cached):
            return f"Курсы актуальны, обновление не требуется (источник: {cached.source})"
        rates = self._fetch_from_client()  # ApiRequestError при сбое
        self._save_cache(rates, self._client.source)
        return f"Курсы обновлены: {len(rates)} валют (источник: {self._client.source})"

    # -- внутренние методы ---------------------------------------------------

    def _snapshot(self) -> _CacheSnapshot:
        """Вернуть снимок курсов: свежий кеш → Parser Service → устаревший кеш."""
        cached = self._load_cache()
        if cached is not None and self._is_fresh(cached):
            return cached
        try:
            rates = self._fetch_from_client()
        except ApiRequestError:
            if cached is not None:
                return cached  # устаревший кеш лучше, чем ничего
            raise
        self._save_cache(rates, self._client.source)
        refreshed = self._load_cache()
        if refreshed is not None:
            return refreshed
        return _CacheSnapshot(rates, {code: now() for code in rates}, self._client.source, now())

    def _fetch_from_client(self) -> dict[str, float]:
        """Запросить курсы у Parser Service и проверить ответ."""
        try:
            raw = self._client.fetch_rates()
        except ApiRequestError:
            raise
        except Exception as exc:
            raise ApiRequestError(f"сервис парсинга недоступен ({exc})") from exc
        if not isinstance(raw, Mapping) or not raw:
            raise ApiRequestError("пустой ответ от Parser Service")
        rates: dict[str, float] = {"USD": 1.0}
        for code, value in raw.items():
            try:
                currency = validate_currency_code(code)
                rate = float(value)
            except (ValidationError, TypeError, ValueError) as exc:
                raise ApiRequestError(f"некорректные данные курса для '{code}'") from exc
            if rate <= 0:
                raise ApiRequestError(f"некорректный курс для {currency}")
            rates[currency] = rate
        return rates

    def _load_cache(self) -> _CacheSnapshot | None:
        """Прочитать кеш курсов из data/rates.json (через DatabaseManager)."""
        data = DatabaseManager().load("rates", {})
        if not isinstance(data, dict) or not data:
            return None
        rates: dict[str, float] = {}
        timestamps: dict[str, datetime] = {}
        for key, entry in data.items():
            if not (isinstance(key, str) and key.endswith(_PAIR_SUFFIX)):
                continue
            if not (isinstance(entry, dict) and "rate" in entry):
                continue
            code = key[: -len(_PAIR_SUFFIX)]
            if not code:
                continue
            try:
                rates[code] = float(entry["rate"])
            except (TypeError, ValueError):
                continue
            timestamps[code] = parse_dt(entry.get("updated_at")) or now()
        if not rates:
            return None
        return _CacheSnapshot(
            rates=rates,
            timestamps=timestamps,
            source=str(data.get("source", "rates.json")),
            last_refresh=parse_dt(data.get("last_refresh")),
        )

    def _is_fresh(self, snapshot: _CacheSnapshot) -> bool:
        """Кеш свежий, если last_refresh моложе TTL и курсы не пустые."""
        return (
            snapshot.last_refresh is not None
            and (now() - snapshot.last_refresh) < self._ttl
            and bool(snapshot.rates)
        )

    def _save_cache(self, rates: dict[str, float], source: str) -> None:
        """Записать курсы в data/rates.json в формате из ТЗ."""
        stamp = to_iso(now())
        payload: dict[str, Any] = {
            f"{code}{_PAIR_SUFFIX}": {"rate": rate, "updated_at": stamp}
            for code, rate in sorted(rates.items())
        }
        payload["source"] = source
        payload["last_refresh"] = stamp
        DatabaseManager().save("rates", payload)


# ---------------------------------------------------------------------------
# Сервис приложения: сессия, портфель, сделки
# ---------------------------------------------------------------------------


class AppService:
    """Бизнес-логика приложения поверх синглтонов и сервиса курсов.

    Синглтоны: ``SettingsLoader`` (конфигурация: TTL курсов, пути к данным
    и логам) и ``DatabaseManager`` (единственная точка доступа к JSON-хранилищу).
    """

    def __init__(self, rate_service: RateService | None = None) -> None:
        setup_logging()
        self._settings = SettingsLoader()
        self._db = DatabaseManager()
        self._rates = rate_service if rate_service is not None else RateService()
        self._current_user: User | None = None
        self._current_portfolio: Portfolio | None = None
        #: Контекст последней операции для декоратора @log_action
        #: (rate, before/after — состояние кошелька).
        self._last_action_context: dict[str, Any] = {}

    @property
    def current_user(self) -> User | None:
        """Текущий залогиненный пользователь (или None)."""
        return self._current_user

    @property
    def current_portfolio(self) -> Portfolio | None:
        """Портфель текущего пользователя (или None)."""
        return self._current_portfolio

    def _require_login(self) -> tuple[User, Portfolio]:
        if self._current_user is None or self._current_portfolio is None:
            raise NotLoggedInError("Сначала выполните login")
        return self._current_user, self._current_portfolio

    # ------------------------------------------------------------------
    # Хранилище: типизированные обёртки над DatabaseManager
    # ------------------------------------------------------------------

    def _load_users(self) -> list[User]:
        raw = self._db.load("users", [])
        return [User.from_dict(item) for item in raw if isinstance(item, dict)]

    def _update_users(self, mutate: Callable[[list[User]], list[User]]) -> None:
        """Безопасное обновление users.json: чтение → модификация → запись."""

        def raw_mutate(raw: Any) -> Any:
            users = [User.from_dict(item) for item in raw if isinstance(item, dict)]
            return [user.to_dict() for user in mutate(users)]

        self._db.update("users", raw_mutate)

    def _load_portfolios(self) -> list[Portfolio]:
        raw = self._db.load("portfolios", [])
        return [Portfolio.from_dict(item) for item in raw if isinstance(item, dict)]

    def _update_portfolios(self, mutate: Callable[[list[Portfolio]], list[Portfolio]]) -> None:
        """Безопасное обновление portfolios.json: чтение → модификация → запись."""

        def raw_mutate(raw: Any) -> Any:
            portfolios = [Portfolio.from_dict(item) for item in raw if isinstance(item, dict)]
            return [portfolio.to_dict() for portfolio in mutate(portfolios)]

        self._db.update("portfolios", raw_mutate)

    def _save_portfolio(self, portfolio: Portfolio) -> None:
        """Сохранить портфель пользователя (обновить существующий или дописать)."""

        def mutate(portfolios: list[Portfolio]) -> list[Portfolio]:
            exists = any(item.user_id == portfolio.user_id for item in portfolios)
            updated = [portfolio if item.user_id == portfolio.user_id else item for item in portfolios]
            if not exists:
                updated.append(portfolio)
            return updated

        self._update_portfolios(mutate)

    # ------------------------------------------------------------------
    # Пользователи и сессия
    # ------------------------------------------------------------------

    @log_action("REGISTER")
    def register(self, username: str, password: str) -> str:
        """Зарегистрировать пользователя и создать ему пустой портфель."""
        username = validate_username(username)
        users = self._load_users()
        if any(user.username == username for user in users):
            raise UsernameTakenError(f"Имя пользователя '{username}' уже занято")
        password = validate_password(password)
        user_id = max((user.user_id for user in users), default=0) + 1
        user = User.create(user_id, username, password)
        self._update_users(lambda items: [*items, user])
        self._update_portfolios(lambda items: [*items, Portfolio(user_id)])
        return (
            f"Пользователь '{username}' зарегистрирован (id={user_id}). "
            f"Войдите: login --username {username} --password ****"
        )

    @log_action("LOGIN")
    def login(self, username: str, password: str) -> str:
        """Войти в систему и зафиксировать текущую сессию."""
        username = validate_username(username)
        user = next((item for item in self._load_users() if item.username == username), None)
        if user is None:
            raise UserNotFoundError(f"Пользователь '{username}' не найден")
        if not user.verify_password(password):
            raise WrongPasswordError("Неверный пароль")
        self._current_user = user
        portfolios = self._load_portfolios()
        portfolio = next((item for item in portfolios if item.user_id == user.user_id), None)
        if portfolio is None:
            portfolio = Portfolio(user.user_id)
            self._update_portfolios(lambda items: [*items, portfolio])
        self._current_portfolio = portfolio
        return f"Вы вошли как '{username}'"

    def logout(self) -> str:
        """Выйти из системы."""
        if self._current_user is None:
            return "Вы не залогинены"
        self._current_user = None
        self._current_portfolio = None
        return "Вы вышли из системы"

    def whoami(self) -> None:
        """Показать информацию о текущем пользователе (User.get_user_info)."""
        user, _ = self._require_login()
        user.get_user_info()

    def change_password(self, new_password: str) -> str:
        """Изменить пароль текущего пользователя и сохранить его."""
        user, _ = self._require_login()
        user.change_password(new_password)
        self._update_users(
            lambda items: [user if item.user_id == user.user_id else item for item in items]
        )
        return "Пароль изменён"

    # ------------------------------------------------------------------
    # Портфель
    # ------------------------------------------------------------------

    def show_portfolio(self, base_currency: str = "USD") -> str:
        """Показать все кошельки и итоговую стоимость в базовой валюте."""
        user, portfolio = self._require_login()
        base = validate_currency_code(base_currency)
        usd_rates = self._rates.get_usd_rates()
        if base not in usd_rates:
            raise UnknownBaseCurrencyError(f"Неизвестная базовая валюта '{base}'")
        wallets = portfolio.wallets
        if not wallets:
            return (
                f"У пользователя '{user.username}' пока нет кошельков.\n"
                "Создайте кошелёк: add-currency --currency USD\n"
                "или совершите первую покупку: buy --currency BTC --amount 0.01"
            )
        base_rate = usd_rates[base]
        lines = [f"Портфель пользователя '{user.username}' (база: {base}):"]
        total = 0.0
        missing: list[str] = []
        for code in sorted(wallets, key=lambda item: (item != "USD", item)):
            wallet = wallets[code]
            rate = usd_rates.get(code)
            if rate is None:
                lines.append(
                    f"- {code}: {format_amount(wallet.balance, code)}  → курс недоступен"
                )
                missing.append(code)
                continue
            value = wallet.balance * rate / base_rate
            total += value
            lines.append(
                f"- {code}: {format_amount(wallet.balance, code)}  → "
                f"{format_amount(value, base)} {base}"
            )
        lines.append("-" * 33)
        lines.append(f"ИТОГО: {format_amount(total, base)} {base}")
        if missing:
            lines.append(f"Курс недоступен для: {', '.join(missing)} (в итоге не учтено)")
        return "\n".join(lines)

    def add_currency(self, currency: str) -> str:
        """Добавить новый пустой кошелёк в портфель пользователя."""
        user, portfolio = self._require_login()
        code = get_currency(currency).code
        wallet = portfolio.add_currency(code)
        self._save_portfolio(portfolio)
        return (
            f"Кошелёк '{code}' создан для пользователя '{user.username}'. "
            f"Баланс: {format_amount(wallet.balance, code)} {code}"
        )

    def deposit(self, currency: str, amount: float) -> str:
        """Пополнить кошелёк указанной валюты (демо-режим симулятора)."""
        user, portfolio = self._require_login()
        code = get_currency(currency).code
        amount = validate_amount(amount)
        existed = portfolio.get_wallet(code) is not None
        wallet = portfolio.get_or_create_wallet(code)
        wallet.deposit(amount)
        self._save_portfolio(portfolio)
        prefix = "" if existed else f"Создан кошелёк '{code}'. "
        return (
            f"{prefix}Кошелёк '{code}' пополнен на {format_amount(amount, code)}. "
            f"Баланс: {format_amount(wallet.balance, code)} {code}"
        )

    # ------------------------------------------------------------------
    # Сделки
    # ------------------------------------------------------------------

    @log_action("BUY", verbose=True)
    def buy(self, currency: str, amount: float) -> str:
        """Купить валюту: пополнить её кошелёк и списать стоимость с USD-кошелька.

        :raises CurrencyNotFoundError: валюта отсутствует в реестре;
        :raises ApiRequestError: не удалось получить курс.
        """
        user, portfolio = self._require_login()
        self._last_action_context = {}
        code = get_currency(currency).code
        amount = validate_amount(amount)
        rate_info = self._get_trade_rate(code)
        existed = portfolio.get_wallet(code) is not None
        wallet = portfolio.get_or_create_wallet(code)
        before = wallet.balance
        wallet.deposit(amount)
        cost = round(amount * rate_info.rate, 8)
        self._last_action_context = {
            "rate": format_rate(rate_info.rate),
            "before": format_amount(before, code),
            "after": format_amount(wallet.balance, code),
        }

        lines = [
            f"Покупка выполнена: {format_amount(amount, code)} {code} "
            f"по курсу {format_rate(rate_info.rate)} USD/{code}"
        ]
        if not existed:
            lines.append(f"Создан кошелёк '{code}'")
        changes = [
            f"- {code}: было {format_amount(before, code)} → "
            f"стало {format_amount(wallet.balance, code)}"
        ]

        # При покупке сумма списывается с USD-кошелька,
        # если он есть и на нём достаточно средств.
        paid = False
        if code != "USD":
            usd_wallet = portfolio.get_wallet("USD")
            if usd_wallet is not None and usd_wallet.balance + 1e-9 >= cost:
                usd_before = usd_wallet.balance
                usd_wallet.withdraw(cost)
                changes.append(
                    f"- USD: было {format_amount(usd_before)} → "
                    f"стало {format_amount(usd_wallet.balance)}"
                )
                paid = True

        lines.append("Изменения в портфеле:")
        lines.extend(changes)
        if paid:
            lines.append(f"Списано с USD-кошелька: {format_amount(cost)} USD")
        elif code == "USD":
            lines.append(
                f"Оценочная стоимость покупки: {format_amount(cost)} USD (базовая валюта)"
            )
        else:
            usd_wallet = portfolio.get_wallet("USD")
            note = "" if usd_wallet is None else " (на USD-кошельке недостаточно средств)"
            lines.append(f"Оценочная стоимость покупки: {format_amount(cost)} USD{note}")
        self._save_portfolio(portfolio)
        return "\n".join(lines)

    @log_action("SELL", verbose=True)
    def sell(self, currency: str, amount: float) -> str:
        """Продать валюту: списать её с кошелька и зачислить выручку в USD.

        :raises WalletNotFoundError: кошелька с такой валютой нет;
        :raises InsufficientFundsError: на кошельке недостаточно средств;
        :raises ApiRequestError: не удалось получить курс.
        """
        user, portfolio = self._require_login()
        self._last_action_context = {}
        code = get_currency(currency).code
        amount = validate_amount(amount)
        wallet = portfolio.get_wallet(code)
        if wallet is None:
            raise WalletNotFoundError(
                f"У вас нет кошелька '{code}'. Добавьте валюту: "
                "она создаётся автоматически при первой покупке."
            )
        if amount > wallet.balance + 1e-9:
            raise InsufficientFundsError(
                available=format_amount(wallet.balance, code),
                required=format_amount(amount, code),
                code=code,
            )
        rate_info = self._get_trade_rate(code)
        before = wallet.balance
        wallet.withdraw(amount)  # InsufficientFundsError как страховка
        revenue = round(amount * rate_info.rate, 8)
        self._last_action_context = {
            "rate": format_rate(rate_info.rate),
            "before": format_amount(before, code),
            "after": format_amount(wallet.balance, code),
        }

        # При продаже эквивалент начисляется на USD-кошелёк
        # (при необходимости кошелёк USD создаётся автоматически).
        usd_existed = portfolio.get_wallet("USD") is not None
        usd_wallet = portfolio.get_or_create_wallet("USD")
        usd_before = usd_wallet.balance
        usd_wallet.deposit(revenue)

        lines = [
            f"Продажа выполнена: {format_amount(amount, code)} {code} "
            f"по курсу {format_rate(rate_info.rate)} USD/{code}",
            "Изменения в портфеле:",
        ]
        if code == "USD":
            # Продажа базовой валюты: списание и зачисление взаимно сокращаются.
            lines.append(
                f"- USD: было {format_amount(before)} → стало {format_amount(wallet.balance)} "
                "(базовая валюта, итог не изменился)"
            )
        else:
            lines.append(
                f"- {code}: было {format_amount(before, code)} → "
                f"стало {format_amount(wallet.balance, code)}"
            )
            lines.append(
                f"- USD: было {format_amount(usd_before)} → "
                f"стало {format_amount(usd_wallet.balance)}"
            )
            if not usd_existed:
                lines.append("Создан USD-кошелёк (базовая валюта)")
        lines.append(f"Выручка зачислена на USD-кошелёк: {format_amount(revenue)} USD")
        self._save_portfolio(portfolio)
        return "\n".join(lines)

    def _get_trade_rate(self, code: str) -> Any:
        """Курс currency→USD для сделки; сбой отдаётся как ApiRequestError."""
        try:
            return self._rates.get_pair_rate(code, "USD")
        except (ApiRequestError, CurrencyNotFoundError):
            raise ApiRequestError(f"не удалось получить курс для {code}→USD") from None

    # ------------------------------------------------------------------
    # Курсы
    # ------------------------------------------------------------------

    def get_rate(self, from_currency: str, to_currency: str) -> str:
        """Получить текущий курс одной валюты к другой.

        :raises CurrencyNotFoundError: один из кодов отсутствует в реестре;
        :raises ApiRequestError: сбой при получении курсов.
        """
        from_c = get_currency(from_currency).code
        to_c = get_currency(to_currency).code
        rate_info = self._rates.get_pair_rate(from_c, to_c)
        return (
            f"Курс {from_c}→{to_c}: {format_rate(rate_info.rate)} "
            f"(обновлено: {format_dt(rate_info.updated_at)})\n"
            f"Обратный курс {to_c}→{from_c}: {format_rate(rate_info.reverse_rate)}"
        )

    def refresh_rates(self) -> str:
        """Принудительно обновить кеш курсов через Parser Service."""
        return self._rates.refresh(force=True)

    def list_currencies(self) -> str:
        """Список поддерживаемых валют (для команды currencies)."""
        lines = ["Поддерживаемые валюты:"]
        for currency in currencies.list_currencies():
            lines.append(f"  {currency.get_display_info()}")
        lines.append("Сделки и курсы доступны только для валют из этого списка.")
        return "\n".join(lines)

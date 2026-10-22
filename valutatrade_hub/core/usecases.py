"""Бизнес-логика приложения: сессия пользователя, портфель, сделки и курсы валют.

Архитектура раздела 4:

- Core Service **не ходит в сеть**: ``RateService`` только читает кеш
  ``data/rates.json``, который пишет Parser Service (команда ``update-rates``);
- пустой кеш → ``RatesNotAvailableError`` с предложением выполнить
  ``update-rates``; устаревший (TTL из ``SettingsLoader``) → курсы
  используются, но к выводу добавляется предупреждение;
- ``AppService.update_rates`` запускает Parser Service (однократно или
  по расписанию), ``AppService.show_rates`` строит таблицу курсов из кеша.

CLI (``valutatrade_hub/cli/interface.py``) не дублирует бизнес-логику —
он лишь разбирает аргументы и вызывает методы ``AppService``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from prettytable import PrettyTable

from ..decorators import log_action
from ..infra.database import DatabaseManager
from ..infra.settings import SettingsLoader
from ..logging_config import setup_logging
from ..parser_service.scheduler import run_forever
from ..parser_service.updater import RatesUpdater, UpdateResult
from . import currencies
from .currencies import get_currency
from .exceptions import (
    CurrencyNotFoundError,
    InsufficientFundsError,
    NotLoggedInError,
    RatesNotAvailableError,
    UnknownBaseCurrencyError,
    UsernameTakenError,
    UserNotFoundError,
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
    validate_positive_int,
    validate_username,
)

#: Предупреждение, которое Core добавляет к выводу при устаревшем кеше.
STALE_RATES_WARNING = "Внимание: кеш курсов устарел — выполните update-rates."

_PAIR_SUFFIX = "_USD"


# ---------------------------------------------------------------------------
# Сервис курсов: чтение кеша data/rates.json (пишет его Parser Service)
# ---------------------------------------------------------------------------


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
    sources: dict[str, str]
    last_refresh: datetime | None


class RateService:
    """Чтение кеша курсов из ``data/rates.json`` (обновляет его Parser Service).

    Поддерживает актуальный формат ``{"pairs": {...}, "last_refresh": ...}``
    и предыдущий плоский формат (пары на верхнем уровне) — при переходе
    на новый формат данные не теряются.

    Курсы хранятся как «1 единица валюты → USD»; курс произвольной пары
    A→B считается кросс-курсом: ``rates[A] / rates[B]``.
    """

    def __init__(self, ttl: timedelta | None = None) -> None:
        if ttl is None:
            ttl = timedelta(seconds=SettingsLoader().rates_ttl_seconds)
        self._ttl = ttl

    # -- публичный API -----------------------------------------------------

    def get_snapshot(self) -> _CacheSnapshot:
        """Снимок кеша; пустой кеш → ``RatesNotAvailableError``."""
        snapshot = self._load_cache()
        if snapshot is None or not snapshot.rates:
            raise RatesNotAvailableError(
                "Локальный кеш курсов пуст. Выполните 'update-rates', чтобы загрузить данные."
            )
        return snapshot

    def get_usd_rates(self) -> dict[str, float]:
        """Курсы всех валют к USD из кеша."""
        return self.get_snapshot().rates

    def get_pair_rate(self, from_currency: str, to_currency: str) -> RateInfo:
        """Курс пары валют из кеша.

        :raises RatesNotAvailableError: кеш пуст;
        :raises CurrencyNotFoundError: один из кодов отсутствует в кеше.
        """
        from_c = validate_currency_code(from_currency)
        to_c = validate_currency_code(to_currency)
        snapshot = self.get_snapshot()
        rate = cross_rate(snapshot.rates, from_c, to_c)
        stamps = [snapshot.timestamps.get(code) for code in (from_c, to_c)]
        stamps = [stamp for stamp in stamps if stamp is not None]
        updated_at = min(stamps) if stamps else now()
        source = snapshot.sources.get(from_c) or snapshot.sources.get(to_c) or "rates.json"
        return RateInfo(from_c, to_c, rate, updated_at, source)

    def is_cache_stale(self) -> bool:
        """Кеш устарел: ``last_refresh`` старше TTL (или кеша нет вовсе)."""
        snapshot = self._load_cache()
        return snapshot is None or self._is_stale(snapshot)

    # -- внутренние методы ---------------------------------------------------

    def _is_stale(self, snapshot: _CacheSnapshot) -> bool:
        return snapshot.last_refresh is None or (now() - snapshot.last_refresh) >= self._ttl

    def _load_cache(self) -> _CacheSnapshot | None:
        """Прочитать кеш из data/rates.json (через DatabaseManager)."""
        data = DatabaseManager().load("rates", {})
        if not isinstance(data, dict) or not data:
            return None
        pairs = data.get("pairs")
        raw_pairs = pairs if isinstance(pairs, dict) else data  # плоский формат
        rates: dict[str, float] = {}
        timestamps: dict[str, datetime] = {}
        sources: dict[str, str] = {}
        for key, entry in raw_pairs.items():
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
            sources[code] = str(entry.get("source", "rates.json"))
        if not rates:
            return None
        # Базовая валюта USD — опорная единица всех кросс-курсов.
        if "USD" not in rates:
            rates["USD"] = 1.0
            timestamps.setdefault("USD", parse_dt(data.get("last_refresh")) or now())
            sources.setdefault("USD", "base")
        return _CacheSnapshot(
            rates=rates,
            timestamps=timestamps,
            sources=sources,
            last_refresh=parse_dt(data.get("last_refresh")),
        )


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
        self._last_action_context: dict[str, object] = {}

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

    def _update_users(self, mutate) -> None:
        """Безопасное обновление users.json: чтение → модификация → запись."""

        def raw_mutate(raw):
            users = [User.from_dict(item) for item in raw if isinstance(item, dict)]
            return [user.to_dict() for user in mutate(users)]

        self._db.update("users", raw_mutate)

    def _load_portfolios(self) -> list[Portfolio]:
        raw = self._db.load("portfolios", [])
        return [Portfolio.from_dict(item) for item in raw if isinstance(item, dict)]

    def _update_portfolios(self, mutate) -> None:
        """Безопасное обновление portfolios.json: чтение → модификация → запись."""

        def raw_mutate(raw):
            portfolios = [Portfolio.from_dict(item) for item in raw if isinstance(item, dict)]
            return [portfolio.to_dict() for portfolio in mutate(portfolios)]

        self._db.update("portfolios", raw_mutate)

    def _save_portfolio(self, portfolio: Portfolio) -> None:
        """Сохранить портфель пользователя (обновить существующий или дописать)."""

        def mutate(portfolios: list[Portfolio]) -> list[Portfolio]:
            exists = any(item.user_id == portfolio.user_id for item in portfolios)
            updated = [
                portfolio if item.user_id == portfolio.user_id else item for item in portfolios
            ]
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
                lines.append(f"- {code}: {format_amount(wallet.balance, code)}  → курс недоступен")
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
        if self._rates.is_cache_stale():
            lines.append(STALE_RATES_WARNING)
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
        _, portfolio = self._require_login()
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
        :raises RatesNotAvailableError: кеш пуст или курс не найден в кеше.
        """
        _, portfolio = self._require_login()
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
            (
                f"Покупка выполнена: {format_amount(amount, code)} {code} "
                f"по курсу {format_rate(rate_info.rate)} USD/{code}"
            )
        ]
        if not existed:
            lines.append(f"Создан кошелёк '{code}'")
        changes = [
            (
                f"- {code}: было {format_amount(before, code)} → "
                f"стало {format_amount(wallet.balance, code)}"
            )
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
            lines.append(f"Оценочная стоимость покупки: {format_amount(cost)} USD (базовая валюта)")
        else:
            usd_wallet = portfolio.get_wallet("USD")
            note = "" if usd_wallet is None else " (на USD-кошельке недостаточно средств)"
            lines.append(f"Оценочная стоимость покупки: {format_amount(cost)} USD{note}")
        if self._rates.is_cache_stale():
            lines.append(STALE_RATES_WARNING)
        self._save_portfolio(portfolio)
        return "\n".join(lines)

    @log_action("SELL", verbose=True)
    def sell(self, currency: str, amount: float) -> str:
        """Продать валюту: списать её с кошелька и зачислить выручку в USD.

        :raises WalletNotFoundError: кошелька с такой валютой нет;
        :raises InsufficientFundsError: на кошельке недостаточно средств;
        :raises RatesNotAvailableError: кеш пуст или курс не найден в кеше.
        """
        _, portfolio = self._require_login()
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
            (
                f"Продажа выполнена: {format_amount(amount, code)} {code} "
                f"по курсу {format_rate(rate_info.rate)} USD/{code}"
            ),
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
        if self._rates.is_cache_stale():
            lines.append(STALE_RATES_WARNING)
        self._save_portfolio(portfolio)
        return "\n".join(lines)

    def _get_trade_rate(self, code: str) -> RateInfo:
        """Курс currency→USD для сделки из локального кеша."""
        try:
            return self._rates.get_pair_rate(code, "USD")
        except CurrencyNotFoundError:
            raise RatesNotAvailableError(f"Курс для '{code}' не найден в кеше.") from None

    # ------------------------------------------------------------------
    # Курсы
    # ------------------------------------------------------------------

    def get_rate(self, from_currency: str, to_currency: str) -> str:
        """Получить курс одной валюты к другой из локального кеша.

        :raises CurrencyNotFoundError: один из кодов отсутствует в реестре;
        :raises RatesNotAvailableError: кеш пуст или пара не найдена в кеше.
        """
        from_c = get_currency(from_currency).code
        to_c = get_currency(to_currency).code
        try:
            rate_info = self._rates.get_pair_rate(from_c, to_c)
        except CurrencyNotFoundError as exc:
            raise RatesNotAvailableError(
                f"Курс для '{exc.currency_code}' не найден в кеше."
            ) from None
        lines = [
            (
                f"Курс {from_c}→{to_c}: {format_rate(rate_info.rate)} "
                f"(обновлено: {format_dt(rate_info.updated_at)})"
            ),
            f"Обратный курс {to_c}→{from_c}: {format_rate(rate_info.reverse_rate)}",
        ]
        if self._rates.is_cache_stale():
            lines.append(STALE_RATES_WARNING)
        return "\n".join(lines)

    def list_currencies(self) -> str:
        """Список поддерживаемых валют (для команды currencies)."""
        lines = ["Поддерживаемые валюты:"]
        for currency in currencies.list_currencies():
            lines.append(f"  {currency.get_display_info()}")
        lines.append("Сделки и курсы доступны только для валют из этого списка.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Parser Service: обновление и просмотр курсов
    # ------------------------------------------------------------------

    def update_rates(self, source: str | None = None, interval: int | str | None = None) -> None:
        """Обновить курсы через Parser Service (однократно или по расписанию).

        ``interval`` задан → планировщик: обновлять каждые N секунд,
        выход по Ctrl+C. Результат каждого цикла печатается сразу.
        """
        source = self._validate_source(source)
        updater = RatesUpdater()
        if interval is not None:
            interval_seconds = validate_positive_int(interval, "interval")

            def on_update(result: UpdateResult) -> None:
                print(self.render_update_result(result))

            run_forever(updater, interval_seconds, source=source, on_update=on_update)
            print("Stopped by user.")
            return
        print(self.render_update_result(updater.run_update(source)))

    @staticmethod
    def render_update_result(result: UpdateResult) -> str:
        """Строки вывода команды update-rates (формат из ТЗ, раздел 4.6)."""
        lines = ["INFO: Starting rates update..."]
        for item in result.results:
            if item.ok:
                lines.append(
                    f"INFO: Fetching from {item.client_name}... OK ({item.rates_count} rates)"
                )
            else:
                lines.append(f"ERROR: Failed to fetch from {item.client_name}: {item.error}")
        if result.entries_count:
            lines.append(f"INFO: Writing {result.entries_count} rates to data/rates.json...")
        if result.all_ok:
            lines.append(
                f"Update successful. Total rates updated: {result.total_written}. "
                f"Last refresh: {result.last_refresh}"
            )
        elif result.entries_count:
            lines.append("Update completed with errors. Check logs/parser.log for details.")
        else:
            lines.append("Update failed. Check logs/parser.log for details.")
        return "\n".join(lines)

    @staticmethod
    def _validate_source(source: str | None) -> str | None:
        """Проверить значение --source: None, 'coingecko' или 'exchangerate'."""
        if source is None:
            return None
        normalized = str(source).strip().lower()
        if normalized not in {"coingecko", "exchangerate"}:
            raise ValidationError(
                f"'source' должен быть 'coingecko' или 'exchangerate' (получено: '{source}')"
            )
        return normalized

    def show_rates(
        self,
        currency: str | None = None,
        top: int | str | None = None,
        base_currency: str | None = None,
    ) -> str:
        """Таблица курсов из локального кеша (команда show-rates).

        Приоритет фильтров: ``--currency`` → ``--top`` → ``--base`` → все пары.
        """
        snapshot = self._rates.get_snapshot()
        updated_header = to_iso(snapshot.last_refresh) if snapshot.last_refresh else "unknown"
        table = PrettyTable(field_names=["Pair", "Rate", "Updated"])
        table.align["Pair"] = "l"
        rows = self._rates_rows(snapshot, currency=currency, top=top, base_currency=base_currency)
        for row in rows:
            table.add_row(row)
        lines = [f"Rates from cache (updated at {updated_header}):", table.get_string()]
        if self._rates.is_cache_stale():
            lines.append(STALE_RATES_WARNING)
        return "\n".join(lines)

    def _rates_rows(
        self,
        snapshot: _CacheSnapshot,
        currency: str | None,
        top: int | str | None,
        base_currency: str | None,
    ) -> list[tuple[str, str, str]]:
        """Строки таблицы show-rates с учётом фильтров."""
        if currency is not None:
            code = validate_currency_code(currency)
            if code not in snapshot.rates:
                raise RatesNotAvailableError(f"Курс для '{code}' не найден в кеше.")
            return [
                (f"{code}_USD", format_rate(snapshot.rates[code]), self._updated_at(snapshot, code))
            ]
        if top is not None:
            limit = validate_positive_int(top, "top")
            crypto_codes = {
                item.code
                for item in currencies.list_currencies()
                if isinstance(item, currencies.CryptoCurrency)
            }
            ranked = sorted(
                ((code, rate) for code, rate in snapshot.rates.items() if code in crypto_codes),
                key=lambda item: item[1],
                reverse=True,
            )
            return [
                (f"{code}_USD", format_rate(rate), self._updated_at(snapshot, code))
                for code, rate in ranked[:limit]
            ]
        if base_currency is not None:
            base = validate_currency_code(base_currency)
            if base not in snapshot.rates:
                raise RatesNotAvailableError(f"Курс для '{base}' не найден в кеше.")
            return [
                (
                    f"{code}_{base}",
                    format_rate(cross_rate(snapshot.rates, code, base)),
                    self._updated_at(snapshot, code),
                )
                for code in sorted(snapshot.rates)
                if code != base
            ]
        return [
            (f"{code}_USD", format_rate(rate), self._updated_at(snapshot, code))
            for code, rate in sorted(snapshot.rates.items())
            if code != "USD"
        ]

    @staticmethod
    def _updated_at(snapshot: _CacheSnapshot, code: str) -> str:
        """Время обновления курса валюты для таблицы show-rates."""
        stamp = snapshot.timestamps.get(code) or snapshot.last_refresh or now()
        return format_dt(stamp)


# Экспорт для обратной совместимости тестов и отладки.
__all__ = [
    "STALE_RATES_WARNING",
    "STUB_USD_RATES",
    "AppService",
    "RateInfo",
    "RateService",
]

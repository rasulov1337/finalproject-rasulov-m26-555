"""Бизнес-логика приложения: сессия пользователя, портфель, сделки и курсы валют.

CLI (``valutatrade_hub/cli/interface.py``) не дублирует бизнес-логику —
он лишь разбирает аргументы и вызывает методы ``AppService`` из этого модуля.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from .models import Portfolio, User
from .utils import (
    PORTFOLIOS_FILE,
    RATES_FILE,
    STUB_USD_RATES,
    USERS_FILE,
    ensure_data_files,
    format_amount,
    format_dt,
    format_rate,
    load_json,
    now,
    parse_dt,
    save_json,
    to_iso,
    validate_amount,
    validate_currency_code,
    validate_password,
    validate_username,
)

# ---------------------------------------------------------------------------
# Ошибки приложения (сообщения показываются пользователю как есть)
# ---------------------------------------------------------------------------


class AppError(Exception):
    """Базовый класс ошибок приложения с сообщением для пользователя."""


class UsernameTakenError(AppError):
    """Имя пользователя уже занято."""


class UserNotFoundError(AppError):
    """Пользователь с таким именем не найден."""


class WrongPasswordError(AppError):
    """Введён неверный пароль."""


class NotLoggedInError(AppError):
    """Команда доступна только залогиненному пользователю."""


class WalletNotFoundError(AppError):
    """У пользователя нет кошелька с такой валютой."""


class InsufficientFundsError(AppError):
    """На кошельке недостаточно средств."""


class RateNotAvailableError(AppError):
    """Курс валюты недоступен."""


class UnknownBaseCurrencyError(AppError):
    """Неизвестная базовая валюта конвертации."""


# ---------------------------------------------------------------------------
# Хранилище: users.json / portfolios.json
# ---------------------------------------------------------------------------


def load_users() -> list[User]:
    """Загрузить всех пользователей из users.json."""
    data = load_json(USERS_FILE, [])
    return [User.from_dict(item) for item in data if isinstance(item, dict)]


def save_users(users: list[User]) -> None:
    """Сохранить всех пользователей в users.json."""
    save_json(USERS_FILE, [user.to_dict() for user in users])


def load_portfolios() -> list[Portfolio]:
    """Загрузить все портфели из portfolios.json."""
    data = load_json(PORTFOLIOS_FILE, [])
    return [Portfolio.from_dict(item) for item in data if isinstance(item, dict)]


def save_portfolios(portfolios: list[Portfolio]) -> None:
    """Сохранить все портфели в portfolios.json."""
    save_json(PORTFOLIOS_FILE, [portfolio.to_dict() for portfolio in portfolios])


def upsert_portfolio(portfolio: Portfolio) -> None:
    """Сохранить портфель в portfolios.json (обновить существующий или дописать)."""
    portfolios = load_portfolios()
    for index, existing in enumerate(portfolios):
        if existing.user_id == portfolio.user_id:
            portfolios[index] = portfolio
            break
    else:
        portfolios.append(portfolio)
    save_portfolios(portfolios)


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

#: Курсы в кеше считаются свежими, пока моложе этого времени.
CACHE_TTL = timedelta(minutes=5)

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

    Курсы хранятся как «1 единица валюты → USD»; курс произвольной пары
    A→B считается кросс-курсом через USD: ``rates[A] / rates[B]``.
    """

    def __init__(self, client: ParserClient | None = None, ttl: timedelta = CACHE_TTL) -> None:
        self._client: ParserClient = client if client is not None else StubParserClient()
        self._ttl = ttl

    # -- публичный API -----------------------------------------------------

    def get_usd_rates(self) -> dict[str, float]:
        """Свежие курсы всех валют к USD (из кеша или после обновления)."""
        return self._snapshot().rates

    def get_pair_rate(self, from_currency: str, to_currency: str) -> RateInfo:
        """Получить курс пары валют, при необходимости обновив кеш."""
        from_c = validate_currency_code(from_currency)
        to_c = validate_currency_code(to_currency)
        snapshot = self._snapshot()
        if from_c not in snapshot.rates or to_c not in snapshot.rates or snapshot.rates[to_c] <= 0:
            raise RateNotAvailableError(
                f"Курс {from_c}→{to_c} недоступен. Повторите попытку позже."
            )
        rate = snapshot.rates[from_c] / snapshot.rates[to_c]
        stamps = [snapshot.timestamps.get(code) for code in (from_c, to_c)]
        stamps = [stamp for stamp in stamps if stamp is not None]
        updated_at = min(stamps) if stamps else now()
        return RateInfo(from_c, to_c, rate, updated_at, snapshot.source)

    def refresh(self, force: bool = False) -> str:
        """Обновить кеш курсов через Parser Service."""
        cached = self._load_cache()
        if not force and cached is not None and self._is_fresh(cached):
            return f"Курсы актуальны, обновление не требуется (источник: {cached.source})"
        try:
            rates = self._fetch_from_client()
        except Exception:
            raise RateNotAvailableError(
                "Сервис курсов недоступен. Повторите попытку позже."
            ) from None
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
        except Exception:
            if cached is not None:
                return cached  # устаревший кеш лучше, чем ничего
            raise RateNotAvailableError(
                "Сервис курсов недоступен. Повторите попытку позже."
            ) from None
        self._save_cache(rates, self._client.source)
        refreshed = self._load_cache()
        if refreshed is not None:
            return refreshed
        return _CacheSnapshot(rates, {code: now() for code in rates}, self._client.source, now())

    def _fetch_from_client(self) -> dict[str, float]:
        """Запросить курсы у Parser Service и проверить ответ."""
        raw = self._client.fetch_rates()
        if not isinstance(raw, Mapping) or not raw:
            raise ValueError("Parser Service вернул пустой ответ")
        rates: dict[str, float] = {"USD": 1.0}
        for code, value in raw.items():
            currency = validate_currency_code(code)
            rate = float(value)
            if rate <= 0:
                raise ValueError(f"Некорректный курс для {currency}")
            rates[currency] = rate
        return rates

    def _load_cache(self) -> _CacheSnapshot | None:
        """Прочитать кеш курсов из data/rates.json."""
        data = load_json(RATES_FILE, {})
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
        save_json(RATES_FILE, payload)


# ---------------------------------------------------------------------------
# Сервис приложения: сессия, портфель, сделки
# ---------------------------------------------------------------------------


class AppService:
    """Бизнес-логика приложения поверх хранилищ JSON и сервиса курсов."""

    def __init__(self, rate_service: RateService | None = None) -> None:
        ensure_data_files()
        self._rates = rate_service if rate_service is not None else RateService()
        self._current_user: User | None = None
        self._current_portfolio: Portfolio | None = None

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

    # -- пользователи и сессия -----------------------------------------------

    def register(self, username: str, password: str) -> str:
        """Зарегистрировать пользователя и создать ему пустой портфель."""
        username = validate_username(username)
        users = load_users()
        if any(user.username == username for user in users):
            raise UsernameTakenError(f"Имя пользователя '{username}' уже занято")
        password = validate_password(password)
        user_id = max((user.user_id for user in users), default=0) + 1
        user = User.create(user_id, username, password)
        save_users([*users, user])
        save_portfolios([*load_portfolios(), Portfolio(user_id)])
        return (
            f"Пользователь '{username}' зарегистрирован (id={user_id}). "
            f"Войдите: login --username {username} --password ****"
        )

    def login(self, username: str, password: str) -> str:
        """Войти в систему и зафиксировать текущую сессию."""
        username = validate_username(username)
        user = next((item for item in load_users() if item.username == username), None)
        if user is None:
            raise UserNotFoundError(f"Пользователь '{username}' не найден")
        if not user.verify_password(password):
            raise WrongPasswordError("Неверный пароль")
        self._current_user = user
        portfolios = load_portfolios()
        portfolio = next((item for item in portfolios if item.user_id == user.user_id), None)
        if portfolio is None:
            portfolio = Portfolio(user.user_id)
            portfolios.append(portfolio)
            save_portfolios(portfolios)
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
        users = load_users()
        save_users([user if item.user_id == user.user_id else item for item in users])
        return "Пароль изменён"

    # -- портфель -------------------------------------------------------------

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
        code = validate_currency_code(currency)
        wallet = portfolio.add_currency(code)
        upsert_portfolio(portfolio)
        return (
            f"Кошелёк '{code}' создан для пользователя '{user.username}'. "
            f"Баланс: {format_amount(wallet.balance, code)} {code}"
        )

    def deposit(self, currency: str, amount: float) -> str:
        """Пополнить кошелёк указанной валюты (демо-режим симулятора)."""
        user, portfolio = self._require_login()
        code = validate_currency_code(currency)
        amount = validate_amount(amount)
        existed = portfolio.get_wallet(code) is not None
        wallet = portfolio.get_or_create_wallet(code)
        wallet.deposit(amount)
        upsert_portfolio(portfolio)
        prefix = "" if existed else f"Создан кошелёк '{code}'. "
        return (
            f"{prefix}Кошелёк '{code}' пополнен на {format_amount(amount, code)}. "
            f"Баланс: {format_amount(wallet.balance, code)} {code}"
        )

    # -- сделки ----------------------------------------------------------------

    def buy(self, currency: str, amount: float) -> str:
        """Купить валюту: пополнить её кошелёк и списать стоимость с USD-кошелька."""
        user, portfolio = self._require_login()
        code = validate_currency_code(currency)
        amount = validate_amount(amount)
        try:
            rate_info = self._rates.get_pair_rate(code, "USD")
        except RateNotAvailableError:
            raise RateNotAvailableError(f"Не удалось получить курс для {code}→USD") from None
        existed = portfolio.get_wallet(code) is not None
        wallet = portfolio.get_or_create_wallet(code)
        before = wallet.balance
        wallet.deposit(amount)
        cost = round(amount * rate_info.rate, 8)

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
        upsert_portfolio(portfolio)
        return "\n".join(lines)

    def sell(self, currency: str, amount: float) -> str:
        """Продать валюту: списать её с кошелька и зачислить выручку в USD."""
        user, portfolio = self._require_login()
        code = validate_currency_code(currency)
        amount = validate_amount(amount)
        wallet = portfolio.get_wallet(code)
        if wallet is None:
            raise WalletNotFoundError(
                f"У вас нет кошелька '{code}'. Добавьте валюту: "
                "она создаётся автоматически при первой покупке."
            )
        if amount > wallet.balance + 1e-9:
            raise InsufficientFundsError(
                f"Недостаточно средств: доступно {format_amount(wallet.balance, code)} {code}, "
                f"требуется {format_amount(amount, code)} {code}"
            )
        try:
            rate_info = self._rates.get_pair_rate(code, "USD")
        except RateNotAvailableError:
            raise RateNotAvailableError(f"Не удалось получить курс для {code}→USD") from None

        before = wallet.balance
        wallet.withdraw(amount)
        revenue = round(amount * rate_info.rate, 8)

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
        upsert_portfolio(portfolio)
        return "\n".join(lines)

    # -- курсы -------------------------------------------------------------------

    def get_rate(self, from_currency: str, to_currency: str) -> str:
        """Получить текущий курс одной валюты к другой."""
        from_c = validate_currency_code(from_currency)
        to_c = validate_currency_code(to_currency)
        rate_info = self._rates.get_pair_rate(from_c, to_c)
        return (
            f"Курс {from_c}→{to_c}: {format_rate(rate_info.rate)} "
            f"(обновлено: {format_dt(rate_info.updated_at)})\n"
            f"Обратный курс {to_c}→{from_c}: {format_rate(rate_info.reverse_rate)}"
        )

    def refresh_rates(self) -> str:
        """Принудительно обновить кеш курсов через Parser Service."""
        return self._rates.refresh(force=True)

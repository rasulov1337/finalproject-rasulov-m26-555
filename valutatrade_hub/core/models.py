"""Доменные модели приложения: ``User``, ``Wallet`` и ``Portfolio``.

Все поля приватные/защищённые: обычные атрибуты — с одним подчёркиванием
(protected, соглашение Python), особо чувствительные данные пользователя
(хеш пароля и соль) — с двумя (private, name mangling). Доступ — только
через свойства с валидацией. Сериализация в JSON — ``to_dict``/``from_dict``.

Точки интеграции раздела 3:

- ``Wallet.currency`` — полиморфный объект валюты из реестра ``currencies``;
- ``Wallet.withdraw`` бросает ``InsufficientFundsError``;
- ``Portfolio.user`` ищет владельца через ``DatabaseManager``.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping

from ..infra.database import DatabaseManager
from .currencies import Currency, get_currency
from .exceptions import InsufficientFundsError, ValidationError
from .utils import (
    STUB_USD_RATES,
    format_amount,
    format_dt,
    generate_salt,
    hash_password,
    now,
    parse_dt,
    to_iso,
    validate_currency_code,
    validate_password,
    validate_username,
    verify_password,
)


def _positive_amount(amount: Any, label: str) -> float:
    """Проверить, что сумма — положительное число, и вернуть её как float."""
    if (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not math.isfinite(float(amount))
        or float(amount) <= 0
    ):
        raise ValidationError(f"{label} должна быть положительным числом")
    return float(amount)


class User:
    """Пользователь системы.

    Пароль хранится только в виде одностороннего псевдо-хеша
    ``sha256(password + salt)``; соль сохраняется рядом. Хеш и соль —
    приватные атрибуты с name mangling (``__``): доступ извне класса
    невозможен даже по соглашению.
    """

    def __init__(
        self,
        user_id: int,
        username: str,
        hashed_password: str,
        salt: str,
        registration_date: datetime | str | None = None,
    ) -> None:
        self._user_id = int(user_id)
        self.username = username  # валидация в сеттере
        if not isinstance(hashed_password, str) or not hashed_password:
            raise ValidationError("Хеш пароля не может быть пустым")
        self.__hashed_password = hashed_password
        if not isinstance(salt, str) or not salt:
            raise ValidationError("Соль не может быть пустой")
        self.__salt = salt
        self._registration_date = parse_dt(registration_date) or now()

    # ------------------------------------------------------------------
    # Создание и сериализация
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        user_id: int,
        username: str,
        password: str,
        registration_date: datetime | str | None = None,
    ) -> "User":
        """Создать нового пользователя: сгенерировать соль и захешировать пароль."""
        username = validate_username(username)
        password = validate_password(password)
        salt = generate_salt()
        return cls(
            user_id,
            username,
            hash_password(password, salt),
            salt,
            registration_date,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "User":
        """Восстановить пользователя из словаря (users.json)."""
        return cls(
            user_id=data["user_id"],
            username=data["username"],
            hashed_password=data["hashed_password"],
            salt=data["salt"],
            registration_date=data.get("registration_date"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Представление для сохранения в users.json (пароль — только хеш)."""
        return {
            "user_id": self._user_id,
            "username": self._username,
            "hashed_password": self.__hashed_password,
            "salt": self.__salt,
            "registration_date": to_iso(self._registration_date),
        }

    # ------------------------------------------------------------------
    # Свойства (геттеры/сеттеры)
    # ------------------------------------------------------------------

    @property
    def user_id(self) -> int:
        """Уникальный идентификатор пользователя (только чтение)."""
        return self._user_id

    @property
    def username(self) -> str:
        """Имя пользователя."""
        return self._username

    @username.setter
    def username(self, value: str) -> None:
        self._username = validate_username(value)

    @property
    def hashed_password(self) -> str:
        """Хеш пароля (только чтение; меняется через change_password)."""
        return self.__hashed_password

    @property
    def salt(self) -> str:
        """Соль пользователя (только чтение)."""
        return self.__salt

    @property
    def registration_date(self) -> datetime:
        """Дата регистрации (только чтение)."""
        return self._registration_date

    # ------------------------------------------------------------------
    # Методы
    # ------------------------------------------------------------------

    def get_user_info(self) -> str:
        """Вывести информацию о пользователе (без пароля) и вернуть её."""
        info = (
            f"Пользователь #{self._user_id}: {self._username}\n"
            f"Дата регистрации: {format_dt(self._registration_date)}"
        )
        print(info)
        return info

    def change_password(self, new_password: str) -> None:
        """Изменить пароль: новый пароль хешируется с новой солью."""
        new_password = validate_password(new_password)
        self.__salt = generate_salt()
        self.__hashed_password = hash_password(new_password, self.__salt)

    def verify_password(self, password: str) -> bool:
        """Проверить введённый пароль на совпадение."""
        if not isinstance(password, str):
            return False
        return verify_password(password, self.__salt, self.__hashed_password)

    def __repr__(self) -> str:
        return f"User(user_id={self._user_id}, username={self._username!r})"


class Wallet:
    """Кошелёк пользователя для одной конкретной валюты."""

    def __init__(self, currency_code: str, balance: float = 0.0) -> None:
        self._currency_code = validate_currency_code(currency_code)
        self.balance = balance  # валидация в сеттере

    # ------------------------------------------------------------------
    # Свойства
    # ------------------------------------------------------------------

    @property
    def currency_code(self) -> str:
        """Код валюты (только чтение, задаётся при создании)."""
        return self._currency_code

    @property
    def currency(self) -> Currency:
        """Объект валюты (FiatCurrency/CryptoCurrency) из реестра.

        Точка интеграции с иерархией ``currencies``: позволяет полиморфно
        получать описание валюты (``get_display_info``) для UI и логов.

        :raises CurrencyNotFoundError: код кошелька отсутствует в реестре.
        """
        return get_currency(self._currency_code)

    @property
    def balance(self) -> float:
        """Текущий баланс."""
        return self._balance

    @balance.setter
    def balance(self, value: float) -> None:
        """Сеттер баланса: запрещает отрицательные значения и не-числа."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("Баланс должен быть числом")
        balance = float(value)
        if not math.isfinite(balance):
            raise ValidationError("Баланс должен быть конечным числом")
        if balance < 0:
            raise ValidationError("Баланс не может быть отрицательным")
        self._balance = round(balance, 8)

    # ------------------------------------------------------------------
    # Создание и сериализация
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], currency_code: str | None = None) -> "Wallet":
        """Восстановить кошелёк из словаря (код валюты может быть ключом)."""
        code = data.get("currency_code") or currency_code
        return cls(code, data.get("balance", 0.0))

    def to_dict(self) -> dict[str, Any]:
        """Представление для сохранения в portfolios.json."""
        return {"currency_code": self._currency_code, "balance": self._balance}

    # ------------------------------------------------------------------
    # Методы
    # ------------------------------------------------------------------

    def deposit(self, amount: float) -> None:
        """Пополнить баланс на положительную сумму."""
        amount = _positive_amount(amount, "Сумма пополнения")
        self._balance = round(self._balance + amount, 8)

    def withdraw(self, amount: float) -> None:
        """Снять средства, если баланс позволяет.

        :raises InsufficientFundsError: сумма снятия превышает баланс.
        """
        amount = _positive_amount(amount, "Сумма списания")
        if amount > self._balance + 1e-9:
            raise InsufficientFundsError(
                available=format_amount(self._balance, self._currency_code),
                required=format_amount(amount, self._currency_code),
                code=self._currency_code,
            )
        self._balance = round(self._balance - amount, 8)
        if abs(self._balance) < 1e-12:
            self._balance = 0.0

    def get_balance_info(self) -> str:
        """Вывести информацию о текущем балансе и вернуть её."""
        info = f"{self._currency_code}: {format_amount(self._balance, self._currency_code)}"
        print(info)
        return info

    def __repr__(self) -> str:
        return f"Wallet({self._currency_code!r}, balance={self._balance})"


class Portfolio:
    """Портфель: все кошельки одного пользователя.

    При покупке валюты сумма списывается с USD-кошелька (если он есть и на нём
    достаточно средств), при продаже — эквивалент начисляется на USD-кошелёк.
    """

    def __init__(self, user_id: int, wallets: Mapping[str, Any] | None = None) -> None:
        self._user_id = int(user_id)
        self._wallets: dict[str, Wallet] = {}
        for code, value in (wallets or {}).items():
            if isinstance(value, Wallet):
                self._wallets[value.currency_code] = value
            else:
                wallet = Wallet.from_dict(value, currency_code=code)
                self._wallets[wallet.currency_code] = wallet

    # ------------------------------------------------------------------
    # Создание и сериализация
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Portfolio":
        """Восстановить портфель из словаря (portfolios.json)."""
        return cls(data["user_id"], data.get("wallets") or {})

    def to_dict(self) -> dict[str, Any]:
        """Представление для сохранения в portfolios.json."""
        return {
            "user_id": self._user_id,
            "wallets": {code: wallet.to_dict() for code, wallet in self._wallets.items()},
        }

    # ------------------------------------------------------------------
    # Свойства
    # ------------------------------------------------------------------

    @property
    def user_id(self) -> int:
        """Идентификатор владельца (только чтение)."""
        return self._user_id

    @property
    def user(self) -> User | None:
        """Объект пользователя-владельца (только чтение, без перезаписи).

        Поиск выполняется через синглтон ``DatabaseManager``.
        """
        raw = DatabaseManager().load("users", [])
        if not isinstance(raw, list):
            return None
        for item in raw:
            if isinstance(item, dict) and item.get("user_id") == self._user_id:
                return User.from_dict(item)
        return None

    @property
    def wallets(self) -> dict[str, Wallet]:
        """Копия словаря кошельков (изменения копии не влияют на портфель)."""
        return dict(self._wallets)

    # ------------------------------------------------------------------
    # Методы
    # ------------------------------------------------------------------

    def add_currency(self, currency_code: str) -> Wallet:
        """Добавить новый кошелёк в портфель (код валюты должен быть уникален)."""
        code = validate_currency_code(currency_code)
        if code in self._wallets:
            raise ValidationError(f"Кошелёк '{code}' уже существует")
        wallet = Wallet(code)
        self._wallets[code] = wallet
        return wallet

    def get_or_create_wallet(self, currency_code: str) -> Wallet:
        """Вернуть кошелёк по коду валюты, при отсутствии — создать пустой."""
        code = validate_currency_code(currency_code)
        if code not in self._wallets:
            self._wallets[code] = Wallet(code)
        return self._wallets[code]

    def get_wallet(self, currency_code: str) -> Wallet | None:
        """Вернуть кошелёк по коду валюты или None, если кошелька нет."""
        code = validate_currency_code(currency_code)
        return self._wallets.get(code)

    def get_total_value(
        self,
        base_currency: str = "USD",
        usd_rates: dict[str, float] | None = None,
    ) -> float:
        """Общая стоимость всех валют портфеля в указанной базовой валюте.

        :param base_currency: базовая валюта конвертации (по умолчанию USD);
        :param usd_rates: курсы «1 единица валюты → USD»; если не переданы,
            используется фиксированный словарь-заглушка ``exchange_rates``.
        """
        rates = dict(STUB_USD_RATES) if usd_rates is None else dict(usd_rates)
        base = validate_currency_code(base_currency)
        if base not in rates:
            raise ValidationError(f"Неизвестная базовая валюта '{base}'")
        total = 0.0
        for code, wallet in self._wallets.items():
            if code not in rates:
                raise ValidationError(f"Не удалось получить курс для {code}→{base}")
            total += wallet.balance * rates[code] / rates[base]
        return round(total, 8)

    def __repr__(self) -> str:
        return f"Portfolio(user_id={self._user_id}, wallets={sorted(self._wallets)})"

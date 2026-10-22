"""Пользовательские исключения приложения.

Все исключения наследуются от ``ValutaTradeError``: их сообщения
предназначены для показа пользователю без дополнительной обработки.
Модуль не зависит от других модулей пакета, поэтому его могут импортировать
``utils``, ``models``, ``currencies`` и ``usecases`` без циклических импортов.
"""

from __future__ import annotations


class ValutaTradeError(Exception):
    """Базовая ошибка приложения; ``str(exc)`` — сообщение для пользователя."""


class ValidationError(ValutaTradeError, ValueError):
    """Ошибка входных данных (валидация аргументов команд)."""


# ---------------------------------------------------------------------------
# Пользователи и сессия
# ---------------------------------------------------------------------------


class UsernameTakenError(ValutaTradeError):
    """Имя пользователя уже занято."""


class UserNotFoundError(ValutaTradeError):
    """Пользователь с таким именем не найден."""


class WrongPasswordError(ValutaTradeError):
    """Введён неверный пароль."""


class NotLoggedInError(ValutaTradeError):
    """Команда доступна только залогиненному пользователю."""


# ---------------------------------------------------------------------------
# Портфель и сделки
# ---------------------------------------------------------------------------


class WalletNotFoundError(ValutaTradeError):
    """У пользователя нет кошелька с такой валютой."""


class UnknownBaseCurrencyError(ValutaTradeError):
    """Неизвестная базовая валюта конвертации."""


class InsufficientFundsError(ValutaTradeError):
    """Недостаточно средств на кошельке.

    Формат сообщения: ``Недостаточно средств: доступно {available} {code},
    требуется {required} {code}`` (``available``/``required`` — уже
    отформатированные суммы). Выбрасывается в ``Wallet.withdraw()``
    и в ``usecases.AppService.sell()``.
    """

    def __init__(self, available: str, required: str, code: str) -> None:
        super().__init__(
            f"Недостаточно средств: доступно {available} {code}, требуется {required} {code}"
        )
        self.available = available
        self.required = required
        self.currency_code = code


# ---------------------------------------------------------------------------
# Валюты и внешние API
# ---------------------------------------------------------------------------


class CurrencyNotFoundError(ValutaTradeError):
    """Неизвестная валюта: кода нет в реестре ``currencies``.

    Формат сообщения: ``Неизвестная валюта '{code}'``. Выбрасывается
    в ``currencies.get_currency()`` и при валидации входа в
    ``buy``/``sell``/``get-rate``.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"Неизвестная валюта '{code}'")
        self.currency_code = code


class ApiRequestError(ValutaTradeError):
    """Сбой при обращении к внешнему API (Parser Service).

    Формат сообщения: ``Ошибка при обращении к внешнему API: {reason}``.
    Выбрасывается в слое получения курсов (``parser_service/api_clients.py``).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"Ошибка при обращении к внешнему API: {reason}")
        self.reason = reason


class RatesNotAvailableError(ValutaTradeError):
    """Курсы недоступны: локальный кеш пуст или пара не найдена в кеше.

    Сообщение формируется в месте поднятия, например:
    «Локальный кеш курсов пуст. Выполните 'update-rates', чтобы загрузить данные.»
    или «Курс для 'ABC' не найден в кеше.»
    """

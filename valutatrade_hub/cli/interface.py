"""Консольный интерфейс (CLI) ValutaTrade Hub.

Единственная точка входа для пользовательских команд. Здесь нет бизнес-логики:
CLI разбирает аргументы, вызывает методы ``AppService`` из
``valutatrade_hub.core.usecases`` и печатает результат.
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field

from ..core import currencies as currency_registry
from ..core.exceptions import ApiRequestError, CurrencyNotFoundError, ValutaTradeError
from ..core.usecases import AppService
from ..core.utils import FlagSpec, parse_flags

EXIT_COMMANDS = {"exit", "quit", "q", "выход"}
HELP_COMMANDS = {"help", "?", "помощь"}

BANNER = """
======================================================
   ValutaTrade Hub — платформа для отслеживания
   и симуляции торговли валютами
======================================================
Введите help — список команд, exit — выход.
"""


@dataclass(frozen=True)
class Command:
    """Описание одной CLI-команды: как вызывать и какой метод сервиса выполнять."""

    usage: str
    description: str
    handler: str
    flags: dict[str, FlagSpec] = field(default_factory=dict)


#: Реестр команд: имя → описание.
COMMANDS: dict[str, Command] = {
    "register": Command(
        usage="register --username <имя> --password <пароль>",
        description="Зарегистрировать нового пользователя",
        handler="register",
        flags={
            "username": FlagSpec("--username", required=True),
            "password": FlagSpec("--password", required=True),
        },
    ),
    "login": Command(
        usage="login --username <имя> --password <пароль>",
        description="Войти в систему",
        handler="login",
        flags={
            "username": FlagSpec("--username", required=True),
            "password": FlagSpec("--password", required=True),
        },
    ),
    "logout": Command(
        usage="logout",
        description="Выйти из системы",
        handler="logout",
    ),
    "whoami": Command(
        usage="whoami",
        description="Информация о текущем пользователе",
        handler="whoami",
    ),
    "change-password": Command(
        usage="change-password --new-password <пароль>",
        description="Изменить пароль текущего пользователя",
        handler="change_password",
        flags={"new_password": FlagSpec("--new-password", required=True)},
    ),
    "show-portfolio": Command(
        usage="show-portfolio [--base USD]",
        description="Показать кошельки и итоговую стоимость",
        handler="show_portfolio",
        flags={"base_currency": FlagSpec("--base", default="USD")},
    ),
    "buy": Command(
        usage="buy --currency <код> --amount <кол-во>",
        description="Купить валюту",
        handler="buy",
        flags={
            "currency": FlagSpec("--currency", required=True),
            "amount": FlagSpec("--amount", required=True),
        },
    ),
    "sell": Command(
        usage="sell --currency <код> --amount <кол-во>",
        description="Продать валюту",
        handler="sell",
        flags={
            "currency": FlagSpec("--currency", required=True),
            "amount": FlagSpec("--amount", required=True),
        },
    ),
    "get-rate": Command(
        usage="get-rate --from <код> --to <код>",
        description="Показать курс одной валюты к другой",
        handler="get_rate",
        flags={
            "from_currency": FlagSpec("--from", required=True),
            "to_currency": FlagSpec("--to", required=True),
        },
    ),
    "currencies": Command(
        usage="currencies",
        description="Список поддерживаемых валют (фиат и крипто)",
        handler="list_currencies",
    ),
    "add-currency": Command(
        usage="add-currency --currency <код>",
        description="Добавить пустой кошелёк в портфель",
        handler="add_currency",
        flags={"currency": FlagSpec("--currency", required=True)},
    ),
    "deposit": Command(
        usage="deposit --currency <код> --amount <кол-во>",
        description="Пополнить кошелёк (демо-режим)",
        handler="deposit",
        flags={
            "currency": FlagSpec("--currency", required=True),
            "amount": FlagSpec("--amount", required=True),
        },
    ),
    "refresh-rates": Command(
        usage="refresh-rates",
        description="Принудительно обновить курсы через Parser Service",
        handler="refresh_rates",
    ),
}


def build_help() -> str:
    """Собрать текст справки по всем командам."""
    rows = [(command.usage, command.description) for command in COMMANDS.values()]
    rows.append(("help", "Показать эту справку"))
    rows.append(("exit", "Выйти из приложения"))
    width = max(len(usage) for usage, _ in rows)
    lines = ["Доступные команды:"]
    lines.extend(f"  {usage:<{width}}  {description}" for usage, description in rows)
    return "\n".join(lines)


def execute(service: AppService, tokens: list[str]) -> bool:
    """Выполнить одну команду по уже разбитанным токенам.

    :return: False, если приложение должно завершиться (команда exit).
    """
    if not tokens:
        return True
    name = tokens[0].lower()
    if name in EXIT_COMMANDS:
        print("До встречи!")
        return False
    if name in HELP_COMMANDS:
        print(build_help())
        return True
    command = COMMANDS.get(name)
    if command is None:
        print(f"Неизвестная команда: {name}")
        print("Введите help для списка команд.")
        return True
    try:
        kwargs = parse_flags(tokens[1:], command.flags)
        result = getattr(service, command.handler)(**kwargs)
        if result is not None:
            print(result)
    except ValutaTradeError as exc:
        # Пользовательские исключения: базовое сообщение печатается как есть,
        # отдельные типы дополняются подсказкой.
        print(exc)
        if isinstance(exc, CurrencyNotFoundError):
            supported = ", ".join(item.code for item in currency_registry.list_currencies())
            print(f"Поддерживаемые коды: {supported}")
            print("Полный список: currencies; справка по командам: help")
        elif isinstance(exc, ApiRequestError):
            print("Повторите попытку позже или проверьте сеть.")
    return True


def main(argv: list[str] | None = None) -> None:
    """Точка входа: интерактивный REPL или одиночная команда из argv.

    Пример одиночного режима: ``python main.py get-rate --from USD --to BTC``.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    service = AppService()
    if args:
        execute(service, args)
        return
    print(BANNER)
    print(build_help())
    while True:
        prompt = f"{service.current_user.username}> " if service.current_user else "> "
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print("\nДо встречи!")
            return
        if not execute(service, shlex.split(line)):
            return

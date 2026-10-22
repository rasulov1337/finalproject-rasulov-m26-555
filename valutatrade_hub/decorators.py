"""Декоратор ``@log_action`` — трассировка доменных операций.

Применяется к методам ``AppService`` (``register``/``login``/``buy``/``sell``)
и пишет в лог уровня INFO запись вида:

.. code-block:: text

    BUY user='alice' currency='BTC' amount=0.0500 rate=59337.21 base='USD' before=0.0000 after=0.0500 result=OK

При исключении запись фиксируется с ``result=ERROR`` и полями
``error_type``/``error_message``, после чего исключение пробрасывается дальше —
декоратор ничего не глотает.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

from .logging_config import get_logger

F = TypeVar("F", bound=Callable[..., Any])


def log_action(action: str, verbose: bool = False) -> Callable[[F], F]:
    """Декоратор метода ``AppService``: логировать операцию и её результат.

    Контекст собирается из:

    - ``self.current_user`` (или kwarg ``username`` для register/login);
    - kwargs метода: ``currency``/``from_currency``, ``amount``, ``base_currency``;
    - ``self._last_action_context`` — словарь, который метод заполняет по ходу
      выполнения (``rate``, ``before``/``after`` — состояние кошелька).

    :param action: имя операции в логе (BUY/SELL/REGISTER/LOGIN);
    :param verbose: добавить состояние кошелька «было→стало» (before/after).
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            context = _collect_context(self, kwargs)
            try:
                result = func(self, *args, **kwargs)
            except Exception as exc:
                context["result"] = "ERROR"
                context["error_type"] = type(exc).__name__
                context["error_message"] = f"'{exc}'"
                get_logger().info("%s %s", action, _render(context))
                raise
            extra = dict(getattr(self, "_last_action_context", None) or {})
            if not verbose:
                extra.pop("before", None)
                extra.pop("after", None)
            context.update(extra)
            context["result"] = "OK"
            get_logger().info("%s %s", action, _render(context))
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def _collect_context(self: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Собрать известные поля операции до её выполнения."""
    context: dict[str, Any] = {}
    user = getattr(self, "current_user", None)
    if user is not None:
        context["user"] = f"'{user.username}'"
    elif "username" in kwargs:
        context["user"] = f"'{kwargs['username']}'"

    code = kwargs.get("currency") or kwargs.get("from_currency")
    if code:
        context["currency"] = f"'{str(code).upper()}'"
    amount = kwargs.get("amount")
    if amount is not None:
        try:
            context["amount"] = f"{float(amount):.4f}"
        except (TypeError, ValueError):
            context["amount"] = amount
    if "base_currency" in kwargs:
        context["base"] = f"'{kwargs['base_currency']}'"
    elif code:
        context["base"] = "'USD'"
    return context


def _render(context: dict[str, Any]) -> str:
    """Словарь контекста → строка «key=value key=value»."""
    return " ".join(f"{key}={value}" for key, value in context.items())

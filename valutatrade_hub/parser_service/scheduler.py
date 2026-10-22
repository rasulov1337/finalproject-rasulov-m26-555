"""Планировщик периодического обновления курсов.

Запускается командой ``update-rates --interval <сек>``: цикл
«обновил → подождал», выход по Ctrl+C (``KeyboardInterrupt``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .updater import RatesUpdater, UpdateResult


def run_forever(
    updater: RatesUpdater,
    interval_seconds: int,
    source: str | None = None,
    on_update: Callable[[UpdateResult], None] | None = None,
) -> None:
    """Бесконечно обновлять курсы каждые ``interval_seconds`` секунд.

    :param updater: экземпляр ``RatesUpdater``;
    :param interval_seconds: пауза между циклами (положительное число секунд);
    :param source: ограничить обновление одним источником (см. update-rates);
    :param on_update: колбэк, вызывается с ``UpdateResult`` после каждого цикла;
    :raises ValueError: неположительный интервал;
    :raises KeyboardInterrupt: Ctrl+C во время обновления (пробрасывается).
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds должен быть положительным")
    while True:
        result = updater.run_update(source)
        if on_update is not None:
            on_update(result)
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            return

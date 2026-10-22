"""Настройка логирования приложения.

Формат зафиксирован текстовым — одна строка на событие:

.. code-block:: text

    INFO 2025-10-09T12:05:22 BUY user='alice' currency='BTC' amount=0.0500 rate=59300.00 base='USD' result=OK

- файл: ``logs/actions.log`` (каталог настраивается ключом ``logs_dir``);
- ротация: ``RotatingFileHandler`` по размеру (``log_max_bytes``, по умолчанию
  1 МБ) с ``log_backup_count`` бэкапами (по умолчанию 3);
- уровень: ``log_level`` (по умолчанию INFO, для отладки — DEBUG);
- в stdout ничего не пишется, чтобы не засорять вывод CLI.

Настройка идемпотентна: повторный вызов ``setup_logging`` не добавляет
дублирующиеся обработчики.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .infra.settings import SettingsLoader

LOGGER_NAME = "valutatrade"
LOG_FILE_NAME = "actions.log"
LOG_FORMAT = "%(levelname)s %(asctime)s %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def get_logger() -> logging.Logger:
    """Логгер приложения (создаётся до setup_logging, но пишет после него)."""
    return logging.getLogger(LOGGER_NAME)


def setup_logging() -> logging.Logger:
    """Настроить логгер приложения (идемпотентно) и вернуть его."""
    settings = SettingsLoader()
    logger = get_logger()
    logger.setLevel(_resolve_level(settings.log_level))
    if logger.handlers:  # уже настроен — не дублируем обработчики
        return logger
    handler = _build_file_handler(settings.logs_dir, settings.log_max_bytes, settings.log_backup_count)
    if handler is not None:
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def _resolve_level(level: str) -> int:
    return getattr(logging, str(level).strip().upper(), logging.INFO)


def _build_file_handler(logs_dir: Path, max_bytes: int, backup_count: int) -> RotatingFileHandler | None:
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            logs_dir / LOG_FILE_NAME,
            maxBytes=max(1024, int(max_bytes)),
            backupCount=max(0, int(backup_count)),
            encoding="utf-8",
        )
    except OSError:
        # Логи не должны ломать приложение: без файла работаем в никуда.
        return None
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    return handler

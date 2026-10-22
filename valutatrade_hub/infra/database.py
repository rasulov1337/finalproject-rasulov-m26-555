"""Singleton ``DatabaseManager`` — единая точка доступа к JSON-хранилищу.

Владеет путями к файлам ``data/*.json`` (каталог берётся из ``SettingsLoader``),
читает и пишет их атомарно (сначала во временный файл, затем замена) и
предоставляет безопасную операцию «чтение → модификация → запись» (``update``):
если мутация бросила исключение, запись не выполняется и файл не портится.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.exceptions import ValidationError
from .settings import SettingsLoader

#: Допустимые имена коллекций (файлов хранилища).
COLLECTIONS = ("users", "portfolios", "rates")


class DatabaseManager:
    """Singleton-обёртка над JSON-хранилищем data/*.json.

    Работает с «сырыми» данными (списки/словари); типизированная конвертация
    в объекты ``User``/``Portfolio`` выполняется слоем бизнес-логики.
    """

    _instance: "DatabaseManager | None" = None

    def __new__(cls) -> "DatabaseManager":
        if cls._instance is None:
            instance = super().__new__(cls)
            data_dir = SettingsLoader().data_dir
            instance._paths = {
                "users": data_dir / "users.json",
                "portfolios": data_dir / "portfolios.json",
                "rates": data_dir / "rates.json",
            }
            cls._instance = instance
        return cls._instance

    def __init__(self) -> None:
        if not hasattr(self, "_paths"):
            # Защита на случай, если _instance сбросили в тестах.
            data_dir = SettingsLoader().data_dir
            self._paths = {
                "users": data_dir / "users.json",
                "portfolios": data_dir / "portfolios.json",
                "rates": data_dir / "rates.json",
            }
        self.ensure_data_files()

    # ------------------------------------------------------------------
    # Пути
    # ------------------------------------------------------------------

    def path(self, collection: str) -> Path:
        """Путь к файлу коллекции."""
        self._check_collection(collection)
        return self._paths[collection]

    # ------------------------------------------------------------------
    # Чтение / запись
    # ------------------------------------------------------------------

    def load(self, collection: str, default: Any = None) -> Any:
        """Прочитать коллекцию; если файла нет или он пуст — вернуть default."""
        self._check_collection(collection)
        path = self._paths[collection]
        fallback = default if default is not None else self._empty_default(collection)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return fallback
        if not text.strip():
            return fallback
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"Не удалось прочитать {path.name}: повреждённый JSON ({exc})"
            ) from exc

    def save(self, collection: str, data: Any) -> None:
        """Атомарно сохранить коллекцию (запись через временный файл)."""
        self._check_collection(collection)
        path = self._paths[collection]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(path)

    def update(self, collection: str, mutate: Callable[[Any], Any]) -> Any:
        """Безопасная операция «чтение → модификация → запись».

        ``mutate`` получает текущие данные и возвращает новые; если ``mutate``
        бросает исключение, сохранение не выполняется.
        """
        data = self.load(collection)
        result = mutate(data)
        self.save(collection, result if result is not None else data)
        return result if result is not None else data

    def ensure_data_files(self) -> None:
        """Создать каталог data/ и пустые JSON-файлы, если их ещё нет."""
        for collection in COLLECTIONS:
            path = self._paths[collection]
            if not path.exists():
                self.save(collection, self._empty_default(collection))

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_default(collection: str) -> Any:
        return {} if collection == "rates" else []

    @staticmethod
    def _check_collection(collection: str) -> None:
        if collection not in COLLECTIONS:
            raise ValidationError(
                f"Неизвестная коллекция хранилища: '{collection}' "
                f"(доступны: {', '.join(COLLECTIONS)})"
            )

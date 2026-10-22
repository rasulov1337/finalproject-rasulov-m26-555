"""Singleton ``SettingsLoader`` — единая точка конфигурации приложения.

Источник настроек — секция ``[tool.valutatrade]`` в ``pyproject.toml``
(читается стандартным ``tomllib``). Приоритет источников:
переменные окружения ``VALUTATRADE_*`` → pyproject.toml → значения по умолчанию.

Singleton реализован через ``__new__``: этот способ проще и читабельнее
метакласса, не требует дополнительного класса и гарантирует единый
экземпляр даже при импорте модуля из разных мест приложения.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

#: Встроенные значения по умолчанию (используются, если ключа нет в конфиге).
_DEFAULTS: dict[str, Any] = {
    "data_dir": "data",
    "logs_dir": "logs",
    "rates_ttl_seconds": 300,
    "default_base_currency": "USD",
    "log_level": "INFO",
    "log_max_bytes": 1_048_576,  # 1 МБ до ротации logs/actions.log
    "log_backup_count": 3,
}

_ENV_PREFIX = "VALUTATRADE_"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SettingsLoader:
    """Singleton: загрузка и кеширование конфигурации проекта.

    Ключи конфигурации (см. ``_DEFAULTS``):

    - ``data_dir`` — каталог JSON-хранилищ (users/portfolios/rates);
    - ``logs_dir`` — каталог логов;
    - ``rates_ttl_seconds`` — сколько секунд кеш курсов считается свежим;
    - ``default_base_currency`` — базовая валюта по умолчанию;
    - ``log_level`` — уровень логирования (INFO, для отладки DEBUG);
    - ``log_max_bytes`` / ``log_backup_count`` — параметры ротации логов.
    """

    _instance: "SettingsLoader | None" = None

    def __new__(cls) -> "SettingsLoader":
        if cls._instance is None:
            instance = super().__new__(cls)
            instance._load()
            cls._instance = instance
        return cls._instance

    def __init__(self) -> None:
        # __new__ уже инициализировал существующий экземпляр;
        # повторную загрузку выполняем только при первом создании.
        if not hasattr(self, "_values"):
            self._load()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = ...) -> Any:
        """Значение настройки по ключу (с fallback на дефолт)."""
        if key in self._values:
            return self._values[key]
        if default is not ...:
            return default
        return _DEFAULTS.get(key)

    def reload(self) -> None:
        """Перечитать конфигурацию с диска и из окружения."""
        self._load()

    def as_dict(self) -> dict[str, Any]:
        """Копия всех текущих значений (для отладки)."""
        return dict(self._values)

    # -- удобные свойства для часто используемых ключей ------------------

    @property
    def data_dir(self) -> Path:
        return self._resolve_path(self.get("data_dir"))

    @property
    def logs_dir(self) -> Path:
        return self._resolve_path(self.get("logs_dir"))

    @property
    def rates_ttl_seconds(self) -> float:
        return float(self.get("rates_ttl_seconds"))

    @property
    def default_base_currency(self) -> str:
        return str(self.get("default_base_currency"))

    @property
    def log_level(self) -> str:
        return str(self.get("log_level"))

    @property
    def log_max_bytes(self) -> int:
        return int(self.get("log_max_bytes"))

    @property
    def log_backup_count(self) -> int:
        return int(self.get("log_backup_count"))

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Собрать значения: дефолты → pyproject.toml → переменные окружения."""
        values = dict(_DEFAULTS)
        values.update(self._read_pyproject())
        values.update(self._read_env())
        self._values = values

    @staticmethod
    def _read_pyproject() -> dict[str, Any]:
        """Прочитать секцию [tool.valutatrade] из pyproject.toml (если есть)."""
        pyproject = _PROJECT_ROOT / "pyproject.toml"
        try:
            with pyproject.open("rb") as file:
                data = tomllib.load(file)
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
            return {}
        section = data.get("tool", {}).get("valutatrade", {})
        return section if isinstance(section, dict) else {}

    @staticmethod
    def _read_env() -> dict[str, Any]:
        """Переопределения из переменных окружения VALUTATRADE_*."""
        overrides: dict[str, Any] = {}
        for key, default in _DEFAULTS.items():
            raw = os.environ.get(_ENV_PREFIX + key.upper())
            if raw is None:
                continue
            try:
                if isinstance(default, bool):
                    overrides[key] = raw.strip().lower() in {"1", "true", "yes"}
                elif isinstance(default, int):
                    overrides[key] = int(raw)
                elif isinstance(default, float):
                    overrides[key] = float(raw)
                else:
                    overrides[key] = raw
            except ValueError:
                continue  # некорректное значение в окружении игнорируем
        return overrides

    @staticmethod
    def _resolve_path(value: Any) -> Path:
        """Относительный путь разрешается от корня проекта, абсолютный — как есть."""
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else _PROJECT_ROOT / path

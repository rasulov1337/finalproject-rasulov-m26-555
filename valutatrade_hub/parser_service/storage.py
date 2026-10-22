"""Хранилище Parser Service: журнал измерений и срез последних курсов.

Два файла в ``data/``:

- ``exchange_rates.json`` — журнал ВСЕХ измерений (история). Каждая запись —
  одна пара валют с курсом, временем и источником; уникальный ``id``
  составляется из пары и времени, поэтому один и тот же замер не
  дублируется (это защищает историю при частых обновлениях);
- ``rates.json`` — срез последних корректных курсов для Core Service.
  Пара обновляется, только если её ``updated_at`` свежеe текущего
  («апдейт побеждает»), поэтому частичное обновление (``--source``)
  не затирает курсы из другого источника.

Обе записи атомарны: сначала пишем во временный файл, затем переименование.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.exceptions import ApiRequestError
from ..core.utils import now_utc, parse_dt, to_iso_utc, to_naive_local
from .api_clients import RateEntry
from .config import ParserConfig


class RatesStorage:
    """Чтение/запись ``exchange_rates.json`` (журнал) и ``rates.json`` (срез)."""

    def __init__(self, config: ParserConfig | None = None) -> None:
        self._config = config if config is not None else ParserConfig()

    # ------------------------------------------------------------------
    # Журнал измерений (exchange_rates.json)
    # ------------------------------------------------------------------

    def load_history(self) -> list[dict[str, Any]]:
        """Все записи журнала (пустой список, если файла нет)."""
        data = self._read_json(self._config.HISTORY_FILE_PATH, [])
        return data if isinstance(data, list) else []

    def append_history(self, entries: Iterable[RateEntry]) -> int:
        """Дописать записи в журнал с дедупликацией по ``id``.

        :return: количество реально добавленных записей.
        """
        history = self.load_history()
        known = {record.get("id") for record in history if isinstance(record, dict)}
        added = 0
        for entry in entries:
            if entry.record_id in known:
                continue
            from_currency, _, to_currency = entry.pair.partition("_")
            history.append(
                {
                    "id": entry.record_id,
                    "from_currency": from_currency,
                    "to_currency": to_currency,
                    "rate": entry.rate,
                    "timestamp": to_iso_utc(entry.timestamp),
                    "source": entry.source,
                    "meta": entry.meta,
                }
            )
            known.add(entry.record_id)
            added += 1
        if added:
            self._write_json(self._config.HISTORY_FILE_PATH, history)
        return added

    # ------------------------------------------------------------------
    # Срез последних курсов (rates.json)
    # ------------------------------------------------------------------

    def load_snapshot(self) -> dict[str, Any]:
        """Текущий срез: ``{"pairs": {...}, "last_refresh": ...}``.

        Понимает и предыдущий (плоский) формат файла — данные не теряются
        при переходе на новый формат.
        """
        data = self._read_json(self._config.RATES_FILE_PATH, {})
        if not isinstance(data, dict) or not data:
            return {"pairs": {}, "last_refresh": ""}
        pairs = data.get("pairs")
        if isinstance(pairs, dict):
            merged = dict(pairs)
        else:
            # Плоский формат: {"EUR_USD": {...}} на верхнем уровне.
            merged = {
                key: value
                for key, value in data.items()
                if isinstance(key, str) and key.endswith("_USD") and isinstance(value, dict)
            }
        return {
            "pairs": merged,
            "last_refresh": str(data.get("last_refresh", "")),
        }

    def save_snapshot(self, entries: Iterable[RateEntry]) -> tuple[int, datetime]:
        """Обновить срез ``rates.json``; слияние «свежее побеждает».

        :return: ``(количество обновлённых пар, время last_refresh)``.
        """
        snapshot = self.load_snapshot()
        pairs: dict[str, Any] = snapshot["pairs"]
        written = 0
        for entry in entries:
            existing = pairs.get(entry.pair)
            if isinstance(existing, dict):
                existing_dt = parse_dt(existing.get("updated_at"))
            else:
                existing_dt = None
            # Обе стороны приводятся к naive-локальному: parse_dt нормализует
            # aware-значения, entry.timestamp может быть aware (UTC от API).
            if existing_dt is not None and existing_dt >= to_naive_local(entry.timestamp):
                continue  # текущее значение не старше — не затираем
            pairs[entry.pair] = {
                "rate": entry.rate,
                "updated_at": to_iso_utc(entry.timestamp),
                "source": entry.source,
            }
            written += 1
        last_refresh = now_utc()
        self._write_json(
            self._config.RATES_FILE_PATH,
            {
                "pairs": dict(sorted(pairs.items())),
                "last_refresh": to_iso_utc(last_refresh),
            },
        )
        return written, last_refresh

    # ------------------------------------------------------------------
    # Внутреннее: атомарная запись и чтение JSON
    # ------------------------------------------------------------------

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        if not text.strip():
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ApiRequestError(
                f"не удалось прочитать {path.name}: повреждённый JSON ({exc})"
            ) from exc

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        """Атомарная запись: временный файл → переименование."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise ApiRequestError(f"не удалось записать {path.name} ({exc})") from exc

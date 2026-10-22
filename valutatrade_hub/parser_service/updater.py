"""Координатор обновления курсов: ``RatesUpdater``.

Опрашивает всех API-клиентов, объединяет результаты, пишет срез
``data/rates.json`` и журнал ``data/exchange_rates.json``. Отказоустойчив:
падение одного клиента (например, нет ключа ExchangeRate-API) залогируется
и не мешает сохранить курсы остальных источников. Каждый шаг пишется
в ``logs/parser.log``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.exceptions import ApiRequestError, ValidationError
from ..core.utils import to_iso_utc
from ..logging_config import setup_parser_logging
from .api_clients import BaseApiClient, CoinGeckoClient, ExchangeRateApiClient, RateEntry
from .config import ParserConfig
from .storage import RatesStorage


@dataclass(frozen=True)
class ClientResult:
    """Итог опроса одного API-клиента."""

    client_name: str
    ok: bool
    rates_count: int = 0
    error: str = ""


@dataclass(frozen=True)
class UpdateResult:
    """Итог одного цикла обновления курсов."""

    results: list[ClientResult] = field(default_factory=list)
    entries_count: int = 0  # всего получено пар от клиентов
    total_written: int = 0  # пар, реально записанных в rates.json
    history_added: int = 0  # записей, добавленных в журнал
    last_refresh: str = ""

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(item.ok for item in self.results)


class RatesUpdater:
    """Точка входа логики парсинга: опрос клиентов → объединение → сохранение."""

    def __init__(
        self,
        clients: list[BaseApiClient] | None = None,
        storage: RatesStorage | None = None,
        config: ParserConfig | None = None,
    ) -> None:
        self._config = config if config is not None else ParserConfig()
        self._clients = clients or [
            CoinGeckoClient(self._config),
            ExchangeRateApiClient(self._config),
        ]
        self._storage = storage if storage is not None else RatesStorage(self._config)
        self._logger = setup_parser_logging()

    @property
    def config(self) -> ParserConfig:
        return self._config

    def run_update(self, source: str | None = None) -> UpdateResult:
        """Опросить клиентов (всех или один ``source``) и сохранить курсы.

        :param source: ``coingecko`` или ``exchangerate`` (без учёта регистра);
            None — опросить все источники.
        """
        clients = self._filter_clients(source)
        self._logger.info("Starting rates update (sources: %s)", ", ".join(c.name for c in clients))

        entries: dict[str, RateEntry] = {}
        results: list[ClientResult] = []
        for client in clients:
            try:
                fetched = client.fetch_rates()
            except ApiRequestError as exc:
                self._logger.error("Failed to fetch from %s: %s", client.name, exc.reason)
                results.append(ClientResult(client_name=client.name, ok=False, error=exc.reason))
                continue
            self._logger.info("Fetched from %s: OK (%d rates)", client.name, len(fetched))
            entries.update(fetched)
            results.append(ClientResult(client_name=client.name, ok=True, rates_count=len(fetched)))

        total_written = 0
        history_added = 0
        last_refresh = ""
        if entries:
            self._logger.info("Writing %d rates to %s", len(entries), self._config.RATES_FILE_PATH)
            history_added = self._storage.append_history(entries.values())
            total_written, refreshed_at = self._storage.save_snapshot(entries.values())
            last_refresh = to_iso_utc(refreshed_at).removesuffix("Z")

        result = UpdateResult(
            results=results,
            entries_count=len(entries),
            total_written=total_written,
            history_added=history_added,
            last_refresh=last_refresh,
        )
        self._log_summary(result)
        return result

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _filter_clients(self, source: str | None) -> list[BaseApiClient]:
        """Отобрать клиентов по значению --source (None — все)."""
        if source is None:
            return list(self._clients)
        normalized = str(source).strip().lower()
        matches = [client for client in self._clients if self._matches_source(client, normalized)]
        if not matches:
            raise ValidationError(
                f"'source' должен быть 'coingecko' или 'exchangerate' (получено: '{source}')"
            )
        return matches

    @staticmethod
    def _matches_source(client: BaseApiClient, source: str) -> bool:
        """«exchangerate» совпадает с клиентом «ExchangeRate-API» и т.п."""
        client_key = client.name.lower().replace("-api", "").replace(" ", "")
        return source in client_key or source in client.name.lower()

    def _log_summary(self, result: UpdateResult) -> None:
        """Итоговая запись цикла в logs/parser.log."""
        ok = sum(1 for item in result.results if item.ok)
        failed = len(result.results) - ok
        level = self._logger.info if result.all_ok else self._logger.warning
        level(
            "Update finished: ok=%d failed=%d written=%d history_added=%d last_refresh=%s",
            ok,
            failed,
            result.total_written,
            result.history_added,
            result.last_refresh or "-",
        )


def update_result_to_dict(result: UpdateResult) -> dict[str, Any]:
    """Представление результата для отладки/логов."""
    return {
        "clients": [item.__dict__ for item in result.results],
        "entries_count": result.entries_count,
        "total_written": result.total_written,
        "history_added": result.history_added,
        "last_refresh": result.last_refresh,
        "all_ok": result.all_ok,
    }

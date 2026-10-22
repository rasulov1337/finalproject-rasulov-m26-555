"""Parser Service — сбор и обновление курсов валют из внешних API.

Модули пакета:

- ``config`` — ``ParserConfig``: ключи, эндпоинты, списки валют, пути, таймауты;
- ``api_clients`` — клиенты CoinGecko (крипта) и ExchangeRate-API (фиат);
- ``updater`` — ``RatesUpdater``: опрос клиентов, объединение, сохранение;
- ``storage`` — журнал ``exchange_rates.json`` и срез ``rates.json``;
- ``scheduler`` — периодическое обновление по таймеру.
"""

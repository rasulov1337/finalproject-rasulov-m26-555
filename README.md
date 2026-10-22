# ValutaTrade Hub

Платформа для отслеживания и симуляции торговли валютами: регистрация
пользователей, управление виртуальным портфелем фиатных и крипто валют,
сделки покупки/продажи и отслеживание актуальных курсов.

Система состоит из двух сервисов:

- **Основной Сервис (Core Service)** — это приложение: CLI, пользователи,
  кошельки, сделки. За курсы не ходит в сеть — читает локальный кеш
  `data/rates.json`.
- **Сервис Парсинга (Parser Service)** — пакет `valutatrade_hub/parser_service/`:
  обращается к внешним API ([CoinGecko](https://www.coingecko.com/) —
  криптовалюты, [ExchangeRate-API](https://www.exchangerate-api.com/) — фиат),
  обновляет кеш `data/rates.json` и ведёт журнал измерений
  `data/exchange_rates.json`. Запускается командой `update-rates` — разово
  или по расписанию (`--interval`).

## Структура проекта

```text
finalproject-rasulov-m26-555/
│
├── data/
│    ├── users.json             # список пользователей
│    ├── portfolios.json        # портфели и кошельки
│    ├── rates.json             # кеш курсов (последний срез)
│    └── exchange_rates.json    # журнал измерений (история курсов)
├── logs/
│    ├── actions.log            # журнал операций (ротация 1 МБ × 3)
│    └── parser.log             # журнал Parser Service
├── demo/
│    └── demo.cast              # запись демо (asciinema play demo/demo.cast)
├── valutatrade_hub/
│    ├── __init__.py
│    ├── logging_config.py      # настройка логов (формат, уровень, ротация)
│    ├── decorators.py          # @log_action — трассировка доменных операций
│    ├── core/
│    │    ├── __init__.py
│    │    ├── currencies.py     # иерархия Currency → Fiat/Crypto + реестр
│    │    ├── exceptions.py     # пользовательские исключения
│    │    ├── models.py         # классы User, Wallet, Portfolio
│    │    ├── usecases.py       # бизнес-логика: AppService, RateService
│    │    └── utils.py          # валидация, конвертации, форматирование
│    ├── infra/
│    │    ├── __init__.py
│    │    ├── settings.py       # Singleton SettingsLoader (конфигурация)
│    │    └── database.py       # Singleton DatabaseManager (JSON-хранилище)
│    ├── parser_service/        # Сервис Парсинга (раздел 4)
│    │    ├── __init__.py
│    │    ├── config.py         # ParserConfig: ключи, эндпоинты, списки валют
│    │    ├── api_clients.py    # CoinGeckoClient, ExchangeRateApiClient (ABC)
│    │    ├── storage.py        # журнал exchange_rates.json + срез rates.json
│    │    ├── updater.py        # RatesUpdater: опрос клиентов → сохранение
│    │    └── scheduler.py      # периодическое обновление (run_forever)
│    └── cli/
│         ├── __init__.py
│         └── interface.py      # команды CLI
│
├── main.py                     # точка входа
├── uv.lock
├── pyproject.toml              # метаданные + секции [tool.valutatrade]/[tool.ruff]
├── README.md
└── .gitignore
```

## Требования

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) — зависимости ставятся командой `uv sync`
  (`requests` — HTTP-клиент парсера, `prettytable` — таблицы `show-rates`)

## Запуск

```bash
uv sync                       # создать окружение (uv.lock уже в репозитории)
uv run main.py                # интерактивный режим (REPL)
uv run valutatrade-hub        # то же самое через консольную точку входа
```

Одна команда без входа в REPL:

```bash
uv run main.py get-rate --from USD --to BTC
uv run main.py update-rates --source coingecko
```

## Команды

| Команда | Описание |
| --- | --- |
| `register --username <имя> --password <пароль>` | Зарегистрировать нового пользователя (создаётся пустой портфель) |
| `login --username <имя> --password <пароль>` | Войти в систему |
| `logout` | Выйти из системы |
| `whoami` | Информация о текущем пользователе |
| `change-password --new-password <пароль>` | Изменить пароль |
| `show-portfolio [--base USD]` | Показать кошельки и итоговую стоимость в базовой валюте |
| `buy --currency <код> --amount <кол-во>` | Купить валюту (кошелёк создаётся автоматически) |
| `sell --currency <код> --amount <кол-во>` | Продать валюту (выручка зачисляется в USD) |
| `get-rate --from <код> --to <код>` | Показать курс валюты к валюте и обратный курс |
| `currencies` | Список поддерживаемых валют (фиат и крипто) |
| `add-currency --currency <код>` | Добавить пустой кошелёк |
| `deposit --currency <код> --amount <кол-во>` | Пополнить кошелёк (демо-режим) |
| `update-rates [--source coingecko\|exchangerate] [--interval <сек>]` | Обновить курсы через Parser Service (`--interval` — по расписанию, выход по Ctrl+C) |
| `show-rates [--currency <код>] [--top <N>] [--base <код>]` | Таблица курсов из локального кеша |
| `help` / `exit` | Справка / выход |

## Сервис Парсинга (раздел 4)

### Источники курсов

| Клиент | Источник | Что даёт | Ключ |
| --- | --- | --- | --- |
| `CoinGeckoClient` | `api.coingecko.com/api/v3/simple/price` | курсы криптовалют (BTC, ETH, USDT, TON, SOL) к USD | не нужен (публичный API) |
| `ExchangeRateApiClient` | `v6.exchangerate-api.com` | курсы фиатных валют (EUR, GBP, RUB, JPY, CNY, KZT) к USD | **нужен** |

API-ключ ExchangeRate-API **хранится только в переменной окружения**
`EXCHANGERATE_API_KEY` — в коде и git его нет:

```bash
export EXCHANGERATE_API_KEY="ваш-ключ"   # получить: https://www.exchangerate-api.com/
uv run main.py update-rates
```

Без ключа крипто-курсы всё равно обновятся через CoinGecko, а по фиату будет
честная ошибка:

```text
INFO: Starting rates update...
INFO: Fetching from CoinGecko... OK (5 rates)
ERROR: Failed to fetch from ExchangeRate-API: API key not set (EXCHANGERATE_API_KEY)
INFO: Writing 5 rates to data/rates.json...
Update completed with errors. Check logs/parser.log for details.
```

Оба клиента наследуют абстрактный `BaseApiClient` (интерфейс
`fetch_rates() -> dict[пара → RateEntry]`), поэтому новый источник — это ещё
один подкласс. Все сетевые сбои (таймаут, HTTP 429/403, битый JSON, отсутствие
ключа) нормализуются в `ApiRequestError` с понятной причиной (reason).

### Команда `update-rates`

1. опрашивает клиентов (всех или один по `--source`);
2. курсы от отказавшего источника не теряются — сохраняется то, что удалось
   получить (отказоустойчивость);
3. пишет журнал `data/exchange_rates.json` — **каждое измерение** с
   уникальным `id` вида `<FROM>_<TO>_<UTC-timestamp>`; повторный замер с тем
   же временем не дублируется (дедупликация по `id`);
4. обновляет срез `data/rates.json` по правилу «апдейт побеждает, если
   `updated_at` свежее текущего» — частичное обновление (`--source`) не
   затирает курсы из другого источника.

`update-rates --interval 300` — планировщик: обновлять каждые N секунд,
выход по Ctrl+C. Все шаги пишутся в `logs/parser.log`.

### Команда `show-rates`

Таблица курсов из кеша (prettytable) с временем обновления каждой пары:

```text
> show-rates --top 3
Rates from cache (updated at 2026-10-22T16:03:00):
+---------+----------+---------------------+
| Pair    |   Rate   |       Updated       |
+---------+----------+---------------------+
| BTC_USD | 59337.21 | 2026-10-22 16:03:00 |
| ETH_USD |   3720   | 2026-10-22 16:03:00 |
| SOL_USD |  145.12  | 2026-10-22 16:03:00 |
+---------+----------+---------------------+
```

Фильтры (в порядке приоритета): `--currency <код>` — одна пара;
`--top <N>` — N самых дорогых криптовалют; `--base <код>` — кросс-курсы всех
валют к указанной (например, `--base EUR`); без флагов — все пары к USD.

## Как работают курсы

Курсы хранятся как «1 единица валюты → USD» (например, `BTC_USD: 59337.21`).
Курс произвольной пары A→B считается кросс-курсом через USD:
`rates[A] / rates[B]` (утилита `cross_rate`).

`get-rate` и сделки работают так:

1. читаем локальный кеш `data/rates.json`;
2. кеш пуст → `RatesNotAvailableError`: «Локальный кеш курсов пуст.
   Выполните 'update-rates', чтобы загрузить данные.»;
3. кеш старше `rates_ttl_seconds` (по умолчанию 300 c) → курсы
   используются, но к выводу добавляется предупреждение:
   «Внимание: кеш курсов устарел — выполните update-rates.»;
4. кода нет в кеше (например, источник недоступен) → «Курс для 'XYZ' не
   найден в кеше.»

Обновляет кеш **только** команда `update-rates` (Parser Service): Core
Service в сеть не ходит, поэтому приложение продолжает работать даже без
интернета — просто предупреждает об устаревании данных.

## Иерархия валют (раздел 3.1)

Валюты — закрытый реестр: сделки, курсы и кошельки работают только с
валютами из `core/currencies.py`. Новая валюта добавляется одной записью в
`_REGISTRY_DATA` (и тикером в `CRYPTO_ID_MAP` парсера, если это крипта).
Пример: `SOL` (Solana, Proof-of-History, капитализация 6.0e10) добавлена в
разделе 4.

- абстрактный базовый класс `Currency`: публичные атрибуты `name`, `code`;
  инварианты — код в верхнем регистре, 2–5 символов, без пробелов; имя —
  непустая строка;
- `FiatCurrency` добавляет `issuing_country`;
- `CryptoCurrency` добавляет `algorithm` и `market_cap`;
- фабрика `get_currency(code)` возвращает объект валюты (регистр не важен),
  для неизвестного кода бросает `CurrencyNotFoundError`;
- `Wallet.currency` — свойство-интеграция: полиморфный объект валюты кошелька.

## Пользовательские исключения (раздел 3.2)

Все исключения наследуются от `ValutaTradeError` (модуль
`core/exceptions.py`); CLI печатает их сообщения как есть и добавляет
подсказки:

| Исключение | Сообщение | Где возникает |
| --- | --- | --- |
| `InsufficientFundsError` | `Недостаточно средств: доступно {available} {code}, требуется {required} {code}` | `Wallet.withdraw()`, `sell` |
| `CurrencyNotFoundError` | `Неизвестная валюта '{code}'` (+ список кодов) | `get_currency()`, `buy`/`sell`/`get-rate` |
| `ApiRequestError` | `Ошибка при обращении к внешнему API: {reason}` | клиенты Parser Service |
| `RatesNotAvailableError` | `Локальный кеш курсов пуст. Выполните 'update-rates'...` / `Курс для 'XYZ' не найден в кеше.` | `RateService` (чтение кеша) |

## Конфигурация и синглтоны (раздел 3.3)

`SettingsLoader` (singleton через `__new__` — проще и читабельнее
метакласса) читает секцию `[tool.valutatrade]` из `pyproject.toml`.
Приоритет: переменные окружения `VALUTATRADE_*` → pyproject.toml → дефолты.

| Ключ | По умолчанию | Смысл |
| --- | --- | --- |
| `data_dir` | `data` | каталог JSON-хранилищ |
| `logs_dir` | `logs` | каталог логов |
| `rates_ttl_seconds` | `300` | сколько секунд кеш курсов считается свежим |
| `default_base_currency` | `USD` | базовая валюта по умолчанию |
| `log_level` | `INFO` | уровень логирования (отладка — `DEBUG`) |
| `log_max_bytes` | `1048576` | размер `logs/actions.log` до ротации |
| `log_backup_count` | `3` | количество файлов ротации |

Пример: `VALUTATRADE_DATA_DIR=/tmp/vt VALUTATRADE_RATES_TTL_SECONDS=60 uv run main.py`

`DatabaseManager` (тоже singleton) — единая точка доступа к `data/*.json`:
атомарная запись через временный файл и безопасная операция
`update(collection, mutate)` «чтение → модификация → запись»: если мутация
бросила исключение, файл не перезаписывается. Синглтоны используются в
`AppService`, `RateService` (TTL) и `Portfolio.user`.

## Логирование операций (раздел 3.4)

`@log_action` (модуль `decorators.py`) применяется к `register`, `login`,
`buy`, `sell` в `usecases.py`. Формат — текстовый, одна строка на событие:

```text
INFO 2026-10-09T12:05:22 BUY user='alice' currency='BTC' amount=0.0500 base='USD' rate=59337.21 before=0.0000 after=0.0500 result=OK
INFO 2026-10-09T12:06:10 SELL user='alice' currency='BTC' amount=99.0000 base='USD' result=ERROR error_type=InsufficientFundsError error_message='Недостаточно средств: ...'
```

Поля: timestamp, действие, пользователь, валюта, сумма, курс, базовая
валюта, результат (`OK`/`ERROR`), при ошибке — `error_type`/`error_message`.
Опция `verbose=True` (включена для `buy`/`sell`) добавляет состояние
кошелька «было→стало» (`before`/`after`). Декоратор не глотает исключения:
ошибка фиксируется в лог и пробрасывается дальше. Ротация —
`RotatingFileHandler` (1 МБ, 3 бэкапа), уровень из настроек, в stdout лог
не пишется.

Parser Service пишет собственный журнал `logs/parser.log` (тот же формат и
ротация): старты обновлений, ответы источников, итоги цикла.

## Логика сделок

- **Покупка** (`buy`): кошелёк покупаемой валюты создаётся автоматически,
  баланс увеличивается на `amount`. Стоимость покупки (`amount * курс`)
  списывается с USD-кошелька, если он есть и на нём достаточно средств;
  иначе стоимость выводится как оценочная.
- **Продажа** (`sell`): проверяется наличие кошелька и достаточность средств,
  баланс уменьшается, а выручка в USD зачисляется на USD-кошелёк
  (он создаётся автоматически при первой продаже).
- Пароль хранится только в виде одностороннего псевдо-хеша
  `sha256(password + salt)`; соль генерируется для каждого пользователя
  и хранится рядом в `users.json`. Хеш и соль — приватные атрибуты
  пользователя (name mangling).

## Формат данных

`data/users.json`:

```json
[
  {
    "user_id": 1,
    "username": "alice",
    "hashed_password": "3e2a19...",
    "salt": "x5T9!",
    "registration_date": "2025-10-09T12:00:00"
  }
]
```

`data/portfolios.json`:

```json
[
  {
    "user_id": 1,
    "wallets": {
      "USD": {"currency_code": "USD", "balance": 1500.0},
      "BTC": {"currency_code": "BTC", "balance": 0.05},
      "EUR": {"currency_code": "EUR", "balance": 200.0}
    }
  }
]
```

`data/rates.json` — кеш курсов (последний срез, пишет Parser Service):

```json
{
  "pairs": {
    "BTC_USD": {"rate": 81632.0, "updated_at": "2026-10-22T13:03:01Z", "source": "CoinGecko"},
    "EUR_USD": {"rate": 1.0786, "updated_at": "2026-10-22T13:03:00Z", "source": "ExchangeRate-API"}
  },
  "last_refresh": "2026-10-22T13:03:01Z"
}
```

`data/exchange_rates.json` — журнал измерений (история):

```json
[
  {
    "id": "BTC_USD_2026-10-22T13:03:01Z",
    "from_currency": "BTC",
    "to_currency": "USD",
    "rate": 81632.0,
    "timestamp": "2026-10-22T13:03:01Z",
    "source": "CoinGecko",
    "meta": {"raw_id": "bitcoin", "request_ms": 412, "status_code": 200, "etag": "\"abc\""}
  }
]
```

## Качество кода

Линтер и форматер — [ruff](https://docs.astral.sh/ruff/) (конфигурация в
`pyproject.toml`, включая правила для русскоязычных строк):

```bash
uv run ruff check .            # 0 ошибок
uv run ruff format --check .   # форматирование
```

## Демо

Запись полного цикла работы (регистрация → вход → пополнение → сделки →
портфель → курсы → обновление через Parser Service → обработка ошибок):

```bash
asciinema play demo/demo.cast
```

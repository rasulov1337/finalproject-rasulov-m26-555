# ValutaTrade Hub

Платформа для отслеживания и симуляции торговли валютами: регистрация
пользователей, управление виртуальным портфелем фиатных и крипто валют,
сделки покупки/продажи и отслеживание актуальных курсов.

Система состоит из двух сервисов:

- **Сервис Парсинга (Parser Service)** — отдельное приложение, которое по
  запросу или расписанию обращается к публичным API, получает актуальные
  курсы, сравнивает их с предыдущими значениями и сохраняет историю в базу
  данных. Пока он не подключён, используется заглушка `StubParserClient`
  с фиксированными курсами и локальный кеш `data/rates.json`.
- **Основной Сервис (Core Service)** — это приложение: CLI, пользователи,
  кошельки, история транзакций и взаимодействие с сервисом парсинга для
  получения актуальных курсов.

## Структура проекта

```text
finalproject-rasulov-m26-555/
│
├── data/
│    ├── users.json             # список пользователей
│    ├── portfolios.json        # портфели и кошельки
│    └── rates.json             # кеш курсов валют
├── logs/
│    └── actions.log            # журнал операций (ротация 1 МБ × 3)
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
│    │    └── utils.py          # валидация, хеширование, конвертации, форматирование
│    ├── infra/
│    │    ├── __init__.py
│    │    ├── settings.py       # Singleton SettingsLoader (конфигурация)
│    │    └── database.py       # Singleton DatabaseManager (JSON-хранилище)
│    └── cli/
│         ├── __init__.py
│         └── interface.py      # команды CLI
│
├── main.py                     # точка входа
├── uv.lock
├── pyproject.toml              # метаданные + секция [tool.valutatrade]
├── README.md
└── .gitignore
```

## Требования

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (или любой Python 3.11+ без uv — проект
  не использует внешних зависимостей)

## Запуск

```bash
uv sync                       # создать окружение (uv.lock уже в репозитории)
uv run main.py                # интерактивный режим (REPL)
uv run valutatrade-hub        # то же самое через консольную точку входа
```

Без uv:

```bash
python3 main.py
```

Одна команда без входа в REPL:

```bash
uv run main.py get-rate --from USD --to BTC
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
| `refresh-rates` | Принудительно обновить курсы через Parser Service |
| `help` / `exit` | Справка / выход |

## Иерархия валют (раздел 3.1)

Валюты — закрытый реестр: сделки, курсы и кошельки работают только с
валютами из `core/currencies.py`. Новая валюта добавляется одной записью в
`_REGISTRY_DATA` (и, при необходимости, курсом в `utils.STUB_USD_RATES`).

- абстрактный базовый класс `Currency`: публичные атрибуты `name`, `code`;
  инварианты — код в верхнем регистре, 2–5 символов, без пробелов; имя —
  непустая строка;
- `FiatCurrency` добавляет `issuing_country`;
- `CryptoCurrency` добавляет `algorithm` и `market_cap`;
- фабрика `get_currency(code)` возвращает объект валюты (регистр не важен),
  для неизвестного кода бросает `CurrencyNotFoundError`;
- `Wallet.currency` — свойство-интеграция: полиморфный объект валюты кошелька.

```text
> currencies
Поддерживаемые валюты:
  [FIAT] CNY — Chinese Yuan (Issuing: China)
  ...
  [FIAT] USD — US Dollar (Issuing: United States)
  [CRYPTO] BTC — Bitcoin (Algo: SHA-256, MCAP: 1.12e12)
  ...
```

## Пользовательские исключения (раздел 3.2)

Все исключения наследуются от `ValutaTradeError` (модуль
`core/exceptions.py`); CLI печатает их сообщения как есть и добавляет
подсказки:

| Исключение | Сообщение | Где возникает |
| --- | --- | --- |
| `InsufficientFundsError` | `Недостаточно средств: доступно {available} {code}, требуется {required} {code}` | `Wallet.withdraw()`, `sell` |
| `CurrencyNotFoundError` | `Неизвестная валюта '{code}'` (+ список кодов) | `get_currency()`, `buy`/`sell`/`get-rate` |
| `ApiRequestError` | `Ошибка при обращении к внешнему API: {reason}` (+ «повторите позже») | слой получения курсов |

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

## Как работают курсы

Курсы хранятся как «1 единица валюты → USD» (например, `BTC_USD: 59337.21`).
Курс произвольной пары A→B считается кросс-курсом через USD:
`rates[A] / rates[B]` (утилита `cross_rate`).

Порядок получения курса (`get-rate` и сделки):

1. берём курс из локального кеша `data/rates.json`;
2. если кеш свежий (моложе `rates_ttl_seconds`) — используем его;
3. иначе запрашиваем у Parser Service (заглушка), обновляем кеш и показываем курс;
4. при сбое сервиса — `ApiRequestError`, но если есть хотя бы устаревший
   кеш, используется он.

### Единый контракт Parser Service

Чтобы подключить реальный сервис парсинга, достаточно реализовать протокол
`ParserClient` из `valutatrade_hub/core/usecases.py` и передать его в
`RateService`:

```python
class ParserClient(Protocol):
    source: str
    def fetch_rates(self) -> dict[str, float]:
        """Вернуть словарь «код валюты → сколько USD стоит 1 единица»."""
```

Заглушка `StubParserClient` возвращает фиксированные курсы из
`utils.STUB_USD_RATES`. HTTP-реализация будет отличаться только внутренностями
`fetch_rates()` (запрос к API Parser Service вместо чтения константы).

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

`data/rates.json` (кеш курсов):

```json
{
  "EUR_USD": {"rate": 1.0786, "updated_at": "2025-10-09T10:30:00"},
  "BTC_USD": {"rate": 59337.21, "updated_at": "2025-10-09T10:29:42"},
  "source": "ParserService",
  "last_refresh": "2025-10-09T10:35:00"
}
```

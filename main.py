"""Точка входа приложения ValutaTrade Hub.

Запуск интерактивного режима::

    python main.py            # или: uv run main.py

Выполнение одной команды без входа в REPL::

    python main.py get-rate --from USD --to BTC
"""

from valutatrade_hub.cli.interface import main

if __name__ == "__main__":
    main()

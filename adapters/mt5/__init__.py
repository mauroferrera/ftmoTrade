"""Adaptador de MetaTrader 5: la única puerta al terminal.

El paquete `MetaTrader5` habla con UN terminal por proceso y no tolera que dos
hilos lo toquen a la vez. Aquí hay una sola sesión (`Session`) con un executor de
un hilo, un lock, y un contador de referencias para que el `close()` espere a que
no quede nadie dentro. La sesión se abre una vez y se reutiliza; `close()` la
cierra.
"""

from adapters.mt5.mt5_forex import (  # noqa: F401
    MT5ForexAdapter,
    Session,
    adapter,
    get_session,
    set_session,
)

__all__ = [
    "MT5ForexAdapter",
    "Session",
    "adapter",
    "get_session",
    "set_session",
]

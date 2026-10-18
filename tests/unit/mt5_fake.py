"""Doble de prueba del paquete `MetaTrader5`, sin terminal.

Por qué un doble y no un parche de `sys.modules`
---------------------------------------------
El paquete `MetaTrader5` es un import nativo: en una máquina sin terminal
instalada, `import MetaTrader5` falla en la propia línea del import, y ni
`monkeypatch.setattr` ni `unittest.mock.patch` llegan a tiempo. Poner un módulo
falso en `sys.modules` ANTES del import sí funciona, y es la única forma de
probar la lógica del adaptador en CI o en un portátil donde no hay bróker.

Este doble no pretende parecerse a MT5. Pretende ser lo que los tests necesitan
comprobar:

  - Que el adaptador llama a la función correcta con los argumentos correctos.
  - Que normaliza bien lo que MT5 devuelve (que es un namedtuple POSICIONAL).
  - Que un fallo del bróker sale como `AdapterError` y no como una excepción
    cruda de la librería.

La que más importa es la normalización: si el adaptador deja que un
`copy_rates_from_pos` devuelva `None` sin comprobarlo, el llamante recibe un
`None` donde esperaba velas, y el error aparece tres capas más arriba como un
`TypeError: 'NoneType' object is not subscriptable`.

Que el doble NO levante excepciones al detectar concurrencia es deliberado: si
`shutdown()` fallara, el test probaría que el doble funciona y no que el
adaptador serializa el acceso. Se registra el hecho (`shutdown_while_busy`) y
es el test quien afirma que sigue a False.

Uso
---
    mt5 = FakeMT5.install(monkeypatch)      # devuelve el doble, ya en sys.modules
    mt5.set_rates([...])                     # qué devuelve copy_rates_from_pos
    ...
    monkeypatch.undo()                       # implícito al final del test
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List, Optional, Sequence

# Constantes reales de MT5. Los valores importan: `TIMEFRAME_H1` en MT5 NO es
# 60 sino 16385 (los timeframes de horas empiezan en el bit 15). Un doble que
# usara 60 "para que se entienda" dejaría pasar el bug de pasar 60 donde el
# bróker espera 16385, que es justo lo que este test existe para cazar.
TIMEFRAME_M1 = 1
TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15
TIMEFRAME_M30 = 30
TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
TIMEFRAME_D1 = 16408
TIMEFRAME_W1 = 32769

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

# Constantes del enum de EJECUCIÓN. Los valores importan y están verificados
# contra el bróker: `ORDER_FILLING_FOK` es 0, no 1. El adaptador de ejecución las
# lleva como literales para poder importarse sin el paquete instalado;
# `test_constantes_coinciden_con_el_paquete` compara ambos juegos y falla si
# divergen.
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 3
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_INVALID_FILL = 10030


class FakeTick:
    """Tick de MT5: los campos son atributos, no índices."""

    def __init__(
        self,
        time: int,
        bid: float,
        ask: float,
        last: float = 0.0,
        volume: int = 1,
        flags: int = 0,
    ) -> None:
        self.time = int(time)
        self.bid = float(bid)
        self.ask = float(ask)
        self.last = float(last)
        self.volume = int(volume)
        self.time_msc = int(time) * 1000
        self.flags = int(flags)
        self.volume_real = float(volume)


class FakeSymbolInfo:
    """`symbol_info()`: los specs de cotización, sin los de contrato."""

    def __init__(
        self,
        name: str,
        digits: int = 5,
        point: float = 0.00001,
        visible: bool = True,
        **extra: Any,
    ) -> None:
        self.name = name
        self.digits = int(digits)
        self.point = float(point)
        self.visible = bool(visible)
        for k, v in extra.items():
            setattr(self, k, v)


class FakeSymbolInfoTrade:
    """`symbol_info_trade()`: los campos `SYMBOL_TRADE_*` de los paquetes nuevos."""

    def __init__(
        self,
        symbol: str,
        trade_contract_size: float = 100000.0,
        trade_tick_size: float = 0.00001,
        trade_tick_value: float = 1.0,
        volume_min: float = 0.01,
        volume_max: float = 100.0,
        volume_step: float = 0.01,
        filling_mode: int = 1,
        trade_mode: int = 4,
    ) -> None:
        self.symbol = symbol
        self.trade_contract_size = float(trade_contract_size)
        self.trade_tick_size = float(trade_tick_size)
        self.trade_tick_value = float(trade_tick_value)
        self.volume_min = float(volume_min)
        self.volume_max = float(volume_max)
        self.volume_step = float(volume_step)
        # `filling_mode` es la MASCARA (bit 1=FOK, bit 2=IOC), no el enum. El
        # default 1 es lo que publica EURUSD en MetaQuotes-Demo: solo FOK. Poner
        # 3 (FOK+IOC) haría que todos los tests de envío mapearan el camino del
        # ciclo IOC, que es justo el que no ocurre en EURUSD.
        self.filling_mode = int(filling_mode)
        self.trade_mode = int(trade_mode)


class FakePosition:
    """Posición abierta de MT5 (`positions_get` devuelve estos objetos)."""

    def __init__(
        self,
        ticket: int,
        symbol: str,
        type: int = POSITION_TYPE_BUY,
        volume: float = 0.1,
        price_open: float = 0.0,
        price_current: float = 0.0,
        sl: float = 0.0,
        tp: float = 0.0,
        profit: float = 0.0,
        magic: int = 0,
        comment: str = "",
    ) -> None:
        self.ticket = int(ticket)
        self.symbol = str(symbol)
        self.type = int(type)
        self.volume = float(volume)
        self.price_open = float(price_open)
        self.price_current = float(price_current)
        self.sl = float(sl)
        self.tp = float(tp)
        self.profit = float(profit)
        self.swap = 0.0
        self.magic = int(magic)
        self.comment = str(comment)
        self.time = 0


class FakeTradeResult:
    """`MqlTradeResult` de `order_send`.

    Es un objeto con ATRIBUTOS, no un dict, y sus atributos existen SIEMPRE
    aunque el envío haya fallado. Por eso el adaptador los lee con `getattr` y no
    con `result["retcode"]`: un dict de test daría verde a un acceso que en el
    bróker revienta.

    `order_send` devuelve `None` cuando la llamada ni se procesó: eso es ausencia
    de respuesta, no un rechazo con un motivo.
    """

    def __init__(
        self,
        retcode: int = 0,
        deal: Optional[int] = None,
        order: Optional[int] = None,
        price: float = 0.0,
        comment: str = "",
    ) -> None:
        self.retcode = int(retcode)
        self.deal = deal
        self.order = order
        self.price = float(price)
        self.comment = str(comment)


class FakeMT5(types.ModuleType):
    """Módulo MT5 falso. Se instancia y se registra en `sys.modules`."""

    def __init__(self) -> None:
        super().__init__("MetaTrader5")
        self.TIMEFRAME_M1 = TIMEFRAME_M1
        self.TIMEFRAME_M5 = TIMEFRAME_M5
        self.TIMEFRAME_M15 = TIMEFRAME_M15
        self.TIMEFRAME_M30 = TIMEFRAME_M30
        self.TIMEFRAME_H1 = TIMEFRAME_H1
        self.TIMEFRAME_H4 = TIMEFRAME_H4
        self.TIMEFRAME_D1 = TIMEFRAME_D1
        self.TIMEFRAME_W1 = TIMEFRAME_W1

        # Constantes del enum de ejecución, con los valores REALES del paquete
        # (verificados contra el bróker: ORDER_FILLING_FOK=0, no 1). El puerto
        # las lleva como literales para importarse sin el paquete, y el test
        # compara ambos juegos para que la divergencia no pueda colarse.
        self.ORDER_TYPE_BUY = ORDER_TYPE_BUY
        self.ORDER_TYPE_SELL = ORDER_TYPE_SELL
        self.TRADE_ACTION_DEAL = TRADE_ACTION_DEAL
        self.TRADE_ACTION_SLTP = TRADE_ACTION_SLTP
        self.ORDER_TIME_GTC = ORDER_TIME_GTC
        self.ORDER_FILLING_FOK = ORDER_FILLING_FOK
        self.ORDER_FILLING_IOC = ORDER_FILLING_IOC
        self.TRADE_RETCODE_DONE = TRADE_RETCODE_DONE
        self.TRADE_RETCODE_PLACED = TRADE_RETCODE_PLACED
        self.TRADE_RETCODE_INVALID_FILL = TRADE_RETCODE_INVALID_FILL

        # Estado: lo que el test configura.
        self.initialize_ok = True
        self.initialize_kwargs: Dict[str, Any] = {}
        self.last_error_value = 0
        self.symbols_list: List[Any] = []
        self.info_by_symbol: Dict[str, Any] = {}
        self.trade_by_symbol: Dict[str, Any] = {}
        self.rates_by_key: Dict[Any, List[Any]] = {}
        self.tick_by_symbol: Dict[str, Any] = {}
        self.terminal_common_path = "/fake/common"
        self.account: Any = types.SimpleNamespace(
            login=1234, balance=10000.0, equity=10000.0,
            margin=0.0, margin_free=10000.0, profit=0.0,
            currency="USD", leverage=100,
        )

        # Posiciones abiertas que devuelve `positions_get`.
        self.positions: List[Any] = []

        # -- ejecución --------------------------------------------------------
        #: Qué devuelve `order_send`. None = el bróker no contesta (un hecho, no
        #: un rechazo). Lista = una respuesta por intento, para poder programar
        #: "FOK falla con 10030 y luego IOC funciona".
        self.order_send_results: Any = None
        #: Peticiones recibidas, en orden, tal cual se mandaron.
        self.orders_sent: List[Dict[str, Any]] = []
        #: Valor de `order_calc_profit`. None = el bróker no lo publica.
        self.calc_profit_value: Optional[float] = None
        self.calc_profit_calls: List[tuple] = []

        # Registro de llamadas: para afirmar QUÉ se pidió y con qué args.
        self.calls: List[tuple] = []
        self.initialized = False
        self.shutdown_count = 0
        # Concurrencia: los counters que un lock único mal puesto corrompía.
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0
        self.shutdown_while_busy = False

    # -- instalación -----------------------------------------------------------

    @classmethod
    def install(cls, monkeypatch, **kwargs: Any) -> "FakeMT5":
        """Crea el doble y lo pone en `sys.modules`. Devuélvelo para configurarlo."""
        fake = cls()
        for k, v in kwargs.items():
            setattr(fake, k, v)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        return fake

    # -- ciclo de vida ---------------------------------------------------------

    def initialize(self, path: Optional[str] = None, **kwargs: Any) -> bool:
        """El MT5 real acepta además `login`, `password`, `server`, `timeout`…

        El `.env` real los aporta, así que el doble tiene que tolerarlos: un
        `TypeError` aquí se colaría como un fallo raro del adaptador en vez de
        como lo que es (un doble incompleto).
        """
        self.calls.append(("initialize", path))
        self.initialize_kwargs = dict(kwargs)
        self.initialized = bool(self.initialize_ok)
        return self.initialized

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))
        self.shutdown_count += 1
        # El bug que motivó la sesión única: un shutdown con una llamada en
        # vuelo. El doble NO lanza; lo registra, para que el test pueda
        # afirmar que ya no ocurre.
        if self.concurrent_calls > 0:
            self.shutdown_while_busy = True
        self.initialized = False

    def last_error(self) -> int:
        return int(self.last_error_value)

    # -- información -----------------------------------------------------------

    def terminal_info(self):
        self.calls.append(("terminal_info",))
        if not self.initialized:
            return None
        return types.SimpleNamespace(common_data_path=self.terminal_common_path)

    def account_info(self):
        self.calls.append(("account_info",))
        if not self.initialized:
            return None
        return self.account

    def symbols_get(self, group: Optional[str] = None):
        self.calls.append(("symbols_get", group))
        if group is None or group == "*":
            return tuple(self.symbols_list)
        # El grupo de la API real es un patrón de Market Watch: un nombre exacto o
        # un glob con `*`. NO es sintaxis de MQL5, así que `\*` no coincide con nada
        # y la llamada devuelve una lista vacía SIN error. El doble respeta el
        # grupo para que un `\*` equivocado en el adaptador falle aquí en vez de
        # dar un `/api/symbols` vacío en el bróker real.
        import fnmatch

        return tuple(s for s in self.symbols_list if fnmatch.fnmatchcase(s, group))

    def symbol_select(self, symbol: str, visible: bool = True) -> bool:
        self.calls.append(("symbol_select", symbol, visible))
        return symbol in self.info_by_symbol or symbol in self.tick_by_symbol

    def symbol_info(self, symbol: str):
        self.calls.append(("symbol_info", symbol))
        return self.info_by_symbol.get(symbol)

    def symbol_info_trade(self, symbol: str):
        self.calls.append(("symbol_info_trade", symbol))
        return self.trade_by_symbol.get(symbol)

    def symbol_info_tick(self, symbol: str):
        self.calls.append(("symbol_info_tick", symbol))
        return self.tick_by_symbol.get(symbol)

    # -- ejecución -------------------------------------------------------------

    def order_send(self, request: Any):
        """`order_send`: registra la petición y devuelve lo programado.

        `order_send_results` puede ser `None` (el bróker no contesta), un único
        `FakeTradeResult` (misma respuesta siempre) o una lista (una por intento,
        la última se repite). El caso de la lista es el que permite programar
        "FOK falla con 10030, luego IOC funciona".
        """
        self.calls.append(("order_send", dict(request or {})))
        self.orders_sent.append(dict(request or {}))
        programado = self.order_send_results
        if isinstance(programado, list):
            if not programado:
                return None
            if len(self.orders_sent) <= len(programado):
                return programado[len(self.orders_sent) - 1]
            return programado[-1]
        return programado

    def order_calc_profit(self, symbol: str, volume: float, entry: float,
                          exit_price: float):
        self.calls.append(("order_calc_profit", symbol, volume, entry, exit_price))
        self.calc_profit_calls.append((symbol, volume, entry, exit_price))
        return self.calc_profit_value

    # -- posiciones ------------------------------------------------------------

    def positions_get(self, symbol: Optional[str] = None, ticket: Optional[int] = None):
        """`positions_get` por ticket y por símbolo, como el real.

        Por TICKET no acepta símbolo: el bróker ignora el resto de filtros cuando
        se pasa `ticket`.
        """
        self.calls.append(("positions_get", symbol, ticket))
        if ticket is not None:
            return tuple(p for p in self.positions if int(p.ticket) == int(ticket))
        if symbol is not None:
            return tuple(p for p in self.positions if p.symbol == symbol)
        return tuple(self.positions)

    # -- precios ---------------------------------------------------------------

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int):
        self.calls.append(("copy_rates_from_pos", symbol, timeframe, start, count))
        return self.rates_by_key.get((symbol, timeframe))

    def copy_rates_range(self, symbol: str, frm: int, to: int, timeframe: int):
        self.calls.append(("copy_rates_range", symbol, frm, to, timeframe))
        return self.rates_by_key.get((symbol, timeframe))

    # -- configuración desde el test -------------------------------------------

    def set_rates(
        self,
        symbol: str,
        timeframe: int,
        rows: Sequence[Sequence[Any]],
        as_numpy: bool = False,
    ) -> None:
        """Qué debe devolver `copy_rates_from_pos` para (symbol, timeframe).

        `as_numpy=True` devuelve un array numpy, que es lo que hace el paquete
        real. Por defecto son listas, y esa diferencia no es menor: la verdad de
        un numpy array de más de un elemento lanza `ValueError`, así que un doble
        que devuelve listas deja en verde un `rows or []` que en el bróker real
        tumba TODA lectura de velas.
        """
        filas = list(rows)
        self.rates_by_key[(symbol, timeframe)] = _como_array(filas) if as_numpy else filas

    def calls_named(self, name: str) -> List[tuple]:
        """Llamadas registradas con ese nombre."""
        return [c for c in self.calls if c and c[0] == name]

    def called(self, name: str) -> bool:
        return bool(self.calls_named(name))


def _como_array(filas: Sequence[Sequence[Any]]) -> Any:
    """Filas posicionales -> array numpy 2D, como el que devuelve el paquete.

    Se indexa por posición igual que una tupla (`row[0]` es el tiempo), que es lo
    único que necesita `normalize_candle()`. Se importa aquí y no arriba para que
    este módulo siga importándose en un entorno sin numpy.
    """
    import numpy

    return numpy.array([tuple(f) for f in filas], dtype=float)


def rates_fixture(
    count: int = 5, start_ts: int = 1_700_000_000, base: float = 1.1000, step: float = 0.0001
) -> List[tuple]:
    """Filas de `copy_rates_from_pos` con la forma POSICIONAL de MT5.

    MT5 devuelve namedtuples de 8 campos, no de 6: los dos últimos son `spread`
    y `real_volume`. Se devuelven tuplas de 8 para que el test se parezca a lo que
    el bróker da de verdad, y para que un adaptador que leyera más allá del
    volumen fallara aquí en vez de en producción.
    """
    out = []
    for i in range(count):
        price = base + i * step
        out.append(
            (
                start_ts + i * 900,
                price,
                price + step,
                price - step,
                price + step / 2,
                100 + i,  # tick_volume
                10,  # spread
                100 + i,  # real_volume
            )
        )
    return out

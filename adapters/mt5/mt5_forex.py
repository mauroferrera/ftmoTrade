"""Adaptador de MT5 para lectura de mercado y cuenta: la ÚNICA puerta al terminal.

El bug que este módulo arregla
------------------------------
Trinity tenía TRES puertas distintas a MT5 (patterns, cvd, research), cada una
con su propio lock y su propio `initialize()`/`shutdown()`. El paquete MT5 no es
reentrante: con dos puertas, el `shutdown()` de una podía desenchufar la conexión
mientras otra estaba a mitad de `copy_rates_from_pos()`. El síntoma es
intermitente: un `copy_rates` que devuelve `None` sin error.

Aquí hay una sola puerta: un `Session` con un executor de un hilo, un lock, y un
contador de referencias. Y la sesión se abre una vez y se reutiliza (REF abría y
cerraba la terminal en CADA llamada: ~100 ms de reconnect por petición).

Alcance (M1)
------------
Solo LECTURA: velas, precio, specs, símbolos, posiciones y cuenta. El envío de
órdenes (`order_send`) vive en otro módulo que llega en M5; un objeto que puede
leer y escribir es un objeto al que se le puede pedir lo segundo creyendo que se
pide lo primero.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional, TypeVar

from adapters.base_adapter import (
    AdapterError,
    SymbolNotFound,
    SymbolSpec,
    TerminalUnavailable,
    TIMEFRAMES,
    filas_o_vacias,
    normalize_ohlc,
    resolve_timeframe,
)

T = TypeVar("T")

# MT5 se importa DENTRO de la sesión, no arriba del todo. Dos razones: (1)
# importar el módulo no debe abrir la terminal; (2) el paquete tiene un import
# nativo que falla en máquinas sin la terminal, y eso no debe romper un
# `pytest` entero de recolección.
DEFAULT_TIMEOUT = 30.0
MAX_BARS = 1000  # tope duro del bróker para copy_rates_from_pos

POSITION_TYPE_BUY = 0


class _MT5Unavailable:
    """Sustituto de `MetaTrader5` cuando el paquete no está instalado.

    Existe para que `import adapters.mt5.mt5_forex` NUNCA falle. Los métodos
    lanzan `TerminalUnavailable`, que es la respuesta honesta: "aquí no hay
    terminal", no "el código está roto".
    """

    __unavailable__ = True

    def __getattr__(self, name: str) -> Any:
        def _raise(*_a: Any, **_kw: Any) -> Any:
            raise TerminalUnavailable(
                "El paquete MetaTrader5 no está instalado en este entorno. "
                "Instalalo con: pip install MetaTrader5"
            )

        return _raise


def _import_mt5():
    """Devuelve el módulo MT5 o el sustituto que lanza al usarlo."""
    try:
        import MetaTrader5 as mt5  # noqa: PLC0415 - perezoso a propósito

        return mt5
    except ImportError:
        return _MT5Unavailable()


class Session:
    """Única sesión de MT5 del proceso. Serializable, y con una sola puerta.

    El executor tiene UN hilo porque el paquete MT5 no es reentrante. No es una
    decisión de rendimiento: es la condición para que funcione. Con dos hilos, un
    `copy_rates` y un `order_send` a la vez se corrompen de formas que el bróker
    no reporta.

    El `thread_name_prefix` es "mt5" a secas, sin sufijo, a propósito. Si algún
    día hay un segundo hilo que hable con MT5, este nombre es lo primero que hay
    que mirar.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self._lock = threading.Lock()
        self._timeout = float(timeout)
        self._opened = False
        self._in_flight = 0  # llamadas dentro del trabajo de MT5
        self._inflight_cv = threading.Condition()
        self._mt5: Any = None

    # -- conexión ---------------------------------------------------------------

    def _ensure_open(self) -> Any:
        """Abre la terminal si hace falta. Devuelve el módulo MT5.

        Se espera al lock: abrir la sesión desde dos hilos a la vez sería llamar
        dos veces a `initialize()`, y conectar con una terminal ya abierta es
        comportamiento indefinido según la versión del paquete.
        """
        with self._lock:
            if self._mt5 is not None:
                return self._mt5
            mt5 = _import_mt5()

            # Parámetros opcionales desde .env
            init_kwargs = {}
            path = os.getenv("MT5_PATH")
            login = os.getenv("MT5_LOGIN")
            password = os.getenv("MT5_PASSWORD")
            server = os.getenv("MT5_SERVER")
            if path:
                init_kwargs["path"] = path
            if login and password:
                try:
                    init_kwargs["login"] = int(login)
                except ValueError:
                    pass
                init_kwargs["password"] = password
                if server:
                    init_kwargs["server"] = server

            ok = False
            if init_kwargs:
                ok = bool(mt5.initialize(**init_kwargs))
            if not ok:
                ok = bool(mt5.initialize(path=path) if path else mt5.initialize())

            if not ok:
                error = mt5.last_error()
                mt5.shutdown()
                raise TerminalUnavailable(
                    "No hay conexión con MetaTrader 5. "
                    "Asegúrate de que la terminal esté abierta y con sesión "
                    f"iniciada. (last_error={error})"
                )
            self._mt5 = mt5
            self._opened = True
            return mt5

    def _dispatch(self, fn: Callable[[Any], T]) -> T:
        """El trabajo real: se ejecuta SIEMPRE en el único hilo del executor."""
        mt5 = self._ensure_open()
        with self._inflight_cv:
            self._in_flight += 1
        try:
            return fn(mt5)
        finally:
            with self._inflight_cv:
                self._in_flight -= 1
                if self._in_flight == 0:
                    self._inflight_cv.notify_all()

    def call(self, fn: Callable[..., T]) -> T:
        """Ejecuta `fn(mt5)` en el único hilo de MT5 y devuelve el resultado.

        Este es EL punto de entrada. Cualquier acceso a MT5 pasa por aquí, y por
        eso no puede haber un segundo camino: si hubiera, la sesión no sería una
        sesión.

        El trabajo va al executor de UN hilo en vez de ejecutarse en el hilo
        llamador. Motivo aparte del rendimiento: `close()` necesita poder ESPERAR
        a que no quede ninguna llamada en vuelo, y eso solo es posible si todas
        pasan por el mismo hilo y se pueden contar.

        El `shutdown()` NO está en un `finally`: cerrar la terminal al terminar
        cada llamada obligaba a reconectarla en la siguiente, y MT5 tarda ~100 ms
        en conectar.

        El timeout se traduce a `AdapterError` y no se deja escapar como
        `TimeoutError`: con la terminal cerrada, `initialize()` puede bloquear
        hasta el timeout, y un `TimeoutError` crudo llegaría a FastAPI como 500
        en vez de 503 ("el terminal no responde").
        """
        try:
            return self._executor.submit(self._dispatch, fn).result(timeout=self._timeout)
        except FuturesTimeoutError as exc:
            raise AdapterError(
                f"La llamada a MT5 excedió {self._timeout}s. "
                "Si el terminal está realmente colgado, reinícialo."
            ) from exc

    def run_async(self, fn: Callable[..., T]) -> Any:
        """Como `call`, pero devuelve el `Future` para no bloquear el llamador."""
        return self._executor.submit(self._dispatch, fn)

    def wait(self, future: Any, timeout: Optional[float] = None) -> Any:
        """Espera un `Future` de `run_async` y traduce el error de timeout."""
        try:
            return future.result(timeout=timeout or self._timeout)
        except FuturesTimeoutError as exc:
            raise AdapterError(
                f"La llamada a MT5 excedió {timeout or self._timeout}s. "
                "Si el terminal está realmente colgado, reinícialo."
            ) from exc

    @property
    def is_open(self) -> bool:
        return self._opened

    def close(self) -> None:
        """Cierra la terminal. Idempotente.

        Espera a que no quede ninguna llamada en vuelo. Cerrar mientras un
        `copy_rates` está dentro es exactamente el bug que motivó este módulo.
        """
        with self._inflight_cv:
            while self._in_flight:
                # El hilo del executor es el único que puede bajar el contador,
                # así que si se espera aquí sin soltar la CV no se desbloquea
                # nunca (y `shutdown(wait=True)` esperaría este lock).
                self._inflight_cv.wait(timeout=self._timeout)
        with self._lock:
            if self._mt5 is None:
                return
            self._mt5.shutdown()
            self._mt5 = None
            self._opened = False

    def shutdown_executor(self) -> None:
        """Cierra la sesión Y el hilo del executor. Para tests y apagados."""
        self.close()
        self._executor.shutdown(wait=True)


# La sesión del proceso. Una sola, como el requisito del paquete MT5.
_SESSION: Optional[Session] = None
_SESSION_LOCK = threading.Lock()


def get_session() -> Session:
    """La sesión compartida. El primer llamador la crea; el resto la reusa."""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = Session()
    return _SESSION


def set_session(session: Optional[Session]) -> Optional[Session]:
    """Sustituye la sesión (tests). Devuelve la anterior."""
    global _SESSION
    previous = _SESSION
    _SESSION = session
    return previous


class MT5ForexAdapter:
    """Lectura de mercado y cuenta vía MetaTrader 5.

    Implementa `base_adapter.MarketDataAdapter` y añade lo que la cuenta necesita
    (posiciones, account_info), todo por lectura. Lo que devuelve está ya
    normalizado: velas en dicts con `time` en segundos epoch.

    Lo que NO hace, a propósito:

    - No inventa specs. Si el bróker no publica el símbolo, `spec()` devuelve
      None y quien llama decide. Un spec inventado produce un cálculo de riesgo
      plausible y equivocado, que es peor que un error.
    - No normaliza el símbolo que envía al bróker. `normalize_symbol()` existe
      para COMPARAR; enviar 'EURUSD' a un bróker que publica 'EURUSD.a' da
      `SymbolNotFound`, y la respuesta útil es decir "no está en tu bróker".
    - No manda órdenes. Eso es M5 y otro módulo.
    """

    name = "mt5"

    def __init__(self, session: Optional[Session] = None) -> None:
        self._session = session or get_session()

    @property
    def session(self) -> Session:
        return self._session

    # -- lectura de mercado ------------------------------------------------------

    def ohlc(
        self, symbol: str, timeframe: str = "M15", bars: int = 300
    ) -> List[Dict[str, Any]]:
        """Velas de más antigua a más reciente, con `time` en segundos epoch.

        `bars` se acota a `MAX_BARS` porque el bróker ignora lo que le pases de
        más: pedir 5000 no da error, da 1000, y un modelo que cree que pidió
        5000 no sabe por qué la serie le sale corta.
        """
        tf = resolve_timeframe(timeframe)
        simbolo = str(symbol or "").strip().upper()
        if not simbolo:
            raise ValueError("el símbolo no puede estar vacío")
        n = max(10, min(int(bars or 0), MAX_BARS))

        def _get(mt5) -> List[Dict[str, Any]]:
            if not mt5.symbol_select(simbolo, True):
                raise SymbolNotFound(
                    f"El bróker no publica {simbolo} en Market Watch. "
                    "Comprueba el nombre exacto (puede llevar sufijo como "
                    "'.a' o '_m') y que esté en la lista de símbolos."
                )
            rows = mt5.copy_rates_from_pos(simbolo, tf, 0, n)
            if rows is None:
                return []
            return normalize_ohlc(rows)

        return self._session.call(_get)

    def ohlc_with_daily(self, symbol: str, timeframe: str = "M15", bars: int = 300):
        """`(velas, velas_D1)`: las intradía y las diarias para PDH/PDL.

        Las diarias vienen en la misma llamada porque el patrón SMC necesita el
        High/Low del día anterior. Pedirlas por separado serían dos viajes al
        terminal para leer el mismo estado, y entre uno y otro el día puede
        cambiar: la vela D1 devuelta en la segunda llamada puede ser ya la de
        hoy, no la de ayer.
        """
        tf = resolve_timeframe(timeframe)
        simbolo = str(symbol or "").strip().upper()
        n = max(10, min(int(bars or 0), MAX_BARS))
        d1 = TIMEFRAMES["D1"]

        def _get(mt5):
            if not mt5.symbol_select(simbolo, True):
                raise SymbolNotFound(f"El bróker no publica {simbolo}.")
            intraday = filas_o_vacias(mt5.copy_rates_from_pos(simbolo, tf, 0, n))
            daily = filas_o_vacias(mt5.copy_rates_from_pos(simbolo, d1, 0, 3))
            return normalize_ohlc(intraday), normalize_ohlc(daily)

        return self._session.call(_get)

    def bars(self, symbol: str, timeframe: str = "M15", count: int = 1) -> List[Dict[str, Any]]:
        """Alias de `ohlc` con el nombre que usa el resto del sistema."""
        return self.ohlc(symbol, timeframe, count)

    def price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Bid/ask/último del tick actual. None si el bróker no lo publica."""
        simbolo = str(symbol or "").strip().upper()

        def _get(mt5) -> Optional[Dict[str, Any]]:
            t = mt5.symbol_info_tick(simbolo)
            if t is None:
                return None
            return {
                "bid": float(t.bid or 0.0),
                "ask": float(t.ask or 0.0),
                "last": float(t.last or 0.0),
                "time": int(t.time or 0),
            }

        return self._session.call(_get)

    def symbols(self) -> List[str]:
        """Símbolos disponibles ahora mismo.

        `symbols_get()` devuelve TODOS los del bróker (miles). Se filtran los
        que no están visibles: para un símbolo con `visible=False`, `symbol_info`
        devuelve None, así que ofrecerlo sería ofrecer algo que no funciona.

        El patrón del grupo es `"*" y NO `"\\*\\*\\*"`: los escapes con barra
        invertida son sintaxis de MQL5 y en la API de Python no coinciden con
        nada, así que `symbols_get` devuelve una lista vacía sin error. El
        síntoma en Trinity era `/api/symbols` con `[]` mientras el bróker
        publicaba 12.335 símbolos.
        """

        def _get(mt5) -> List[str]:
            grupo = mt5.symbols_get(group="*") or ()
            return sorted(s.name for s in grupo if getattr(s, "visible", False))

        return self._session.call(_get)

    # -- specs -------------------------------------------------------------------

    def spec(self, symbol: str) -> Optional[SymbolSpec]:
        """Spec del símbolo, o None si el bróker no lo publica."""
        simbolo = str(symbol or "").strip().upper()

        def _get(mt5) -> Optional[SymbolSpec]:
            info = mt5.symbol_info(simbolo)
            if info is None:
                return None
            return self._build_spec(simbolo, info, _safe_trade(mt5, simbolo))

        return self._session.call(_get)

    @staticmethod
    def _build_spec(
        simbolo: str, info: Any, trade: Any = None
    ) -> SymbolSpec:
        """`(symbol_info, symbol_info_trade)` -> `SymbolSpec`.

        Los campos `SYMBOL_TRADE_*` viven en `symbol_info_trade` en las versiones
        modernas del paquete y en `symbol_info` en las antiguas. Se leen de los
        dos sitios, con el moderno primero, y un `None` explícito cuenta como
        "no publicado": cae al default en vez de devolver `None`, porque un
        `int(None)` reventaría al consumidor con un error que no señala el origen.
        """

        def campo(nombre: str, default: Any = None) -> Any:
            for fuente in (trade, info):
                if fuente is None:
                    continue
                v = getattr(fuente, nombre, None)
                if v is not None:
                    return v
            return default

        return SymbolSpec(
            symbol=simbolo,
            digits=int(campo("digits", 5) or 5),
            point=float(campo("point", 0.0) or 0.0),
            contract_size=float(campo("trade_contract_size", 0.0) or 0.0),
            tick_size=float(campo("trade_tick_size", 0.0) or 0.0),
            tick_value=float(campo("trade_tick_value", 0.0) or 0.0),
            volume_min=float(campo("volume_min", 0.01) or 0.01),
            volume_max=float(campo("volume_max", 100.0) or 0.0) or 100.0,
            volume_step=float(campo("volume_step", 0.01) or 0.01),
        )

    # -- cuenta (lectura) ---------------------------------------------------------

    def positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Posiciones abiertas, normalizadas. Vacío si no hay ninguna.

        `[]` es una respuesta legítima: no tener posiciones abiertas no es un
        error. Un `None` del bróker (terminal caída) propaga la excepción.
        """
        simbolo = str(symbol or "").strip().upper() if symbol else None

        def _get(mt5) -> List[Dict[str, Any]]:
            crudas = (
                mt5.positions_get(symbol=simbolo) if simbolo else mt5.positions_get()
            )
            return [_fila_posicion(p) for p in filas_o_vacias(crudas)]

        return self._session.call(_get)

    def account_info(self) -> Dict[str, Any]:
        """Balance, equity, flotante y margen. Lanza si la terminal no responde.

        El `account_info` de MT5 devuelve None con la terminal caída, y un
        `getattr(None, ...)` daría un `AttributeError` sin origen. Aquí se
        traduce a `TerminalUnavailable`, que es lo que significa.
        """

        def _get(mt5) -> Dict[str, Any]:
            info = mt5.account_info()
            if info is None:
                raise TerminalUnavailable(
                    "MetaTrader 5 no devuelve la cuenta: la terminal no está "
                    "conectada."
                )
            return {
                "login": int(getattr(info, "login", 0) or 0),
                "balance": float(getattr(info, "balance", 0.0) or 0.0),
                "equity": float(getattr(info, "equity", 0.0) or 0.0),
                "margin": float(getattr(info, "margin", 0.0) or 0.0),
                "margin_free": float(getattr(info, "margin_free", 0.0) or 0.0),
                "profit": float(getattr(info, "profit", 0.0) or 0.0),
                "currency": str(getattr(info, "currency", "") or ""),
                "leverage": int(getattr(info, "leverage", 0) or 0),
            }

        return self._session.call(_get)


def _fila_posicion(pos: Any) -> Dict[str, Any]:
    """Posición de MT5 -> dict normalizado, sin inventar ceros donde no hay dato."""
    return {
        "ticket": int(getattr(pos, "ticket", 0) or 0),
        "symbol": str(getattr(pos, "symbol", "") or ""),
        "type": "BUY" if int(getattr(pos, "type", 0) or 0) == POSITION_TYPE_BUY else "SELL",
        "volume": float(getattr(pos, "volume", 0.0) or 0.0),
        "price_open": float(getattr(pos, "price_open", 0.0) or 0.0),
        "price_current": float(getattr(pos, "price_current", 0.0) or 0.0),
        "sl": float(getattr(pos, "sl", 0.0) or 0.0),
        "tp": float(getattr(pos, "tp", 0.0) or 0.0),
        "profit": float(getattr(pos, "profit", 0.0) or 0.0),
        "swap": float(getattr(pos, "swap", 0.0) or 0.0),
        "magic": int(getattr(pos, "magic", 0) or 0),
        "comment": str(getattr(pos, "comment", "") or ""),
        "time": int(getattr(pos, "time", 0) or 0),
    }


def _safe_trade(mt5: Any, simbolo: str) -> Optional[Any]:
    """`symbol_info_trade` si el paquete la expone; None si no.

    Se traga la excepción a propósito: en un paquete viejo la función no existe,
    y eso no es un fallo del símbolo sino de la versión. El `None` hace que
    `_build_spec` lea los campos de `symbol_info`, que es lo que funcionaba antes.
    """
    fn = getattr(mt5, "symbol_info_trade", None)
    if fn is None:
        return None
    try:
        return fn(simbolo)
    except Exception:  # noqa: BLE001 - versión antigua: se lee de symbol_info
        return None


def adapter(session: Optional[Session] = None) -> MT5ForexAdapter:
    """Fábrica. Permite inyectar una sesión de test sin tocar la global."""
    return MT5ForexAdapter(session)


__all__ = [
    "AdapterError",
    "MT5ForexAdapter",
    "MAX_BARS",
    "Session",
    "SymbolNotFound",
    "TerminalUnavailable",
    "adapter",
    "get_session",
    "set_session",
]

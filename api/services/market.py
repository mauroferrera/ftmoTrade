"""El mercado y la cuenta, leídos por MT5 a través de la sesión del proceso.

Qué es este módulo
------------------
La implementación de la fachada de mercado que consume la API. No calcula nada:
lee el bróker y normaliza la forma. El análisis (SMC, score, niveles) vive en
`core/` y llega en M2; las reglas FTMO en M3.

Por qué NO vive en `api/routes/`
--------------------------------
Los endpoints de Trinity hacían el trabajo dentro del handler: un snapshot de
210 líneas estaba en `app.py` al lado de las rutas. Si eso vive en el handler no
hay forma de probarlo sin levantar FastAPI, y el test acaba comprobando que el
JSON tiene llaves. Aquí cada respuesta es un método que se llama con dos strings.

Las tres diferencias con Trinity que importan
---------------------------------------------
1. **Una sola puerta a MT5.** Todo lo que necesita el terminal pasa por
   `Session`, la del proceso (`adapters/mt5/mt5_forex.py`).

2. **La caché de velas es de 10 s y se puede vaciar.** El score de una vela M15
   no cambia en 10 s, y cada lectura sin caché son viajes a la terminal por
   petición. El reloj de la caché es inyectable para poder probar la caducidad
   sin dormir.

3. **Nada se inventa.** `spec()` devuelve `None` si el bróker no publica el
   símbolo; `price()` devuelve `None` si no hay tick. Quien llama decide si eso
   es un 404 o un 503.

Lo que este módulo NO hace
--------------------------
- **No manda órdenes.** La escritura es otro servicio y otra fase (M5).
- **No calcula niveles ni scores.** Eso es `core/`, puro y testeable sin
  terminal.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from adapters.mt5 import mt5_forex
from core import clock
from core import levels as levels_engine
from core import risk_engine
from core import setup_gate
from core import smc_engine
from core import strategy

DEFAULT_SYMBOL = "EURUSD"
DEFAULT_TIMEFRAME = "M15"
DEFAULT_BARS = 300

#: TTL de la caché de velas. El mismo número y la misma justificación que en
#: Trinity: el gráfico refresca cada pocos segundos y el viaje al terminal cuesta
#: ~100 ms de ida y vuelta por petición.
CACHE_TTL_S = 10.0

#: Tope duro del bróker para `copy_rates_from_pos`. Duplicado del adaptador a
#: propósito: quien formula la petición tiene que poder acotar sin importar el
#: adaptador (un doble de test no lo expone).
MAX_BARS = 1000


def simbolo(valor: Optional[str]) -> str:
    """El símbolo de una query, normalizado: en mayúsculas y sin espacios.

    Vive aquí y no en cada router porque la normalización tiene que ser LA MISMA
    en todas partes. Con dos copias, la que se quede sin `.strip()` acepta
    " eurusd " y devuelve un `SymbolNotFound` con el motivo equivocado, y el
    síntoma parece del bróker.
    """
    return str(valor or DEFAULT_SYMBOL).strip().upper()


class MarketService:
    """El mercado y la cuenta, leídos por MT5.

    Los colaboradores son inyectables porque las tres respuestas que un test
    tiene que poder fijar son "el bróker dice que sí", "el bróker dice que no" y
    "no hay bróker": con un doble de adaptador las tres se prueban sin terminal.
    """

    def __init__(
        self,
        adapter: Optional[Any] = None,
        session: Optional[Any] = None,
        now: Optional[Callable[[], float]] = None,
        cache_ttl_s: float = CACHE_TTL_S,
    ) -> None:
        # El adaptador por defecto se construye PERO NO CONECTA: `MT5ForexAdapter`
        # solo abre la terminal en la primera llamada real. Arrancar la app no
        # debe abrir MT5, y este servicio se instancia en `Runtime.build()`.
        self._adapter = adapter if adapter is not None else mt5_forex.adapter()
        self._session = session if session is not None else self._adapter.session
        self._now = now if now is not None else clock.epoch
        self._ttl = float(cache_ttl_s)
        self._cache: Dict[Tuple[str, str, int], Tuple[float, List[Dict[str, Any]]]] = {}
        self._patron_cache: Dict[Tuple[str, str, int], Tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

    # -- acceso a las piezas (para tests y para el cableado) ---------------------

    @property
    def adapter(self) -> Any:
        return self._adapter

    @property
    def session(self) -> Any:
        return self._session

    def estado(self) -> Dict[str, Any]:
        """Cómo está cableado el mercado, SIN tocar el terminal.

        `is_open` es un flag de la sesión, no una llamada al bróker: leerlo no
        abre nada. `/api/health` lo publica para que un monitor sepa si la
        terminal está conectada sin que el health tenga efectos secundarios.
        """
        return {
            "adaptador": str(getattr(self._adapter, "name", "desconocido")),
            "terminal": "abierto" if getattr(self._session, "is_open", False) else "cerrado",
        }

    # -- cuenta y posiciones ------------------------------------------------------

    def account_info(self) -> Dict[str, Any]:
        """Balance, equity, flotante y margen. Lanza si la terminal no responde."""
        return self._adapter.account_info()

    def positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Posiciones abiertas. `[]` es una respuesta legítima: no tener ninguna."""
        return self._adapter.positions(symbol)

    def position(self, ticket: int) -> Optional[Dict[str, Any]]:
        """UNA posición por ticket, o `None`.

        Se busca en el BRÓKER y no en Python: `positions_get(ticket=...)` es una
        llamada distinta a traer todas, y filtrar la lista en Python está bien
        para 3 posiciones y mal para 300.
        """

        def _get(mt5) -> Optional[Dict[str, Any]]:
            found = mt5.positions_get(ticket=int(ticket)) or []
            if not found:
                return None
            p = found[0]
            return {
                "ticket": int(p.ticket),
                "symbol": str(p.symbol),
                "type": "BUY" if int(p.type) == 0 else "SELL",
                "volume": float(p.volume),
                "price_open": float(p.price_open),
                "price_current": float(p.price_current),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "profit": float(p.profit),
                "swap": float(getattr(p, "swap", 0.0) or 0.0),
                "magic": getattr(p, "magic", None),
                "time": int(getattr(p, "time", 0) or 0),
            }

        return self._session.call(_get)

    # -- precio, velas y specs ----------------------------------------------------

    def price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Bid/Ask del tick. `None` si el bróker no publica el símbolo.

        `None` y no un dict con error: "no hay precio" y "el bróker está caído"
        son hechos distintos y quien llama los trata distinto.
        """
        s = simbolo(symbol)
        datos = self._adapter.price(s)
        if datos is None:
            return None
        return {"symbol": s, **datos}

    def candles(
        self,
        symbol: str,
        timeframe: str = DEFAULT_TIMEFRAME,
        bars: int = DEFAULT_BARS,
    ) -> List[Dict[str, Any]]:
        """Velas normalizadas, de más antigua a más reciente, con caché de 10 s.

        `None` del bróker ya es `[]` en el adaptador (un mercado cerrado es una
        lista vacía, no un error), así que aquí no se distingue nada más: quien
        llama ve una lista y decide si un 404 tiene sentido.
        """
        s = simbolo(symbol)
        tf = str(timeframe or DEFAULT_TIMEFRAME).strip().upper()
        n = max(10, min(int(bars or 0), MAX_BARS))
        clave = (s, tf, n)
        ahora = float(self._now())
        with self._cache_lock:
            hit = self._cache.get(clave)
            if hit is not None and (ahora - hit[0]) < self._ttl:
                return hit[1]
        serie = list(self._adapter.ohlc(s, tf, n))
        with self._cache_lock:
            self._cache[clave] = (ahora, serie)
        return serie

    def pattern_data(
        self,
        symbol: str,
        timeframe: str = DEFAULT_TIMEFRAME,
        bars: int = DEFAULT_BARS,
    ) -> Optional[Dict[str, Any]]:
        """Velas + PDH/PDL + análisis SMC + niveles, con caché de 10 s.

        Es lo que consume el gráfico del copiloto: la serie, las ZONAS
        (FVG/OB/sweeps) y las LÍNEAS (PDH/PDL, swings, redondos). El SMC y los
        niveles se calculan aquí (en `core/`, puro) y no en la ruta, para poder
        probar el ensamblado sin FastAPI.

        `pdh`/`pdl` salen de las diarias ANTERIORES a la de hoy: el máximo/mínimo
        del día en curso cambia con cada tick, y un PDH que se mueve con el precio
        no es un nivel, es una sombra. Por eso se piden 3 diarias y se descarta la
        última (la de hoy, aún en curso).

        `None` cuando el bróker no devuelve velas del símbolo: quien llama decide
        si eso es un 404 o un 503. Un mercado cerrado con histórico es una lista
        válida, no un `None`.
        """
        s = simbolo(symbol)
        tf = str(timeframe or DEFAULT_TIMEFRAME).strip().upper()
        n = max(10, min(int(bars or 0), MAX_BARS))
        clave = (s, tf, n)
        ahora = float(self._now())
        with self._cache_lock:
            hit = self._patron_cache.get(clave)
            if hit is not None and (ahora - hit[0]) < self._ttl:
                return hit[1]

        intraday, daily = self._adapter.ohlc_with_daily(s, tf, n)
        if not intraday:
            return None

        pdh = pdl = None
        pasadas = list(daily or [])[:-1]
        if pasadas:
            pdh = max(float(c["high"]) for c in pasadas)
            pdl = min(float(c["low"]) for c in pasadas)

        analisis = smc_engine.analyze(intraday, pdh, pdl, symbol=s, timeframe=tf)
        analisis["levels"] = levels_engine.build_levels(intraday, pdh, pdl)
        datos = {
            "symbol": s,
            "timeframe": tf,
            "candles": intraday,
            "pdh": pdh,
            "pdl": pdl,
            "analysis": analisis,
        }
        with self._cache_lock:
            self._patron_cache[clave] = (ahora, datos)
        return datos

    def snapshot(
        self,
        symbol: str,
        timeframe: str = DEFAULT_TIMEFRAME,
        bars: int = DEFAULT_BARS,
        cfg: Optional[Dict[str, Any]] = None,
        force_killzone: bool = False,
        now: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """El snapshot COMPLETO de un símbolo: precio, análisis y Setup Score.

        Es la entrada única del gate (`core.setup_gate.evaluate_gate`) y del watcher.
        Ensambla lo que ya saben hacer `pattern_data` (SMC + niveles) y
        `risk_engine.score_directions` (el score de las DOS direcciones), y le añade el
        precio actual leído del tick.

        `cfg` es el YAML CRUDO de estrategia: los resolvers de `core.strategy` aceptan
        la forma anidada y de ahí salen los pesos, las killzones, el umbral y el
        `tp_ratio_r`. No se recalcula nada aquí: el cálculo vive en `core/`.

        `force_killzone` fuerza el reloj del score a un instante dentro de la primera
        ventana, para que una demo/test pueda ver el camino "dentro de sesión" sin
        esperar la hora real. El snapshot marca `killzone_forced=True` cuando lo hizo,
        para que nadie confunda una sesión forzada con la del mercado.

        `None` cuando el bróker no devuelve velas del símbolo: quien llama decide si eso
        es un 404 o un 503.
        """
        datos = self.pattern_data(symbol, timeframe, bars)
        if datos is None:
            return None
        s = datos["symbol"]
        tf = datos["timeframe"]

        precio: Optional[float] = None
        quote = self.price(s)
        if quote:
            bid, ask = quote.get("bid"), quote.get("ask")
            if bid is not None and ask is not None:
                precio = (float(bid) + float(ask)) / 2.0
            elif bid is not None:
                precio = float(bid)
            elif ask is not None:
                precio = float(ask)
        if precio is None:
            velas = datos.get("candles") or []
            if velas:
                precio = float(velas[-1]["close"])

        patterns = (datos.get("analysis") or {}).get("patterns") or {}
        killzones = strategy.killzones_for(cfg)
        weights = strategy.risk_weights_for(cfg)

        velas = datos.get("candles") or []
        last_time = int(velas[-1]["time"]) if velas else None

        momento = now
        forced = False
        if force_killzone:
            momento = setup_gate.killzone_forced_now(killzones, now)
            forced = momento is not None

        return {
            "symbol": s,
            "timeframe": tf,
            "current_price": precio,
            "last_time": last_time,
            "analysis": datos.get("analysis") or {},
            "risk_engine": risk_engine.score_directions(patterns, weights, killzones, momento),
            "killzone_forced": forced,
        }

    def spec(self, symbol: str) -> Optional[Any]:
        """El `SymbolSpec` del bróker, o `None`. Nunca inventado."""
        return self._adapter.spec(simbolo(symbol))

    def point(self, symbol: str) -> Optional[float]:
        """`SYMBOL_POINT` del símbolo, o `None` si no se puede leer.

        Envuelto en su propia función porque el spread es una mejora y no puede
        hacer que el resto falle: sin terminal no hay punto, y sin punto no hay
        spread, pero el precio y las velas siguen siendo válidos.
        """
        try:
            spec = self.spec(symbol)
        except Exception:  # noqa: BLE001 - el punto es un extra, no el dato
            return None
        return float(spec.point) if spec is not None and spec.point else None

    def spread_points(self, symbol: str) -> Optional[float]:
        """El spread en PUNTOS del bróker, o `None` si no hay con qué medirlo.

        Se calcula en puntos y no en pips a propósito: la conversión a pips
        depende de los decimales del símbolo y esa regla vive en `SymbolSpec`
        (`pip`), así que aquí no se reimplementa.
        """
        quote = self.price(symbol)
        point = self.point(symbol)
        if not quote or not point:
            return None
        bid, ask = quote.get("bid"), quote.get("ask")
        if bid is None or ask is None:
            return None
        return round((float(ask) - float(bid)) / float(point), 1)

    # -- símbolos -----------------------------------------------------------------

    def symbols(self) -> List[str]:
        """Los símbolos visibles que publica el bróker, ordenados."""
        return list(self._adapter.symbols())

    # -- caché --------------------------------------------------------------------

    def clear_cache(self, symbol: Optional[str] = None, timeframe: Optional[str] = None) -> None:
        """Vacía la caché de velas y de patrones (entera, o por símbolo/timeframe)."""
        with self._cache_lock:
            self._patron_cache.clear()
            if symbol is None and timeframe is None:
                self._cache.clear()
                return
            for clave in list(self._cache):
                if (symbol is None or clave[0] == simbolo(symbol)) and (
                    timeframe is None or clave[1] == str(timeframe).strip().upper()
                ):
                    self._cache.pop(clave, None)


__all__ = [
    "CACHE_TTL_S",
    "DEFAULT_BARS",
    "DEFAULT_SYMBOL",
    "DEFAULT_TIMEFRAME",
    "MarketService",
    "simbolo",
]

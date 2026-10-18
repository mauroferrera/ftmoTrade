"""Interfaz común de los adaptadores de datos + la forma normalizada que ve `core/`.

Por qué existe
--------------
Cada proveedor habla su idioma: MT5 devuelve namedtuples posicionales, y cada
futura fuente traerá la suya. `core/` no debería saber nada de eso. Este módulo
fija la frontera:

    proveedor  ->  [adaptador]  ->  forma normalizada  ->  core/

La forma normalizada es la que ya consume `core/`, no una invención nueva:

- **Vela OHLC**: `{"time", "open", "high", "low", "close", "volume"}`. Los
  `time` son epoch en SEGUNDOS, no los del bróker: la conversión es trabajo del
  adaptador, no del núcleo.
- **Spec del símbolo**: `SymbolSpec` (ver abajo).

Dos reglas que este módulo hace cumplir por construcción:

1. **`time` en segundos epoch, siempre.** MT5 devuelve las velas en segundos;
   cualquier otra fuente que llegue tendrá que normalizar aquí.
2. **Fallo explícito, nunca un número inventado.** Un `spec` con `tick_value=0`
   significa "el bróker no lo publica". Las funciones de dinero devuelven `None`
   antes que devolver 0.0, porque 0.0 *es* un número y se lee como "no hay
   riesgo" cuando significa "no lo sé".

Sobre `core/` y este módulo
---------------------------
La dependencia va en un solo sentido: `adapters -> core`. Este módulo define
tipos que `core/` puede importar (son estructuras de datos, no dependencias),
pero ningún módulo de `core/` importa nada de aquí. Lo vigila
`tests/unit/test_core_purity.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

# Timeframes en el nombre corto que ya usa el resto del proyecto ("M15", "H1").
# El valor es el entero de MT5, que es lo que espera la API del broker.
MTF_M1 = 1
MTF_M5 = 5
MTF_M15 = 15
MTF_M30 = 30
MTF_H1 = 16385
MTF_H4 = 16388
MTF_D1 = 16408
MTF_W1 = 32769

TIMEFRAMES: Dict[str, int] = {
    "M1": MTF_M1,
    "M5": MTF_M5,
    "M15": MTF_M15,
    "M30": MTF_M30,
    "H1": MTF_H1,
    "H4": MTF_H4,
    "D1": MTF_D1,
    "W1": MTF_W1,
}

# Segundos por timeframe. Necesario para normalizar timestamps: MT5 devuelve
# la hora de APERTURA de la vela, y para saber su duración hay que mirar el
# timeframe, no la vela.
TF_SECONDS: Dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}

# A partir de aquí (inclusive) la convención "pip" de forex aplica: la unidad de
# presentación es el pip y no el punto. Oro cotiza a 2 decimales y se cuenta en
# puntos; llamarlos pips en un panel de riesgo es mentir por la unidad.
PIP_MIN_DIGITS = 3

UNIT_LABEL_PIPS = "pips"
UNIT_LABEL_POINTS = "puntos"


class AdapterError(RuntimeError):
    """Fallo de un proveedor de datos (no puede hablar con el bróker).

    Distinto de un `ValueError` de argumentos: un `ValueError` es un bug del
    llamador (un timeframe que no existe), un `AdapterError` es el mundo
    exterior fallando (terminal cerrada, símbolo no publicado). Se separan
    porque quien llama reacciona distinto: el primero se arregla en código, el
    segundo se le avisa al usuario.
    """


class TerminalUnavailable(AdapterError):
    """El proveedor está bien instalado pero no hay sesión abierta.

    Es el caso de uso normal en desarrollo y en CI: el paquete `MetaTrader5`
    puede no haber terminal. Que sea una excepción propia y NO un `ImportError`
    permite que los tests recorran el camino de "no hay terminal" sin fingir
    que hay uno.
    """


class SymbolNotFound(AdapterError):
    """El bróker no publica ese símbolo en Market Watch.

    No es lo mismo que "no hay datos": el símbolo existe en la plataforma pero
    el bróker no lo ofrece, y la respuesta útil es "no está disponible en tu
    bróker", no un error genérico de red.
    """


@dataclass(frozen=True)
class SymbolSpec:
    """Especificaciones de cotización y contrato de un símbolo.

    `point` es el precio de UN punto (`SYMBOL_POINT`). `digits` son los
    decimales de la cotización.

    Los campos de contrato/lote vienen de `SYMBOL_TRADE_*` y pueden venir a 0
    cuando el bróker no los publica. 0 significa "no lo sé", NO "vale cero":
    por eso las funciones de dinero que los usan devuelven `None` en vez de
    calcular con 0. Un cálculo con 0 daría 0 de riesgo, que es el peor resultado
    posible porque parece una respuesta.
    """

    symbol: str
    digits: int
    point: float
    contract_size: float = 0.0
    tick_size: float = 0.0
    tick_value: float = 0.0
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    # Escape hatch para cotizaciones no estándar (bróker con pip de 5 decimales,
    # CFD exótico). Si es > 0 manda sobre la regla de los dígitos. NUNCA se
    # rellena a ciegas: 0 = usar la regla.
    pip_override: float = 0.0

    # -- unidad de presentación ------------------------------------------------

    @property
    def pip(self) -> float:
        """Precio de UNA unidad: pip en forex, punto en metales e índices.

        La regla es `point * 10 si digits >= 3`. Sale de cómo se cotizan los
        pares de forex: EURUSD cotiza a 5 decimales y un pip es el último
        (0.0001 = point * 10); el yen cotiza a 3 y un pip es 0.01 (también
        point * 10). El oro cotiza a 2, donde point * 10 = 0.1 en vez de 0.01:
        aplicarle la regla de forex multiplicaba el SL por diez, y en un SL de
        12 unidades de oro pasaba de 0.12 a 1.2.
        """
        if self.pip_override and self.pip_override > 0:
            return float(self.pip_override)
        point = float(self.point or 0.0)
        if point <= 0:
            return 0.0
        return point * 10.0 if int(self.digits) >= PIP_MIN_DIGITS else point

    @property
    def pip_quote(self) -> bool:
        """True si la convención forex aplica (y "pips" es un término honesto).

        Un `pip_override` la descarta por definición: existe justamente para los
        símbolos que NO cotizan como un par de forex. Si el bróker publica 3
        decimales y además fija la unidad a mano, llamar "pips" a esa unidad
        deshonesta al símbolo, que es el mismo error que se corrigió para el
        oro: el número del motivo de rechazo tiene que nombrarse como lo nombra
        el mercado.
        """
        if self.pip_override and self.pip_override > 0:
            return False
        return int(self.digits) >= PIP_MIN_DIGITS

    @property
    def unit_label(self) -> str:
        return UNIT_LABEL_PIPS if self.pip_quote else UNIT_LABEL_POINTS

    def to_price(self, units: float) -> float:
        """Unidades de presentación (pips/puntos) -> precio."""
        unit = self.pip
        return float(units) * unit if unit else 0.0

    def from_price(self, distance: float) -> float:
        """Distancia de precio -> unidades de presentación."""
        unit = self.pip
        return float(distance) / unit if unit else 0.0

    def format_units(self, distance: float, decimals: int = 1) -> str:
        """Distancia de precio en la unidad del símbolo ("12.0 pips")."""
        n = self.from_price(distance)
        if n < 0.01:
            # Por debajo de una centésima de unidad, "0.04 pips" es ruido que
            # esconde que el número es una distancia en precio crudo.
            return f"{distance:g} en precio"
        return f"{n:.{decimals}f} {self.unit_label}"

    def format_distance(self, distance: float, decimals: int = 1) -> str:
        """Alias legible de `format_units` para los motivos de rechazo."""
        return self.format_units(distance, decimals)

    def with_pip(self, pip: Optional[float]) -> "SymbolSpec":
        """Copia con `pip_override` cambiado (`None` = volver a la regla)."""
        return replace(self, pip_override=float(pip) if pip else 0.0)

    def normalize_volume(self, volume: float) -> float:
        """Volume ajustado a `[volume_min, volume_max]` y múltiplo de `volume_step`.

        Un bróker rechaza 0.113 lotes, así que el cálculo de tamaño tiene que
        redondear ANTES de enviar la orden, no después.
        """
        v = float(volume or 0.0)
        step = float(self.volume_step or 0.0)
        if step > 0:
            v = round(v / step) * step
        if self.volume_min and v < self.volume_min:
            v = float(self.volume_min)
        if self.volume_max and v > self.volume_max:
            v = float(self.volume_max)
        # El redondeo a 8 decimales quita el 0.010000000000000009 que sale de
        # multiplicar y dividir por 0.01, que un `==` de float del bróker
        # rechazaría aunque el número sea correcto en la pantalla.
        return round(v, 8)

    # -- dinero (None cuando el bróker no publica el dato) --------------------

    def tick_value_for_volume(self, volume: float = 1.0) -> Optional[float]:
        """Valor en dinero de UN tick para `volume` lotes.

        None si el bróker no publica `tick_value`: es imposible calcular riesgo
        sin él y devolver 0.0 haría que una posición sin SL pareciera sin riesgo.
        """
        if not self.tick_value or self.tick_value <= 0:
            return None
        return float(self.tick_value) * float(volume or 0.0)

    def distance_ticks(self, distance: float) -> Optional[float]:
        """Distancia de precio expresada en ticks del bróker.

        None sin `tick_size`. Con `tick_size` publicado, el número de ticks es
        la unidad en la que el bróker valida el SL: un SL a 0.4 del punto se
        rechaza como "demasiado cercano" aunque el cálculo de riesgo cuadre.
        """
        ts = float(self.tick_size or 0.0)
        if ts <= 0:
            return None
        return float(distance) / ts


@dataclass(frozen=True)
class Candle:
    """Vela OHLC normalizada.

    Existe como clase Y como función de conversión (`normalize_candle`) porque
    `core/` trabaja con dicts (los tests construyen dicts a mano) y no tiene
    sentido romper eso. La dataclass fija el contrato; la normalización produce
    dicts porque eso es lo que consume el núcleo.
    """

    time: int  # epoch SEGUNDOS, hora de apertura de la vela
    open: float
    high: float
    low: float
    close: float
    volume: int  # tick_volume en forex; tamaño real en cripto

    def as_dict(self) -> Dict[str, Any]:
        return {
            "time": int(self.time),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": int(self.volume),
        }


def normalize_candle(row: Any) -> Dict[str, Any]:
    """Fila cruda del proveedor -> vela en la forma de `core/`.

    Acepta dos shapes porque hay dos familias de proveedores:

    - MT5 devuelve namedtuples POSICIONALES: `row[0]` es el tiempo, `row[1]` el
      open, etc. El orden está en el C struct del paquete y no cambia.
    - Cualquier fuente futura puede devolver dicts con nombre.

    Se distinguen por `isinstance(row, dict)` y, si no, por indexarse. Es
    frágil por naturaleza; por eso la normalización vive en un solo sitio, en
    vez de repartirse por los adaptadores.
    """
    if isinstance(row, dict):
        return {
            "time": int(row.get("time") or row.get("timestamp") or row.get("t") or 0),
            "open": float(row.get("open", row.get("o", 0.0)) or 0.0),
            "high": float(row.get("high", row.get("h", 0.0)) or 0.0),
            "low": float(row.get("low", row.get("l", 0.0)) or 0.0),
            "close": float(row.get("close", row.get("c", 0.0)) or 0.0),
            "volume": int(row.get("volume", row.get("v", 0)) or 0),
        }
    # Posicional de MT5: (time, open, high, low, close, tick_volume, spread, real_volume)
    return {
        "time": int(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": int(row[5]),
    }


def filas_o_vacias(rows: Any) -> Sequence[Any]:
    """`None` -> lista vacía. Lo demás, tal cual. SIN preguntar por su verdad.

    Existe por una trampa concreta: las funciones `copy_*` de MT5 devuelven un
    array **numpy**, y la verdad de un numpy array de más de un elemento lanza
    `ValueError: The truth value of an array with more than one element is
    ambiguous`. Así que el idiom tan natural `rows or []` -y el igual de común
    `if not rows`- no es "poco elegante": revienta, y solo cuando hay más de una
    fila, que es el caso normal de una lectura de velas.

    El síntoma era que cualquier lectura de velas devolvía `ValueError` en el
    bróker de verdad, con toda la suite en verde, porque el doble de MT5 devuelve
    listas y opcionalmente `None`: para una lista, la verdad es inocua. Un doble
    que devuelve siempre el tipo fácil oculta justo el error que importa.

    Se compara contra `None` y no se convierte aquí a propósito: `normalize_candle`
    indexa las filas posicionalmente y un array se indexa sin problema, así que
    hacer una copia no aporta nada y copia 60 velas en cada lectura.
    """
    return [] if rows is None else rows


def normalize_ohlc(rows: Sequence[Any]) -> List[Dict[str, Any]]:
    """Lista de filas crudas -> lista de velas normalizadas."""
    return [normalize_candle(r) for r in filas_o_vacias(rows)]


@runtime_checkable
class MarketDataAdapter(Protocol):
    """Lo mínimo que `core/` y la API pueden pedirle a cualquier proveedor.

    Es un `Protocol` y no una clase base heredable a propósito: obliga a que la
    forma del método sea la misma sin obligar a que el adaptador herede nada.
    """

    name: str

    def symbols(self) -> List[str]:
        """Símbolos que el proveedor publica ahora mismo."""
        ...

    def ohlc(self, symbol: str, timeframe: str = "M15", bars: int = 300) -> List[Dict[str, Any]]:
        """Velas normalizadas, ordenadas de MÁS ANTIGUA a MÁS RECIENTE.

        El orden importa y no es negociable: cualquier cálculo acumulado (CVD,
        swings, secuencias) produce una curva invertida con la lista al revés.
        """
        ...

    def price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Bid/ask del tick actual. None si el proveedor no lo publica."""
        ...

    def spec(self, symbol: str) -> Optional[SymbolSpec]:
        """Spec del símbolo, o None si el proveedor no lo publica.

        None es la respuesta HONESTA aquí: inventar unos specs por defecto haría
        que el cálculo de riesgo saliera plausible y equivocado.
        """
        ...


def resolve_timeframe(timeframe: str) -> int:
    """Nombre corto ("H1") -> entero del broker.

    Lanza `ValueError` (no `AdapterError`) porque un timeframe mal escrito es un
    bug del llamador, no un fallo del mundo exterior. `AdapterError` está para
    "la terminal no abre"; mezclar los dos hace que el mensaje de un typo diga
    "comprueba que la terminal esté abierta", que no ayuda a nadie.
    """
    tf = TIMEFRAMES.get(str(timeframe or "").strip().upper())
    if tf is None:
        validos = ", ".join(TIMEFRAMES)
        raise ValueError(
            f"timeframe {timeframe!r} no válido. Usa uno de: {validos}."
        )
    return tf


def normalize_symbol(symbol: str) -> str:
    """Símbolo del bróker -> forma canónica para comparar.

    Los sufijos del bróker ('EURUSD.a', 'EURUSD_m', 'XAUUSD.pro') son ruido del
    proveedor, no identidad del instrumento. Comparar 'EURUSD' contra 'EURUSD.a'
    da "símbolo desconocido" y hace que el símbolo configurado en el YAML no
    se encuentre con el que publica el bróker.

    OJO: esto NO cambia el símbolo que se ENVÍA al bróker. Se normaliza solo para
    comparar y para claves de caché. Enviar 'EURUSD' cuando el bróker publica
    'EURUSD.a' da `SymbolNotFound`, y "arreglarlo" quitando el sufijo lo rompe
    del otro modo.
    """
    s = str(symbol or "").strip().upper()
    for sep in (".", "_", "-", "#"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s

"""Adaptadores: la única frontera entre el mundo exterior y el resto del sistema.

Regla de dependencia (la vigila `tests/unit/test_core_purity.py`):
`adapters -> core` está permitido SOLO para tipos (estructuras de datos);
`core/` nunca importa de aquí. La forma normalizada que produce este paquete es
la única que `core/` y la API conocen: dicts de vela con `time` en epoch
SEGUNDOS, `SymbolSpec` con la aritmética de pip resuelta, y excepciones propias
para distinguir "la terminal no abre" de "el bróker no publica ese símbolo".
"""

from adapters.base_adapter import (  # noqa: F401
    AdapterError,
    Candle,
    MarketDataAdapter,
    SymbolNotFound,
    SymbolSpec,
    TerminalUnavailable,
    TF_SECONDS,
    TIMEFRAMES,
    filas_o_vacias,
    normalize_candle,
    normalize_ohlc,
    normalize_symbol,
    resolve_timeframe,
)

__all__ = [
    "AdapterError",
    "Candle",
    "MarketDataAdapter",
    "SymbolNotFound",
    "SymbolSpec",
    "TerminalUnavailable",
    "TF_SECONDS",
    "TIMEFRAMES",
    "filas_o_vacias",
    "normalize_candle",
    "normalize_ohlc",
    "normalize_symbol",
    "resolve_timeframe",
]

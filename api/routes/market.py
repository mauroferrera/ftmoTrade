"""Rutas de mercado: símbolos, precio, velas, spec y cuenta.

Endpoints delgados: validan los parámetros de la query y delegan en
`MarketService`. Cualquier cálculo (spread, normalización, caché) vive en el
servicio, donde se puede probar sin FastAPI.

Los errores del bróker no se traducen aquí: `AdapterError` (terminal caída) y
`SymbolNotFound` (símbolo no publicado) los recoge `api/errors.py` a nivel de
app, igual que `ConfigUnavailable`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from api.services.market import (
    DEFAULT_BARS,
    DEFAULT_SYMBOL,
    DEFAULT_TIMEFRAME,
    MarketService,
)

from .. import deps

router = APIRouter(prefix="/api/market", tags=["mercado"])

#: Tope de velas aceptado por la query. El servicio acota al revés (10..1000);
#: este es el techo que el cliente puede PEDIR, para que un 50000 no llegue ni
#: al servicio.
MAX_BARS_QUERY = 1000


def market(rt=Depends(deps.runtime)) -> MarketService:
    """El servicio de mercado del runtime. 503 si no está cableado."""
    return deps.market_service(rt)


@router.get("/status", response_model=None)
def status(svc: MarketService = Depends(market)) -> Dict[str, Any]:
    """Cómo está cableado el mercado (adaptador + terminal abierta/cerrada).

    No toca el terminal: es un flag de la sesión, para que un monitor pueda
    consultar `/api/market/status` sin abrir MT5.
    """
    return svc.estado()


@router.get("/symbols", response_model=None)
def symbols(svc: MarketService = Depends(market)) -> Dict[str, Any]:
    """Los símbolos visibles del bróker, ordenados."""
    return {"symbols": svc.symbols()}


@router.get("/price", response_model=None)
def price(
    symbol: str = Query(DEFAULT_SYMBOL, min_length=1),
    svc: MarketService = Depends(market),
) -> Dict[str, Any]:
    """Bid/ask del tick. 404 si el bróker no publica el símbolo."""
    datos = svc.price(symbol)
    if datos is None:
        from adapters.base_adapter import SymbolNotFound  # noqa: PLC0415

        raise SymbolNotFound(
            f"Sin cotización para {symbol.upper()}: no está publicado en tu bróker."
        )
    quote = dict(datos)
    quote["spread_points"] = svc.spread_points(symbol)
    return quote


@router.get("/candles", response_model=None)
def candles(
    symbol: str = Query(DEFAULT_SYMBOL, min_length=1),
    timeframe: str = Query(DEFAULT_TIMEFRAME, min_length=1),
    bars: int = Query(DEFAULT_BARS, ge=10, le=MAX_BARS_QUERY),
    svc: MarketService = Depends(market),
) -> Dict[str, Any]:
    """Velas normalizadas, de más antigua a más reciente.

    404 si el símbolo no existe en el bróker. Un mercado cerrado devuelve una
    lista vacía, que NO es un error: es el hecho.
    """
    serie = svc.candles(symbol, timeframe, bars)
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe.upper(),
        "bars": len(serie),
        "candles": serie,
    }


@router.get("/analysis", response_model=None)
def analysis(
    symbol: str = Query(DEFAULT_SYMBOL, min_length=1),
    timeframe: str = Query(DEFAULT_TIMEFRAME, min_length=1),
    bars: int = Query(DEFAULT_BARS, ge=10, le=MAX_BARS_QUERY),
    svc: MarketService = Depends(market),
) -> Dict[str, Any]:
    """Velas + zonas SMC + niveles S/R, listos para pintar en el gráfico.

    404 si el bróker no devuelve velas del símbolo (no existe, o no está en el
    Market Watch). El análisis se calcula en `core/` (puro) y el ensamblado en el
    servicio: aquí solo se valida la query.
    """
    datos = svc.pattern_data(symbol, timeframe, bars)
    if datos is None:
        from adapters.base_adapter import SymbolNotFound  # noqa: PLC0415

        raise SymbolNotFound(
            f"Sin velas para {symbol.upper()}: el bróker no lo publica o está "
            "fuera del Market Watch."
        )
    return datos


@router.get("/spec", response_model=None)
def spec(
    symbol: str = Query(DEFAULT_SYMBOL, min_length=1),
    svc: MarketService = Depends(market),
) -> Dict[str, Any]:
    """Spec del símbolo. 404 si el bróker no lo publica.

    El spec nunca se inventa: un `SymbolSpec` fabricado produce un cálculo de
    riesgo plausible y equivocado, que es peor que un error.
    """
    s = svc.spec(symbol)
    if s is None:
        from adapters.base_adapter import SymbolNotFound  # noqa: PLC0415

        raise SymbolNotFound(
            f"El bróker no publica {symbol.upper()}: sin specs disponibles."
        )
    return {
        "symbol": s.symbol,
        "digits": s.digits,
        "point": s.point,
        "pip": s.pip,
        "unit_label": s.unit_label,
        "contract_size": s.contract_size,
        "tick_size": s.tick_size,
        "tick_value": s.tick_value,
        "volume_min": s.volume_min,
        "volume_max": s.volume_max,
        "volume_step": s.volume_step,
    }


@router.get("/account", response_model=None)
def account(svc: MarketService = Depends(market)) -> Dict[str, Any]:
    """Balance, equity, flotante y margen de la cuenta."""
    return svc.account_info()


@router.get("/positions", response_model=None)
def positions(
    symbol: Optional[str] = Query(None),
    svc: MarketService = Depends(market),
) -> Dict[str, Any]:
    """Posiciones abiertas, todas o filtradas por símbolo."""
    lista: List[Dict[str, Any]] = svc.positions(symbol)
    return {"positions": lista, "count": len(lista)}


__all__ = ["MAX_BARS_QUERY", "router"]

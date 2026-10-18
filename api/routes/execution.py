"""Las rutas de ejecución: lo único que toca dinero, y las puertas que lo rodean.

Tres escrituras y una lectura
-----------------------------
`POST /api/trade/market` abre una posición. `POST /api/positions/{ticket}/close`
la cierra. `POST /api/positions/{ticket}/modify` mueve su stop (y/o su objetivo).
`GET /api/positions` mira lo que hay abierto.

**Las tres escrituras piden token; la lectura no.** No es una comodidad: abrir,
cerrar o mover un stop mueven dinero, y una ruta sin token convierte cualquier
página web en un cliente de la API con la sesión del usuario. Mirar posiciones no
cambia nada, así que exigir token ahí solo impediría pintar el panel.

Dónde vive la lógica y por qué no aquí
--------------------------------------
Ni el dimensionado, ni el gate FTMO, ni la calidad de ejecución viven en este
fichero: los tres son `ExecutionService` (y, debajo, `core/`). Esta capa hace dos
cosas y ninguna más:

1. Elige el servicio del runtime (con su puerto de ejecución inyectable).
2. Traduce el `(cuerpo, status)` que devuelve el servicio a HTTP.

**Por qué el status lo decide el servicio.** El servicio es el MISMO que usa
cualquier camino automático (el watcher, el arranque). Si la ruta validara por su
cuenta, el camino con menos puertas acabaría siendo el automático, que es
exactamente el que menos gente está mirando cuando manda la orden.

Por qué `HTTPException(status, detail=cuerpo)` y no un `JSONResponse`
--------------------------------------------------------------------
El cuerpo del servicio ya trae su propio `status` en el JSON (`"SYMBOL_NOT_ALLOWED"`,
`"LOT_UNAVAILABLE"`, …) y el código HTTP va aparte. `HTTPException` deja el cuerpo
intacto dentro de `detail` y Starlette le pone el código; construir a mano un
`JSONResponse` en cada handler duplicaría el mapa de códigos que el servicio ya
resolvió. El `detail` con un dict es la forma en que el proyecto ya responde 501
en la ruta de auto-ejecución del watcher, así que el frontend lee lo mismo.

Las lecturas (`GET /api/positions`)
-----------------------------------
No piden token, no escriben auditoría y no mandan nada. El servicio devuelve
`(filas, error)`: `error` distingue "no hay puerto de ejecución cableado" de "el
puerto no sabe listar", y aquí solo se traduce a 503 con el motivo. Una lista
vacía NO es un error: es "no hay posiciones".
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import deps
from ..services.execution import DEFAULT_TIMEFRAME, execution_from

router = APIRouter(tags=["execution"])


class TradeMarketBody(BaseModel):
    """Orden de mercado manual (el botón de "operar" del panel).

    `volume` es opcional a propósito: si no viene, el lote se dimensiona por el
    riesgo efectivo del setup, con la MISMA cuenta que usa el resto del sistema. El
    `score` y el `verdict` que llegan aquí son el contexto con el que el panel
    decidió enseñar el botón: se pasan al servicio para que el riesgo efectivo sea
    el mismo que la pantalla mostró, y no uno recalculado en otro sitio.

    `no_tp=True` deja la orden sin objetivo cuando la salida la decide una política
    y no un target congelado. `invalidate_level` y `planned_entry` alimentan la
    comprobación de calidad de ejecución (deriva e invalidación), no el envío.
    """

    symbol: str
    action: str = "BUY"
    volume: Optional[float] = None
    sl_distance: Optional[float] = None
    tp_distance: Optional[float] = None
    no_tp: bool = False
    magic: Optional[int] = None
    comment: Optional[str] = None
    deviation: Optional[int] = None
    score: Optional[float] = None
    verdict: Optional[str] = None
    invalidate_level: Optional[float] = None
    planned_entry: Optional[float] = None
    timeframe: Optional[str] = None


class ModifyStopBody(BaseModel):
    """Lo que la ruta de modificación manda: los niveles nuevos, por separado.

    `sl` y `tp` se aceptan independientes porque un trailing mueve el stop y deja
    el objetivo quieto. `None` significa "no tocar ese nivel": el bróker conserva
    el que ya tiene. Quitar un nivel es `0.0`, que es la forma en que MT5 lo
    entiende — y por eso el servicio rechaza el caso en que los dos sean `None`
    (no habría nada que modificar) sin confundirlo con "quitar los dos".
    """

    sl: Optional[float] = None
    tp: Optional[float] = None


def _servicio(rt: Any) -> Any:
    """El `ExecutionService` para esta petición, con los puertos del runtime.

    Se construye por petición y NO se cachea: a diferencia del watcher, el servicio
    de ejecución no guarda memoria de ciclo, y sus colaboradores (el puerto MT5, el
    `PropService`, el store) viven en el `Runtime`. Construirlo aquí es lo que
    permite inyectar un puerto de ejecución en un test sin tocar la ruta.
    """
    return execution_from(rt)


@router.post("/api/trade/market", response_model=None)
def post_trade_market(
    body: TradeMarketBody,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Abre una posición a mercado, pasando por TODAS las puertas.

    El servicio decide el status (403 si la lista blanca o el FTMO bloquean, 503 si
    no hay mercado o puerto, 400 si el lote o la geometría no valen, 200 si sale).
    Aquí solo se traduce: `status >= 400` se levanta como excepción y el cuerpo del
    servicio viaja íntegro en `detail`.

    `source="manual"` marca la fila de auditoría: la MISMA orden puede llegar desde
    el botón del panel o desde un escaneo automático, y la fila tiene que decir por
    cuál de los dos caminos se pidió.
    """
    cuerpo, status = _servicio(rt).execute_market_trade(
        symbol=body.symbol, action=body.action, volume=body.volume,
        sl_distance=body.sl_distance, tp_distance=body.tp_distance, no_tp=body.no_tp,
        magic=body.magic, comment=body.comment, deviation=body.deviation,
        score=body.score, verdict=body.verdict, invalidate_level=body.invalidate_level,
        planned_entry=body.planned_entry,
        timeframe=body.timeframe or DEFAULT_TIMEFRAME, source="manual",
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


@router.get("/api/positions", response_model=None)
def get_positions(
    magic: Optional[int] = None,
    rt: Any = Depends(deps.runtime),
) -> Dict[str, Any]:
    """Las posiciones abiertas, filtradas por `magic` si se pasa.

    Lectura: sin token, sin auditoría, sin envío. `magic` filtra en la ruta (no en
    la ruta HTTP, en el servicio) para que el panel pueda enseñar solo las
    operaciones del sistema y no las que el operador tenga abiertas a mano.

    Un `error` del servicio es un 503: significa que no se puede contestar (sin
    puerto de ejecución, o un puerto que se calló). Una lista vacía es un 200: "no
    hay posiciones" es una respuesta, no un fallo.
    """
    filas, error = _servicio(rt).positions(magic=magic)
    if error:
        raise HTTPException(status_code=503, detail={"error": error})
    return {"positions": list(filas or [])}


@router.post("/api/positions/{ticket}/close", response_model=None)
def post_close_position(
    ticket: int,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Cierra la posición indicada por ticket.

    El ticket va en la ruta y no en un cuerpo porque es un identificador: se puede
    enlazar y comprobar sin abrir el cuerpo de la petición.

    El cierre NO pasa por la lista blanca ni por el gate FTMO (lo razona el
    servicio): un tope para ABRIR no puede dejar una posición viva sin forma de
    salir. El 404 cuando no hay posición con ese ticket es el caso normal de un
    doble clic, no un fallo del sistema.
    """
    cuerpo, status = _servicio(rt).close_position(ticket=ticket)
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


@router.post("/api/positions/{ticket}/modify", response_model=None)
def post_modify_position(
    ticket: int,
    body: ModifyStopBody,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Mueve el SL/TP de la posición abierta `ticket`.

    Mover un stop no abre riesgo (lo cierra o lo deja igual), así que —igual que el
    cierre— no pasa por las puertas de apertura. La garantía de que el stop no se
    afloja está en el cálculo de salida que decide el nivel (el ratchet "nunca
    afloja"), no aquí: esta ruta manda el nivel que le llega, tal cual.

    Un cuerpo con `sl` y `tp` a `None` es un 400 del servicio ("no hay nada que
    modificar"), que es distinto de `0.0` ("quita el nivel").
    """
    cuerpo, status = _servicio(rt).modify_stop(ticket, sl=body.sl, tp=body.tp)
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


__all__ = ["ModifyStopBody", "TradeMarketBody", "router"]

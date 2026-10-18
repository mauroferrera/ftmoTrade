"""Rutas del watcher: qué se vigila, un escaneo, y por qué no hay auto-ejecución.

Tres rutas, y ninguna ejecuta
-----------------------------
`GET /api/watcher/status` publica la configuración y el estado vigente de cada símbolo.
`POST /api/watcher/scan` corre un ciclo: evalúa, audita en `setup_log`/`setup_state` y
devuelve los eventos. `POST /api/watcher/auto-execute` responde **501** con el motivo.

Por qué el 501 y no un `200` con `{"ok": false}`
-----------------------------------------------
Un frontend que haga `res.ok ? res.json() : reject(...)` con un `200` enseñaría "watcher
auto-ejecutar ACTIVADO" tras encender un interruptor que no encendió nada. El estado del
interruptor sale del `GET /status`, que publica `auto_execute: false` siempre. Un 501 con
el motivo en el cuerpo es la única respuesta que un panel no puede convertir en mentira.

Delgadez y token
----------------
Ni score, ni zonas, ni dedup aquí: eso es `core/setup_gate.py`,
`core/setup_lifecycle.py` y `api/services/watcher.py`. Esta capa solo elige el servicio
del runtime y traduce el botón. `POST /scan` **escribe** en la base, así que pide token;
`GET /status` es una lectura y no lo pide.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import deps
from ..services import watcher as watcher_service

router = APIRouter(tags=["watcher"])


class AutoExecuteBody(BaseModel):
    """Lo que el interruptor del panel manda.

    `enabled` es obligatorio y sin valor por defecto a propósito: un interruptor que se
    enciende sin decir a qué se refiere es un interruptor que alguien activó sin querer;
    que el cuerpo no valide es un `422` y el checkbox se queda como estaba.
    """

    enabled: bool


def _servicio(rt: Any) -> Any:
    """El `WatcherService` de esta app, no uno nuevo por petición.

    El servicio deduplica los descartes en memoria (firma de rechazo por símbolo/timeframe).
    Si la ruta lo construyese en cada `POST /scan`, esa memoria moriría con el `return` y un
    setup por debajo del umbral escribiría una fila de `setup_log` por ciclo, para siempre,
    sin que nada fallara. La instancia vive en el `Runtime`.
    """
    return deps.watcher_service(rt)


@router.get("/api/watcher/status", response_model=None)
def watcher_status(rt: Any = Depends(deps.runtime)) -> Dict[str, Any]:
    """Qué vigila el watcher y en qué estado está cada símbolo.

    Incluye `auto_execute` (siempre `false`), `auto_execute_conf` (lo que pide el YAML) y
    `auto_execute_disponible` (si hay código que lo haga). Los tres juntos son la
    diferencia entre "está apagado" y "no existe": un panel que solo publicara el valor
    del YAML rotularía "AUTO-EJECUCIÓN" sobre un sistema que no manda nada.
    """
    return _servicio(rt).estado()


@router.post("/api/watcher/scan", response_model=None)
def watcher_scan(
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Un ciclo: evalúa cada símbolo, audita y devuelve los eventos.

    No acepta parámetros a propósito. `auto_execute` no es un flag de esta ruta: la
    ejecución no se puede encender desde aquí, y una ruta que parece encenderla es justo
    lo que hay que evitar.

    Escribe en `setup_log` y `setup_state`, así que **es una escritura y pide token**. Que
    no mande órdenes no la convierte en lectura.
    """
    return _servicio(rt).escanear()


@router.post("/api/watcher/auto-execute", response_model=None)
def watcher_auto_execute(
    body: AutoExecuteBody,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """501: el watcher no manda órdenes.

    Se responde igual con `enabled` a true o a false. Un `200` de "apagado" enseñaría que
    el interruptor estaba encendido y se ha apagado; no lo estaba, porque no hay nada que
    encender en esta fase. El cuerpo lleva el motivo y qué haría falta para que exista.
    """
    raise HTTPException(
        status_code=501,
        detail={
            "error": "auto-execute no disponible: el watcher no manda órdenes",
            "enabled": bool(getattr(body, "enabled", False)),
            "auto_execute": False,
            "auto_execute_disponible": watcher_service.AUTO_EJECUCION_DISPONIBLE,
            "motivo": watcher_service.MOTIVO_SIN_AUTO_EJECUCION,
        },
    )


__all__ = ["AutoExecuteBody", "router"]

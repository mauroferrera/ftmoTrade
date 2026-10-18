"""Rutas de estado: ¿qué está vivo y con qué config?

Las dos cosas que un operador mira antes que nada, y las dos contestan con
estados nombrados (`ok`, `sin_puerto`, `error`) en vez de booleanos: `store:
false` no dice si la base está apagada o si el store está mal cableado.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from .. import deps
from ..runtime import Runtime

router = APIRouter(tags=["estado"])


@router.get("/api/health", response_model=None)
def health(rt: Runtime = Depends(deps.runtime)) -> Dict[str, Any]:
    """El estado del proceso y de sus cables. Nunca devuelve 500.

    Es la ruta que un monitor consulta cada 30 s. Si `health` puede lanzar, el
    monitor solo distingue "la API está caída" de "la API está caída por lo que
    intenta decirme", y para eso ya está el log.
    """
    estado = rt.health()
    estado["auth"] = "token" if deps.token_presente() else "sin_token"
    return estado


@router.get("/api/config/summary", response_model=None)
def config_summary(rt: Runtime = Depends(deps.runtime)) -> Dict[str, Any]:
    """Config de trading que la UI necesita, con los valores tal cual.

    `symbols` y `execution` son un recorte de la config anidada para que el
    frontend no tenga que hardcodear ni parsear el YAML entero.
    """
    return deps.resumen_config(rt)


__all__ = ["router"]

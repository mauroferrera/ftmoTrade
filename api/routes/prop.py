"""Rutas de prop firm: el estado del challenge de la cuenta activa.

Endpoints delgados: no calculan nada. La cuenta la lee `MarketService`, el perfil
lo resuelve `PropService` desde `config/prop_profiles.yaml` y la regla FTMO vive
en `core/prop_rules.py`. Aquí solo se elige el servicio del runtime y se devuelve.

Los errores no se traducen aquí: `AdapterError` (terminal caída) y
`ConfigUnavailable` (sin store) son 503; `PropProfilesError` es un `ValueError`
y sale 400 (un YAML de topes mal escrito se ve, no se esconde); los recoge
`api/errors.py` a nivel de app.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from api.services.prop import PropService

from .. import deps

router = APIRouter(prefix="/api/prop", tags=["prop"])


def prop(rt=Depends(deps.runtime)) -> PropService:
    """El servicio de prop del runtime. 503 si no está cableado."""
    return deps.prop_service(rt)


@router.get("/state", response_model=None)
def state(svc: PropService = Depends(prop)) -> Dict[str, Any]:
    """El veredicto del shell prop para la cuenta activa.

    Siempre la misma forma. Con la cuenta sin perfil mapeado devuelve
    `tracked=false` y el motivo (`fail-closed`): es un HECHO que la UI muestra, no
    un error. El 404/503/400 solo aparece cuando falla el bróker o la config.
    """
    return svc.state()


@router.get("/profiles", response_model=None)
def profiles(svc: PropService = Depends(prop)) -> Dict[str, Any]:
    """Los perfiles definidos y los logins mapeados. Diagnóstico para la UI."""
    return svc.perfiles()


__all__ = ["router"]

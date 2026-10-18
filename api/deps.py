"""Dependencias de FastAPI: dónde están las cosas y quién puede llamarlas.

Dos familias, y la separación es deliberada:

- **Proveedores** (`runtime`, `store`): leen el contenedor de `app.state` y
  devuelven la pieza. Ninguna función de este módulo importa `api.app`, así que
  un test de endpoint no necesita la app y un test de la app no necesita endpoints.
- **Autenticación** (`require_api_token`): compara el token por petición.

El token se lee por petición y se compara con `secrets.compare_digest` en vez de
`==`: la comparación de cadenas de Python sale antes en cuanto encuentra una
diferencia, así que un `==` filtra el token prefijo a prefijo.
"""

from __future__ import annotations

import os
import secrets
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

from .runtime import Runtime, token_actual

#: Variable de entorno con la lista de orígenes permitidos, separada por comas.
ALLOWED_ORIGINS_ENV = "ALLOWED_ORIGINS"

#: Orígenes de desarrollo por defecto.
DEFAULT_ORIGINS = ("http://localhost:8000", "http://127.0.0.1:8000")


def origins(origins_value: Optional[str] = None) -> list:
    """Los orígenes permitidos, leídos del entorno si no se pasan.

    Nunca devuelve `*`: con credenciales o sin ellas, un `*` abierto convierte
    cualquier página web en un cliente de la API con la sesión del usuario.
    """
    crudo = origins_value if origins_value is not None else os.getenv(ALLOWED_ORIGINS_ENV)
    if not crudo:
        return list(DEFAULT_ORIGINS)
    return [o.strip() for o in str(crudo).split(",") if o.strip()]


# -- proveedores ---------------------------------------------------------------


def runtime(request: Request) -> Runtime:
    """El contenedor de la aplicación.

    Si no está, es que alguien montó un `APIRouter` sin la app: es un bug de
    cableado, y por eso el error lo dice con su nombre en vez de devolver un 503.
    """
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:
        raise HTTPException(
            status_code=503,
            detail="la aplicación no tiene runtime: falta startup() o el router está suelto",
        )
    return rt


def store(request: Request) -> Any:
    rt = runtime(request)
    if rt.store is None:
        raise HTTPException(status_code=503, detail="base de datos no cableada")
    return rt.store


def market_service(rt: Runtime) -> Any:
    """El servicio de mercado del contenedor. 503 si no está cableado."""
    svc = getattr(rt, "market", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="mercado no cableado: falta Runtime.build() con market",
        )
    return svc


def prop_service(rt: Runtime) -> Any:
    """El servicio de prop del contenedor. 503 si no está cableado.

    Depende del mercado (lee la cuenta) y del store (perfil + estado): sin
    cualquiera de los dos, el runtime lo deja en `None` y aquí se responde 503.
    """
    svc = getattr(rt, "prop", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="prop no cableado: falta Runtime.build() con prop",
        )
    return svc


def watcher_service(rt: Runtime) -> Any:
    """El servicio del watcher del contenedor. 503 si no está cableado.

    Se construye aunque falte el mercado o el store, porque degrada y lo declara: un
    503 en `/api/watcher/status` no ayudaría a un panel que solo quiere saber si hay
    algo configurado. El 503 solo aparece si el propio servicio no se pudo ni construir.
    """
    svc = getattr(rt, "watcher", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="watcher no cableado: falta Runtime.build() con watcher",
        )
    return svc


# -- autenticación -------------------------------------------------------------


def require_api_token(request: Request) -> None:
    """Valida el token si hay uno configurado. 401 si no cuadra.

    Sin `API_TOKEN` en el entorno **no** se exige nada: es el modo de desarrollo
    local, y ponerlo obligatorio rompe el arranque de un portátil sin servidor.
    Con token puesto, la ausencia de cabecera y el valor equivocado son la misma
    respuesta y el mismo código.
    """
    esperado = token_actual()
    if esperado is None:
        return
    recibido = request.headers.get("x-api-token") or ""
    if not recibido:
        authorization = request.headers.get("authorization") or ""
        if authorization.lower().startswith("bearer "):
            recibido = authorization[7:].strip()
    if not recibido or not secrets.compare_digest(recibido, esperado):
        raise HTTPException(status_code=401, detail="token inválido o ausente")


def token_presente() -> bool:
    """¿Hay token configurado? Lo que `/api/health` publica en `auth`."""
    return token_actual() is not None


def resumen_config(rt: Runtime) -> Dict[str, Any]:
    """Lo que la UI necesita de la config para no hardcodear. Read-only."""
    cfg: Dict[str, Any] = {}
    if rt.store is not None:
        try:
            cfg = rt.store.get_trading_config() or {}
        except Exception:  # noqa: BLE001 - health/config no lanzan
            cfg = {}
    return {
        "symbols": cfg.get("symbols") or [],
        "execution": cfg.get("execution") or {},
    }


__all__ = [
    "ALLOWED_ORIGINS_ENV",
    "DEFAULT_ORIGINS",
    "market_service",
    "origins",
    "prop_service",
    "require_api_token",
    "resumen_config",
    "runtime",
    "store",
    "token_presente",
    "watcher_service",
]

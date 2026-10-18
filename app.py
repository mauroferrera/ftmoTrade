"""Punto de entrada: `python app.py`.

Arranca Uvicorn con el `app` de `api.app`. Existe como fichero de raíz para que
el comando de arranque sea uno y corto (`python app.py`), en vez de recordar la
cadena de `uvicorn`. La config de host/puerto sale del `.env` (o del entorno).

`reload` se activa con `DEBUG=true` en el `.env`: recarga al guardar, cómodo en
desarrollo y peligroso en producción (dos procesos), por eso es opt-in.
"""

from __future__ import annotations

import os

import uvicorn
from dotenv import load_dotenv

from core import paths

load_dotenv(os.path.join(paths.PROJECT_DIR, ".env"))
load_dotenv(os.path.join(os.path.dirname(paths.PROJECT_DIR), ".env"))


def _bool_env(name: str, default: bool = False) -> bool:
    valor = os.getenv(name)
    if valor is None:
        return default
    return valor.strip().lower() in ("1", "true", "yes", "on")


def main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    reload = _bool_env("DEBUG", False)
    log_level = os.getenv("LOG_LEVEL", "info").lower()
    uvicorn.run(
        "api.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()

"""La aplicación FastAPI. Bootstrap, ciclo de vida, middleware, montaje y routers.

Este fichero hace cuatro cosas, y ninguna más:

1. `create_app()`: construye el contenedor, engancha los routers, instala CORS,
   los handlers de error y los estáticos.
2. El `lifespan`: `runtime.startup()` al entrar, `runtime.shutdown()` al salir.
3. El middleware de tiempo de respuesta.
4. El `app` de módulo, para `uvicorn api.app:app`.

Lo que NO está aquí y por qué
-----------------------------
- **Lógica de negocio.** Ni SMC, ni score, ni lectura de MT5. Eso son `core/`
  (puro) y, más adelante, `api/services/`.
- **Routers.** Viven en `api/routes/`. Cada uno se importa por su módulo para que
  un fallo de uno no impida montar la app entera.
- **Estado mutable.** El contenedor vive en `app.state.runtime`.
- **Acceso a la base de datos.** No se abre aquí. `runtime.startup()` la
  inicializa y falla hacia atrás con un warning, porque una API sin base de datos
  puede servir `/api/health`, que es justo lo que se necesita para saberlo.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import errors
from .deps import ALLOWED_ORIGINS_ENV, origins
from .routes import execution, health, market, prop, setup, watcher
from .runtime import Runtime

#: Carga `.env` de la raíz del proyecto una sola vez al importar la app.
#: `override=False` para no pisar variables ya definidas en el entorno real.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

API_TITLE = "FTMO Copilot Dashboard"
API_VERSION = "0.1.0"

log = logging.getLogger("api")

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static"
)
INDEX_FILE = "index.html"

#: Peticiones por encima de esto se avisan en el log. Es un umbral de vigilancia.
LOG_SLOW_MS = 1000.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Enciende y apaga el contenedor. Lo único que vive fuera de una petición."""
    runtime = app.state.runtime
    await runtime.startup()
    try:
        yield
    finally:
        await runtime.shutdown()


def create_app(runtime: Optional[Runtime] = None) -> FastAPI:
    """La aplicación. Con `runtime` para tests; sin él, el cableado real."""
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.runtime = runtime if runtime is not None else Runtime.build()

    errors.install(app)
    _instala_cors(app)
    _instala_tiempos(app)

    for modulo in (execution, health, market, prop, setup, watcher):
        app.include_router(modulo.router)

    _monta_estaticos(app)
    return app


def _instala_cors(app: FastAPI) -> None:
    """CORS con la lista de orígenes del entorno, leída al construir la app.

    El default son dos `localhost`, no `*`: con credenciales, un `*` abierto
    convierte cualquier página web en un cliente de la API con la sesión del
    usuario. Ampliar es cosa del despliegue, por entorno.
    """
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Process-Time-Ms"],
    )


def _instala_tiempos(app: FastAPI) -> None:
    """Un middleware de tiempo. Uno solo, y solo mide."""

    @app.middleware("http")
    async def _cronometro(request: Request, call_next: Any) -> Any:
        inicio = time.perf_counter()
        respuesta = await call_next(request)
        ms = (time.perf_counter() - inicio) * 1000.0
        respuesta.headers["X-Process-Time-Ms"] = "{0:.1f}".format(ms)
        if ms > LOG_SLOW_MS:
            log.warning("petición lenta: %s %s en %.0f ms",
                        request.method, request.url.path, ms)
        return respuesta


def _monta_estaticos(app: FastAPI) -> None:
    """Sirve `static/` y `/`. Si no existe el directorio, la API sigue.

    Un frontend ausente es un problema de despliegue; que la API no arranque por
    eso es un problema de diseño. Se avisa y se sigue.
    """
    if not os.path.isdir(STATIC_DIR):
        log.warning("sin estáticos en %s: la API sirve pero el dashboard no", STATIC_DIR)

        @app.get("/", response_model=None)
        def _sin_estaticos() -> JSONResponse:
            return JSONResponse(
                {"error": "el frontend no está desplegado", "docs": "/api/docs"},
                status_code=503,
            )

        return

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_model=None, include_in_schema=False)
    def index() -> Any:
        ruta = os.path.join(STATIC_DIR, INDEX_FILE)
        if not os.path.isfile(ruta):
            return JSONResponse({"error": "falta static/index.html"}, status_code=503)
        return FileResponse(ruta)


#: El objeto que Uvicorn carga: `uvicorn api.app:app`.
app = create_app()


__all__ = ["API_TITLE", "API_VERSION", "ALLOWED_ORIGINS_ENV", "STATIC_DIR",
           "app", "create_app", "lifespan"]

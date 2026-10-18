"""Traducción de excepciones de las capas inferiores a respuestas HTTP.

Cada handler se registra sobre la clase que SIGNIFICA algo, no sobre una clase
ancha. Un handler sobre `RuntimeError` (que es la base de casi todo) se traga un
`KeyError` disfrazado de cualquier lado y lo devuelve como "no hay conexión con
el bróker".

| excepción            | código | qué le dice al cliente              |
|----------------------|--------|-------------------------------------|
| `ConfigUnavailable`  | 503    | no hay `strategy.yaml` que cargar    |
| `TerminalUnavailable`| 503    | no hay terminal/bróker conectado     |
| `AdapterError`       | 503    | el proveedor de datos falló          |
| `SymbolNotFound`     | 404    | el bróker no publica ese símbolo     |
| `ValueError`         | 400    | el cliente pidió algo imposible      |

`SymbolNotFound` hereda de `AdapterError`, y Starlette resuelve el handler por la
MRO de la excepción: busca la clase más específica registrada. Basta con registrar
ambas, porque un `SymbolNotFound` encontrará su propio handler (404) antes que el
de la base (503). Sin el de `SymbolNotFound`, un símbolo inexistente saldría como
503 ("el bróker falla") cuando lo correcto es 404 ("ese símbolo no existe").

Un bug de código (`TypeError`, `KeyError`, `AttributeError`) NO tiene handler:
sube y sale 500 con el traceback en el log, que es donde tiene que estar.

`ValueError` es el único caso ambiguo y se acepta a propósito: es lo que lanzan
los validadores de parámetros, y esos son errores de la petición.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from adapters.base_adapter import AdapterError, SymbolNotFound, TerminalUnavailable
from database.store import ConfigUnavailable

#: `(excepción, código, mensaje por defecto)`. El orden importa: las subclases
#: van antes que sus bases para que `SymbolNotFound` no caiga en el handler de
#: `AdapterError`.
MANEJADORES: Tuple[Tuple[type, int, str], ...] = (
    (ConfigUnavailable, 503, "No hay configuración de estrategia cargada."),
    (SymbolNotFound, 404, "El bróker no publica ese símbolo."),
    (TerminalUnavailable, 503, "No hay conexión con el terminal del bróker."),
    (AdapterError, 503, "El proveedor de datos no pudo responder."),
    (ValueError, 400, "Parámetro fuera de rango."),
)


def _cuerpo(mensaje: str, codigo: int, por_defecto: str) -> Dict[str, Any]:
    texto = str(mensaje).strip() or por_defecto
    return {"error": texto, "status": codigo}


def _handler_para(codigo: int, por_defecto: str) -> Callable:
    """Construye el handler asíncrono de una clase de error."""

    async def handler(_request: Any, exc: BaseException) -> JSONResponse:
        return JSONResponse(_cuerpo(str(exc), codigo, por_defecto), status_code=codigo)

    return handler


def install(app: FastAPI) -> None:
    """Registra los handlers de las capas de datos sobre `app`."""
    for exc, codigo, por_defecto in MANEJADORES:
        app.add_exception_handler(exc, _handler_para(codigo, por_defecto))


__all__ = ["MANEJADORES", "install"]

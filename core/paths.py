"""Fuente única de verdad de las rutas del proyecto.

Todo cálculo de ruta vive aquí y es ABSOLUTA, contra la raíz del repositorio y
nunca contra el CWD. Con una ruta relativa, lanzar la app o la suite desde otro
directorio crea un SEGUNDO `trading.db`: la auditoría parece evaporada mientras
sigue entera, en otro fichero.

Este módulo no importa nada del proyecto. Si alguna vez necesita hacerlo, es la
señal de que una ruta está mal de sitio.
"""

from __future__ import annotations

import os

# Raíz del repositorio: el directorio que contiene ESTE fichero, un nivel arriba
# (core/ -> raíz). Fijado por estructura, no por la convención del CWD.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Configuración -----------------------------------------------------------

CONFIG_DIR = os.path.join(PROJECT_DIR, "config")

STRATEGY_PATH = os.environ.get(
    "STRATEGY_PATH", os.path.join(CONFIG_DIR, "strategy.yaml")
)

# prop_profiles.yaml -> perfil de prop firm (FTMO) por cuenta. Aparte de
# strategy.yaml porque es OTRA clase de configuración: strategy.yaml describe
# UNA estrategia; este describe los topes de UNA CUENTA (producto, capital,
# cortes internos). La llave es el login de MT5 (`accounts:`), así que varias
# cuentas conviven sin tocar el resto de la config.
PROP_PROFILES_PATH = os.environ.get(
    "PROP_PROFILES_PATH", os.path.join(CONFIG_DIR, "prop_profiles.yaml")
)

SYMBOLS_PATH = os.environ.get(
    "SYMBOLS_PATH", os.path.join(CONFIG_DIR, "symbols.yaml")
)

# --- Persistencia ------------------------------------------------------------

DATABASE_DIR = os.path.join(PROJECT_DIR, "database")

DB_PATH = os.environ.get("DB_PATH", os.path.join(DATABASE_DIR, "trading.db"))

# --- Datos externos (fuera del repo) ------------------------------------------

# Los datos PESAN: las capturas de pantalla de cada operación crecen con cada
# trade y no son código. Fuera del repo para que git no se los lleve y para que
# OneDrive no los sincronice. La raíz es una VARIABLE por lo mismo: una regla
# escrita solo en un comentario depende de que alguien la lea.
DATA_ROOT = os.environ.get(
    "FTMO_DATA_ROOT",
    os.path.join(os.path.expanduser("~"), "Desktop", "ftmo_data"),
)

DATA_DIR = DATA_ROOT

# Capturas post-trade (tabla `trade_marks.capture_path`).
COPILOT_DIR = os.path.join(DATA_ROOT, "copilot")
COPILOT_MARKS_DIR = os.path.join(COPILOT_DIR, "marks")

# --- Testing -----------------------------------------------------------------

TEST_FIXTURES_DIR = os.path.join(PROJECT_DIR, "tests", "fixtures")


def ensure_dir(path: str) -> str:
    """Crea el directorio si falta y lo devuelve. Para rutas de escritura."""
    os.makedirs(path, exist_ok=True)
    return path


# --- Guardián de la raíz de datos ---------------------------------------------

# El proyecto de referencia (Trinity) vive FUERA de este repo y es de SOLO
# LECTURA: de ahí se copian piezas, pero no se le escribe nunca. Escribir datos
# ahí corrompería el original con el que se comparan los ports.
REFERENCE_DIR = os.path.join(
    os.path.dirname(PROJECT_DIR), "Trinity_proyect"
)


def _is_within(path: str, parent: str) -> bool:
    r"""True si `path` cae dentro de `parent` (o es él mismo).

    No mira si existen: es una comprobación de FORMA, y tiene que poderse hacer
    ANTES de crear nada. `normcase` hace que en Windows `c:\users\...` y
    `C:\Users\...` cuenten como el mismo sitio; sin él, la regla se salta con
    cualquier diferencia de mayúsculas.
    """
    try:
        p = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(parent))
    except (TypeError, ValueError, OSError):
        return False
    if p == b:
        return True
    return p.startswith(b if b.endswith(os.sep) else b + os.sep)


def _within_onedrive(path: str) -> bool:
    """True si algún componente del camino es un directorio de OneDrive.

    No se comprueba solo "OneDrive\\Desktop": el tenant añade su nombre
    ("OneDrive - Empresa"). Con empezar por `onedrive` se cubren todos.
    """
    try:
        parts = os.path.normcase(os.path.abspath(path)).split(os.sep)
    except (TypeError, ValueError, OSError):
        return False
    return any(p.startswith("onedrive") for p in parts if p)


def data_root_guard_error(path: str) -> str | None:
    """Motivo por el que `path` no sirve como raíz de datos, o `None` si sirve.

    Es el contrato de DATOS FUERA DEL REPO escrito como función y no como
    comentario: un comentario depende de que alguien lo lea, esta función no.
    Quien descargue datos la llama y se niega a escribir si devuelve un motivo.

    Cuatro motivos, de lo más caro a lo menos: vacía, relativa (contra el CWD),
    dentro del repo (git se lo llevaría), dentro de OneDrive (se sincroniza), o
    dentro de REF (solo lectura por decisión).
    """
    if path is None or not str(path).strip():
        return "la raíz de datos está vacía"
    if not os.path.isabs(path):
        return f"es relativa: {path!r} dependería del CWD del proceso"
    if _is_within(path, PROJECT_DIR):
        return (
            f"dentro del repo ({path}): el repo está en OneDrive, así que los "
            "datos subirían a la nube Y se podrían versionar en git"
        )
    if _within_onedrive(path):
        return f"dentro de OneDrive ({path}): gigabytes sincronizados a diario"
    if _is_within(path, REFERENCE_DIR):
        return f"dentro de REF ({path}): es código de referencia de solo lectura"
    return None

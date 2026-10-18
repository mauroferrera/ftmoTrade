"""Reloj canónico UTC: UNA sola convención de tiempo para todo el proyecto.

Extraído de `REF/tclock.py`, que mezclaba dos cosas con requisitos opuestos:

  - el reloj UTC canónico, que es aritmética de `datetime` y no depende de nadie;
  - el reloj de PARED DEL BROKER, que lee `strategy.yaml`, `MetaTrader5` y un
    fichero que publica el EA.

Separar las dos no es un capricho: con el módulo unido, cualquier capa que quisiera
sellar una hora tenía que importar MetaTrader5, y `core/` (que por D-009 no puede
importarlo) se quedaba sin reloj. Aquí vive la mitad pura; la del broker se queda
en `tclock`/`api` hasta que haya un plan para MT5.

CONVENCIONES (y solo dos, sin excepciones):

1. **UTC aware** en TODO Python/SQLite. Todo datetime que se persiste, se compara
   o se cruza con la API de MT5 lleva tzinfo. `now_iso()` es el único
   generador de sellos de auditoría. Un valor naive en la base es un bug, no
   una convención: `to_utc()` los reinterpreta como UTC para que el sistema no
   reviente al comparar, pero el escritor es el que debe corregirlo.

2. **Día de trading** = día natural del SERVIDOR DEL BROKER. Esa parte vive en
   `tclock.py` porque necesita el offset del broker; aquí no, y no se finge lo
   contrario. Lo que sí se conserva es la regla que la hace funcionar: el
   backend y el EA deben contar EXACTAMENTE las mismas operaciones en la
   EXACTAMENTE misma ventana, o `max_trades_day` deja de ser un tope
   compartido (con dos ventanas desalineadas, EA y bot autorizan 3 + 3 = 6).

Este módulo no importa nada del proyecto: ni store, ni strategy, ni app. Por eso
cualquier capa puede usar el reloj sin riesgo de ciclo de imports.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Any, Optional

UTC = timezone.utc


# ============================================================
# Reloj canónico (UTC)
# ============================================================


def now_utc() -> datetime:
    """Instante actual, aware, en UTC. Es el reloj por defecto del sistema."""
    return datetime.now(UTC)


def now_iso(at: Optional[datetime] = None) -> str:
    """Sello de auditoría: ISO 8601 UTC con offset explícito (+00:00).

    Formato fijo porque se usa como clave de orden lexicográfica en SQLite:
    todos los valores deben tener exactamente la misma longitud y sufijo, o la
    comparación de strings recorta (es lo que pasaba con `+00:00` contra naive).
    """
    return to_utc(at or now_utc()).isoformat(timespec="seconds")


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normaliza a UTC aware.

    - aware -> convierte a UTC (no cambia el instante, cambia la etiqueta).
    - naive  -> se asume UTC. Es la única interpretación segura: casi todo lo
      naive heredado venía de `now_iso()` (que siempre fue UTC) al que se le
      había stripped el offset al serializar.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def from_epoch(value: Any) -> Optional[datetime]:
    """Epoch en segundos -> datetime UTC aware.

    Existe porque MT5 habla en epoch (`deal.time`, `candle.time`) y `to_utc()` solo
    acepta datetimes. Pasar un epoch a `to_utc()` no avisa: revienta con
    `AttributeError: 'int' object has no attribute 'tzinfo'` tres capas más allá, en
    el endpoint, donde ya no se sabe de dónde salió el entero.

    Devuelve `None` para `None` y para un valor no convertible, para que el llamante
    pueda distinguir "sin fecha" de "con fecha cero".
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_utc(value)
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def parse_iso(value: Any) -> Optional[datetime]:
    """ISO 8601 -> datetime UTC aware. Tolera 'Z', naive (asumido UTC) y None."""
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, int, float)):
        return from_epoch(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return to_utc(datetime.fromisoformat(text))
    except (TypeError, ValueError):
        return None


def epoch() -> float:
    """Epoch UTC (independiente de la zona del SO)."""
    return _time.time()


def fmt_utc(
    value: Any = None,
    pattern: str = "%Y-%m-%d %H:%M:%S",
    suffix: str = " UTC",
) -> str:
    """Formatea un instante (epoch, ISO o datetime) en UTC con etiqueta.

    Sin argumento, formatea el ahora. La etiqueta no es decorativa: sin ella,
    un "2026-09-27 12:00:00" al lado de velas UTC es indistinguible de la hora
    local y es exactamente el defecto que hizo que el agente razonara sobre un
    reloj 3 h desfasado.
    """
    if value is None:
        dt = now_utc()
    elif isinstance(value, datetime):
        dt = to_utc(value)
    else:
        dt = parse_iso(value)
    if dt is None:
        return ""
    return dt.strftime(pattern) + suffix


def fmt_wall(dt: datetime, pattern: str, suffix: str) -> str:
    """Formatea un datetime TAL CUAL (sin convertir de zona).

    Para cuando el datetime ya viene en la zona que se quiere mostrar. Pasar por
    `fmt_utc` aquí volvería a convertir a UTC y mostraría la hora equivocada.
    """
    if dt is None:
        return ""
    return dt.strftime(pattern) + suffix


def fmt_hm(value: Any) -> str:
    """HH:MM en UTC (para ejes y badges donde la etiqueta se ve en otro sitio)."""
    return fmt_utc(value, "%H:%M", "")
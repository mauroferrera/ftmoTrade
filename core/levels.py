"""Niveles horizontales de soporte/resistencia para el gráfico.

El SMC (`core/smc_engine.py`) devuelve ZONAS (cajas FVG/OB). Este módulo
devuelve LÍNEAS horizontales: los precios donde históricamente el precio ha
reaccionado y que el copiloto marca en el chart. Son capas distintas y se
pintan distinto (línea vs caja).

Capas (D-003: `strategy.yaml:copilot.level_layers`):
  - `swings`:  extremos de swing confirmados (soportes/resistencias estructurales).
  - `pd`:      PDH/PDL, el rango del día anterior (referencia de sesión).
  - `round`:   números redondos dentro del rango visible (psicológicos).

`volume_profile` está en la config pero NO aquí: es una capa pesada que llega en
una fase posterior. Dejarla fuera de `build_levels` es la decisión de no
inventar datos que aún no se calculan.

Todo es puro: recibe velas normalizadas y `pdh`/`pdl`, devuelve dicts con
`{"kind", "price", "label"}`. Sin I/O, sin MT5, sin red.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from core import smc_engine

KIND_SWING_HIGH = "swing_high"
KIND_SWING_LOW = "swing_low"
KIND_PD = "pd"
KIND_ROUND = "round"

#: Cuántos swings de cada lado se devuelven como máximo. El gráfico no necesita
#: 40 líneas: necesita las que importan, y las más recientes son las que importan.
MAX_SWINGS_PER_SIDE = 3

#: División del rango visible para elegir el paso de los números redondos. 8 da
#: una decena de líneas sin saturar el chart.
_ROUND_BUCKETS = 8.0


def _paso_redondo(lo: float, hi: float) -> Optional[float]:
    """Un paso "bonito" (1/2/2.5/5 × 10^k) cercano a `span/8`.

    Los números redondos solo tienen sentido en pasos psicológicos: en EURUSD
    alrededor de 1.10 el paso útil es 0.0010; en oro a 2600 es 10. Elegir el
    paso por el ORDEN DE MAGNUD del rango hace que lo mismo sirva para un par de
    divisas y para un metal sin configuración por símbolo.
    """
    span = hi - lo
    if span <= 0 or not math.isfinite(span):
        return None
    raw = span / _ROUND_BUCKETS
    if raw <= 0:
        return None
    mag = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        paso = mult * mag
        if paso >= raw:
            return paso
    return 10.0 * mag


def swing_levels(candles: List[dict], window: int = smc_engine.BOS_WINDOW) -> List[dict]:
    """Los swings más recientes, como líneas.

    Devuelve hasta `MAX_SWINGS_PER_SIDE` swing highs y lo mismo de lows, usando
    los PRECIOS ya redondeados a 6 decimales. Se recorren los pivotes del más
    reciente al más antiguo para quedarse con los que el operador ve ahora.
    """
    if not candles:
        return []
    highs, lows = smc_engine.swing_pivots(candles, window)
    out: List[dict] = []
    seen: set = set()

    for i in reversed(highs):
        precio = _r6(candles[i]["high"])
        if precio in seen:
            continue
        seen.add(precio)
        out.append({"kind": KIND_SWING_HIGH, "price": precio, "label": "Swing H"})
        if _contar(out, KIND_SWING_HIGH) >= MAX_SWINGS_PER_SIDE:
            break

    seen.clear()
    for i in reversed(lows):
        precio = _r6(candles[i]["low"])
        if precio in seen:
            continue
        seen.add(precio)
        out.append({"kind": KIND_SWING_LOW, "price": precio, "label": "Swing L"})
        if _contar(out, KIND_SWING_LOW) >= MAX_SWINGS_PER_SIDE:
            break

    return out


def pd_levels(pdh: Optional[float], pdl: Optional[float]) -> List[dict]:
    """PDH y PDL como líneas. `None` si ese extremo no se conoce."""
    out: List[dict] = []
    if pdh is not None:
        out.append({"kind": KIND_PD, "price": _r6(pdh), "label": "PDH"})
    if pdl is not None:
        out.append({"kind": KIND_PD, "price": _r6(pdl), "label": "PDL"})
    return out


def round_levels(candles: List[dict]) -> List[dict]:
    """Números redondos dentro del rango de precios visible."""
    if not candles:
        return []
    lo = min(c["low"] for c in candles)
    hi = max(c["high"] for c in candles)
    paso = _paso_redondo(lo, hi)
    if not paso:
        return []
    out: List[dict] = []
    # Empezar en el primer múltiplo de `paso` por encima de `lo`, para no emitir
    # una línea por debajo del mínimo visible.
    p = math.ceil(lo / paso) * paso
    # +1e-9 de tolerancia: `math.ceil` sobre un float puede dejar el último
    # múltiplo justo por debajo del tope y perder la línea de arriba.
    while p <= hi + paso * 1e-9:
        out.append({"kind": KIND_ROUND, "price": _r6(p), "label": _r6(p).__str__()})
        p += paso
    return out


def build_levels(candles: List[dict], pdh: Optional[float] = None,
                 pdl: Optional[float] = None) -> List[dict]:
    """Todas las capas de niveles, en una lista plana para el frontend.

    Cada nivel lleva `kind` (para colorear) y `label` (para la etiqueta). El
    frontend solo itera la lista: no necesita saber de qué capa viene cada una.
    """
    niveles: List[Dict] = []
    niveles.extend(pd_levels(pdh, pdl))
    niveles.extend(swing_levels(candles))
    niveles.extend(round_levels(candles))
    return niveles


def _contar(levels: List[dict], kind: str) -> int:
    return sum(1 for lv in levels if lv["kind"] == kind)


def _r6(x: float) -> float:
    return round(float(x), 6)


__all__ = [
    "KIND_PD",
    "KIND_ROUND",
    "KIND_SWING_HIGH",
    "KIND_SWING_LOW",
    "MAX_SWINGS_PER_SIDE",
    "build_levels",
    "pd_levels",
    "round_levels",
    "swing_levels",
]

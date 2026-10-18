"""Motor de detección de patrones SMC: Fair Value Gaps, Order Blocks, Sweeps.

Algoritmos puros sobre velas OHLC. Reciben `[{"time", "open", "high", "low",
"close"}]` (la forma que devuelve `MarketService.candles`) y devuelven
estructuras listas para el frontend. No tocan MT5, la red ni la base: eso los
hace testeables con una lista de dicts y deterministas.

Convención de mitigación
------------------------
Una zona (FVG u OB) queda MITIGADA en cuanto cualquier mecha posterior entra o
cruza la caja, no solo un cierre. Solo se devuelven zonas VIVAS: una zona
mitigada ya no es soporte/resistencia, es memoria.

Portado de `Trinity_proyect/core/smc_engine.py` (D-001), recortado a lo que el
copiloto usa. El módulo es idéntico en comportamiento; cambia el estilo y se
expone `swing_pivots` como público porque `core/levels.py` lo reutiliza.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import List, Optional, Tuple

BULLISH_FVG = "BULLISH_FVG"
BEARISH_FVG = "BEARISH_FVG"
BULLISH_OB = "BULLISH_OB"
BEARISH_OB = "BEARISH_OB"
PDH_SWEEP = "PDH_SWEEP"
PDL_SWEEP = "PDL_SWEEP"

_ZONE_LABELS = {
    BULLISH_FVG: "Bullish FVG",
    BEARISH_FVG: "Bearish FVG",
    BULLISH_OB: "Bullish OB",
    BEARISH_OB: "Bearish OB",
}

#: Ventana de confirmación de un swing (velas a cada lado). Es el mismo número
#: que define un BOS: un punto que no sobrevive 5 velas a cada lado no es un
#: swing, es ruido.
BOS_WINDOW = 5


def _r6(x: float) -> float:
    return round(float(x), 6)


def swing_pivots(candles: List[dict], window: int = BOS_WINDOW) -> Tuple[List[int], List[int]]:
    """Índices de swing highs y swing lows con `window` velas a cada lado.

    Un índice `i` es swing high si su `high` es el máximo de la ventana
    `(i-window, i+window)` (excluido `i`); swing low si su `low` es el mínimo.
    Se excluye `i` de la comparación para que una vela plana no sea su propio
    máximo y mínimo.
    """
    highs: List[int] = []
    lows: List[int] = []
    n = len(candles)
    for i in range(window, n - window):
        seg_h = [candles[i + k]["high"] for k in range(-window, window + 1) if i + k != i]
        seg_l = [candles[i + k]["low"] for k in range(-window, window + 1) if i + k != i]
        if candles[i]["high"] >= max(seg_h):
            highs.append(i)
        if candles[i]["low"] <= min(seg_l):
            lows.append(i)
    return highs, lows


def _zone_mitigated(candles: List[dict], from_idx: int, top: float, bottom: float) -> bool:
    """True si alguna mecha a partir de `from_idx` entra o cruza `[bottom, top]`."""
    for c in candles[from_idx:]:
        if c["low"] <= top and c["high"] >= bottom:
            return True
    return False


def detect_fvgs(candles: List[dict]) -> List[dict]:
    """Fair Value Gaps de 3 velas, solo los VIVOS (no mitigados).

    Un FVG bullish deja un hueco cuando la vela `i` abre por encima del high de
    la vela `i-2` (`cur.low > prev.high`); el hueco es `[prev.high, cur.low]`.
    El bearish es el espejo. El tipo distingue `BULLISH_FVG` de `BEARISH_FVG`.
    """
    out: List[dict] = []
    n = len(candles)
    for i in range(2, n):
        prev, cur = candles[i - 2], candles[i]
        if cur["low"] > prev["high"]:
            top, bottom, bullish = cur["low"], prev["high"], True
        elif cur["high"] < prev["low"]:
            top, bottom, bullish = prev["low"], cur["high"], False
        else:
            continue
        if _zone_mitigated(candles, i + 1, top, bottom):
            continue
        out.append({
            "type": BULLISH_FVG if bullish else BEARISH_FVG,
            "top": _r6(top),
            "bottom": _r6(bottom),
            "start_time": cur["time"],
        })
    return out


def _last_opposite_candle(candles: List[dict], before_idx: int, window: int, up: bool) -> Optional[int]:
    """Última vela en dirección contrastaria al impulso, previa al BOS.

    `up=True`  → impulso alcista → la OB es la última vela BAJISTA.
    `up=False` → impulso bajista → la OB es la última vela ALCISTA.
    """
    start = max(0, before_idx - window)
    for j in range(before_idx - 1, start - 1, -1):
        c = candles[j]
        if up and c["close"] < c["open"]:
            return j
        if (not up) and c["close"] > c["open"]:
            return j
    return None


def _append_active_ob(out, candles, ob_idx, bos_idx, ob_type) -> None:
    ob = candles[ob_idx]
    top, bottom = ob["high"], ob["low"]
    # La mitigación se evalúa DESPUÉS de la vela de ruptura del BOS: la propia
    # vela de impulso sale de la zona por definición y no la mitiga.
    if _zone_mitigated(candles, bos_idx + 1, top, bottom):
        return
    out.append({
        "type": ob_type,
        "top": _r6(top),
        "bottom": _r6(bottom),
        "start_time": ob["time"],
    })


def detect_order_blocks(candles: List[dict]) -> List[dict]:
    """Order Blocks: la última vela contraria previa al impulso que rompe estructura.

    Solo los VIVOS: se descartan los que cualquier vela posterior haya vuelto a
    penetrar. Un OB es la base desde la que salió el impulso que rompió un swing,
    y ahí suele quedar liquidez sin ejecutada.
    """
    out: List[dict] = []
    n = len(candles)
    highs, lows = swing_pivots(candles)
    last_sh = None  # (precio, índice) del último swing high confirmado
    last_sl = None  # (precio, índice) del último swing low confirmado
    seen_obs: set = set()  # evita repetir el mismo OB en cada vela de BOS

    hi_p, lo_p = 0, 0
    for i in range(max(BOS_WINDOW, 1), n):
        # Avanzar pivotes confirmados hasta antes de i
        while hi_p < len(highs) and highs[hi_p] < i:
            if last_sh is None or candles[highs[hi_p]]["high"] >= last_sh[0]:
                last_sh = (candles[highs[hi_p]]["high"], highs[hi_p])
            hi_p += 1
        while lo_p < len(lows) and lows[lo_p] < i:
            if last_sl is None or candles[lows[lo_p]]["low"] <= last_sl[0]:
                last_sl = (candles[lows[lo_p]]["low"], lows[lo_p])
            lo_p += 1

        if last_sh and candles[i]["close"] > last_sh[0]:
            ob_idx = _last_opposite_candle(candles, i, BOS_WINDOW, up=True)
            if ob_idx is not None and ob_idx not in seen_obs:
                _append_active_ob(out, candles, ob_idx, i, BULLISH_OB)
                seen_obs.add(ob_idx)
        elif last_sl and candles[i]["close"] < last_sl[0]:
            ob_idx = _last_opposite_candle(candles, i, BOS_WINDOW, up=False)
            if ob_idx is not None and ob_idx not in seen_obs:
                _append_active_ob(out, candles, ob_idx, i, BEARISH_OB)
                seen_obs.add(ob_idx)
    return out


def detect_sweeps(candles: List[dict], pdh: Optional[float], pdl: Optional[float]) -> List[dict]:
    """Liquidity Sweeps: mecha que supera PDH/PDL y cierra de vuelta dentro.

    Es el barrido de liquidez por encima de la resistencia (PDH) o por debajo del
    soporte (PDL): el precio pincha el nivel, atrapa a los que estaban esperando
    el breakout y vuelve. Se reporta el nivel barrido y el extremo de la mecha.
    """
    out: List[dict] = []
    for c in candles:
        if pdh is not None and c["high"] > pdh and c["close"] < pdh:
            out.append({
                "type": PDH_SWEEP,
                "price_level": _r6(pdh),
                "wick_extreme": _r6(c["high"]),
                "time": c["time"],
            })
        elif pdl is not None and c["low"] < pdl and c["close"] > pdl:
            out.append({
                "type": PDL_SWEEP,
                "price_level": _r6(pdl),
                "wick_extreme": _r6(c["low"]),
                "time": c["time"],
            })
    return out


def analyze(candles: List[dict], pdh: Optional[float], pdl: Optional[float],
            symbol: str = "", timeframe: str = "") -> dict:
    """El análisis SMC completo: fvgs + order_blocks + sweeps.

    Es la pieza que consume `/api/market/analysis` (y el watcher más adelante).
    """
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "patterns": {
            "fvgs": detect_fvgs(candles),
            "order_blocks": detect_order_blocks(candles),
            "sweeps": detect_sweeps(candles, pdh, pdl),
        },
    }


def classify_entry(candles: List[dict], pdh: Optional[float], pdl: Optional[float],
                   entry_price: float, entry_time: int) -> dict:
    """Cruza el precio/hora de entrada con las zonas detectadas (para la bitácora).

    Devuelve `{"poi_type": str|None, "liquidity_swept": str|None}`: qué zona SMC
    cubre la entrada y qué liquidez se barrió justo antes.
    """
    j = bisect_right([c["time"] for c in candles], entry_time) - 1
    if j < 0:
        return {"poi_type": None, "liquidity_swept": None}

    parts: List[str] = []

    for fg in detect_fvgs(candles):
        if fg["start_time"] <= candles[j]["time"] and fg["bottom"] <= entry_price <= fg["top"]:
            parts.append(_ZONE_LABELS[fg["type"]])
    for ob in detect_order_blocks(candles):
        if ob["start_time"] <= candles[j]["time"] and ob["bottom"] <= entry_price <= ob["top"]:
            parts.append(_ZONE_LABELS[ob["type"]])

    swept = None
    for sw in detect_sweeps(candles[: j + 1], pdh, pdl):
        idx = _index_of_time(candles, sw["time"])
        if idx is not None and j - idx <= 8:
            swept = "PDH Sweep" if sw["type"] == PDH_SWEEP else "PDL Sweep"
            break

    return {
        "poi_type": " + ".join(dict.fromkeys(parts)) or None,
        "liquidity_swept": swept,
    }


def _index_of_time(candles: List[dict], ts: int) -> Optional[int]:
    for i, c in enumerate(candles):
        if c["time"] == ts:
            return i
    return None


__all__ = [
    "BOS_WINDOW",
    "BEARISH_FVG",
    "BEARISH_OB",
    "BULLISH_FVG",
    "BULLISH_OB",
    "PDH_SWEEP",
    "PDL_SWEEP",
    "analyze",
    "classify_entry",
    "detect_fvgs",
    "detect_order_blocks",
    "detect_sweeps",
    "swing_pivots",
]

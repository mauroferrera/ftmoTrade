"""Motor de riesgo determinista: Setup Score (SMC + killzone) y validación de entrada.

Módulo PURO: no importa MT5, ni la app, ni los servicios de datos. Recibe toda la
entrada por parámetros para poder probarse sin terminal abierta. Produce el Setup
Score (0-100) de cada dirección y valida la entrada con R:R mínimo, invalidez
estructural y TTL del setup.

Recorte respecto al motor de Trinity (ver `docs/ALCANCE.md`): aquí SOLO entran los
componentes con feed en este proyecto. Fuera el COT, el CVD, el SMR/DXY y el
clasificador de régimen; el estado prop-firm vive en `core/prop_rules.py`. Un peso
> 0 sobre un componente sin datos es puntuación reservada que nunca se devuelve:
aplanaba el score hacia cero sin que nadie pudiera decir por qué.

Convenciones de dirección: "BUY" (long) o "SELL" (short).
Los pesos, umbrales, ventanas de killzone y TTL vienen de la config de trading y se
sobreescriben con estos defaults si faltan.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from typing import Any, Dict, List, Optional

# Pesos por defecto, en puntos. El score es sum(valor*peso)/total*100, así que si
# la config no dice otra cosa estos DOS son los que reparten los 100 puntos. En
# `strategy.yaml` (score.weights) viven los de producción: 85 + 15, los mismos. El
# score no puede medir sobre una escala que el YAML no conoce.
#
# `killzone` tiene peso aquí porque en este proyecto la ventana SÍ puntúa (D-007,
# "Score = SMC + killzone"): dentro de ventana vale 1.0, fuera 0.25. No es un gate
# aparte —darle puntos Y exigirla como AND duro contaría la misma variable dos
# veces— y por eso `core/setup_gate.py` no la vuelve a exigir.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "smc": 85.0,
    "killzone": 15.0,
}

DEFAULT_GRADE_THRESHOLDS: Dict[str, float] = {
    "alta": 80.0,
    "media": 60.0,
}

# Ventanas UTC. Deben coincidir con `strategy.yaml`, que es la fuente de verdad
# (07:00-10:00 Londres / 12:30-15:30 Nueva York). El score razona en UTC, no en la
# hora del broker: es lo que ve el gráfico y lo que lee el agente.
DEFAULT_KILLZONES: List[Dict[str, str]] = [
    {"name": "Londres", "start": "07:00", "end": "10:00"},
    {"name": "Nueva York", "start": "12:30", "end": "15:30"},
]

VERDICT_ALTA = "ALTA_PROBABILIDAD"
VERDICT_MEDIA = "MEDIA_PROBABILIDAD"
VERDICT_SIN = "SIN_OPERATIVA"


def _h_as_min(t: dtime) -> int:
    return t.hour * 60 + t.minute


def _parse_hhmm(value: str) -> Optional[dtime]:
    try:
        h, m = str(value).strip().split(":")
        return dtime(hour=int(h), minute=int(m))
    except (ValueError, AttributeError):
        return None


def killzone_score(windows: Optional[List[Dict[str, str]]] = None,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Valor del componente killzone: 1.0 dentro de una ventana, 0.25 fuera.

    Ventanas UTC como [{"name","start","end"}] con HH:MM. `now` se usa por
    parámetro (pureza/testeable); si no se pasa, se toma datetime.now(timezone.utc).
    """
    wins = windows if windows is not None else DEFAULT_KILLZONES
    dt = now if now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    t = dtime(hour=dt.hour, minute=dt.minute, second=dt.second)
    tmin = _h_as_min(t)

    for w in wins:
        start = _parse_hhmm(w.get("start", ""))
        end = _parse_hhmm(w.get("end", ""))
        if start is None or end is None:
            continue
        smin, emin = _h_as_min(start), _h_as_min(end)
        inside = (smin <= tmin < emin)
        if inside:
            return {
                "value": 1.0,
                "in_killzone": True,
                "name": w.get("name") or None,
                "detail": f"Dentro de la ventana {w.get('name') or 'killzone'} (UTC).",
            }
    return {
        "value": 0.25,
        "in_killzone": False,
        "name": None,
        "detail": "Fuera de killzone (UTC): el componente puntúa parcial.",
    }


def _bull_keys(patterns: Dict[str, Any], direction: str) -> Dict[str, Any]:
    """Claves de las zonas a favor de la dirección, más la contrapartida.

    Para un BUY la confluencia es FVG/OB alcistas y el sweep del PDL (barrido por
    debajo que cierra de vuelta dentro); para un SELL, el espejo.
    """
    bullish_side = str(direction).upper() == "BUY"
    fvg_key = "BULLISH_FVG" if bullish_side else "BEARISH_FVG"
    ob_key = "BULLISH_OB" if bullish_side else "BEARISH_OB"
    sweep_key = "PDL_SWEEP" if bullish_side else "PDH_SWEEP"
    opposed_fvg = "BEARISH_FVG" if bullish_side else "BULLISH_FVG"
    opposed_ob = "BEARISH_OB" if bullish_side else "BULLISH_OB"

    fvgs = [z for z in patterns.get("fvgs", []) if z.get("type") == fvg_key]
    obs = [z for z in patterns.get("order_blocks", []) if z.get("type") == ob_key]
    sweeps = [s for s in patterns.get("sweeps", []) if s.get("type") == sweep_key]
    opposed = (
        len([z for z in patterns.get("fvgs", []) if z.get("type") == opposed_fvg])
        + len([z for z in patterns.get("order_blocks", []) if z.get("type") == opposed_ob])
    )
    return {"fvgs": fvgs, "obs": obs, "sweeps": sweeps, "opposed": opposed,
            "sweep_key": sweep_key}


def smc_component(patterns: Optional[Dict[str, Any]], direction: str) -> Dict[str, Any]:
    """Confluencia SMC: FVG + Order Block + Liquidity Sweep a favor de la dirección."""
    pat = patterns or {}
    if not any(k in pat for k in ("fvgs", "order_blocks", "sweeps")):
        return {"value": 0.0, "detail": "Sin estructura SMC."}

    k = _bull_keys(pat, direction)
    has_fvg, has_ob, has_sweep = bool(k["fvgs"]), bool(k["obs"]), bool(k["sweeps"])
    if has_fvg and has_ob and has_sweep:
        value, why = 1.0, "FVG + OB + Sweep a favor"
    elif has_fvg and (has_ob or has_sweep):
        value, why = 0.7, ("FVG + " + ("OB" if has_ob else "Sweep") + " a favor")
    elif has_fvg or has_ob or has_sweep:
        value, why = 0.5, ("OB" if has_ob else ("FVG" if has_fvg else str(k["sweep_key"]))) + " a favor"
    else:
        value, why = 0.15, "Sin confluencia a favor"

    if has_sweep and k["opposed"] > 0:
        value = max(0.0, value - 0.1)

    return {
        "value": round(value, 2),
        "detail": why + f" (contrapartida estructural: {k['opposed']}).",
    }


def grade_score(score: float) -> str:
    if score >= DEFAULT_GRADE_THRESHOLDS["alta"]:
        return VERDICT_ALTA
    if score >= DEFAULT_GRADE_THRESHOLDS["media"]:
        return VERDICT_MEDIA
    return VERDICT_SIN


def invalidation_level(patterns: Optional[Dict[str, Any]], direction: str) -> Optional[float]:
    """Nivel de invalidez estructural: para BUY el menor bottom de las zonas alcistas
    vivas (FVG/OB); para SELL el mayor top de las bajistas. None sin estructura."""
    pat = patterns or {}
    if str(direction).upper() == "BUY":
        lows = [float(z["bottom"]) for z in pat.get("fvgs", []) if z.get("type") == "BULLISH_FVG"]
        lows += [float(z["bottom"]) for z in pat.get("order_blocks", []) if z.get("type") == "BULLISH_OB"]
        return min(lows) if lows else None
    highs = [float(z["top"]) for z in pat.get("fvgs", []) if z.get("type") == "BEARISH_FVG"]
    highs += [float(z["top"]) for z in pat.get("order_blocks", []) if z.get("type") == "BEARISH_OB"]
    return max(highs) if highs else None


def _resolve_weights(weights: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Pesos efectivos: DEFAULT_WEIGHTS sobrepuestos por los de la config."""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if v is not None})
    return w


def component_share(weights: Optional[Dict[str, float]] = None,
                    key: str = "killzone") -> float:
    """Cuánto del score representa un componente, como fracción 0-1. PURO.

    El score es `sum(valor*peso)/total*100`, así que la cuota real de un componente
    es `peso/total` y cambia en cuanto cambia cualquier otro peso del YAML. Un
    literal "20% del score" en la UI miente en cuanto el YAML se toca.
    """
    w = _resolve_weights(weights)
    total = sum(max(0.0, v) for v in w.values())
    if not total or key not in w:
        return 0.0
    return max(0.0, float(w[key])) / total


def setup_score(inputs: Dict[str, Any],
                weights: Optional[Dict[str, float]] = None,
                killzones: Optional[List[Dict[str, str]]] = None,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """Setup Score 0-100 con breakdown de los componentes.

    inputs:
      direction  ("BUY"/"SELL")
      patterns   {"fvgs","order_blocks","sweeps",...} o None
    weights: {"smc","killzone"} en puntos; si no suman 100 se normalizan al total.
    """
    w = _resolve_weights(weights)
    total_w = sum(max(0.0, v) for v in w.values()) or 1.0

    direction = str(inputs.get("direction") or "BUY").upper()
    kz = killzone_score(killzones, now)
    smc = smc_component(inputs.get("patterns"), direction)

    comps = {
        "smc": {**smc, "weight": w.get("smc", 0.0)},
        "killzone": {"value": kz["value"], "weight": w.get("killzone", 0.0),
                     "detail": kz["detail"], "in_killzone": kz["in_killzone"],
                     "name": kz["name"]},
    }
    score = sum(c["value"] * c["weight"] for c in comps.values()) / total_w * 100.0

    return {
        "score": round(score, 1),
        "verdict": grade_score(score),
        "components": comps,
        "weight_total": round(total_w, 1),
        "computed_at": (now or datetime.now(timezone.utc)).isoformat(),
    }


def score_directions(patterns: Optional[Dict[str, Any]],
                     weights: Optional[Dict[str, float]] = None,
                     killzones: Optional[List[Dict[str, str]]] = None,
                     now: Optional[datetime] = None) -> Dict[str, Any]:
    """El bloque `risk_engine` de un snapshot: score de BUY y SELL + invalidez.

    Devuelve `{"bull": {...}, "bear": {...}, "invalidation": {"BUY","SELL"}}`, que
    es exactamente lo que consume `core/setup_gate.evaluate_gate`. Se calculan las
    DOS direcciones de una vez para que el gate elija por score y no por la
    dirección que alguien fijó de antemano.
    """
    salida: Dict[str, Any] = {}
    for lado, direccion in (("bull", "BUY"), ("bear", "SELL")):
        salida[lado] = setup_score(
            {"direction": direccion, "patterns": patterns}, weights, killzones, now
        )
    salida["invalidation"] = {
        "BUY": invalidation_level(patterns, "BUY"),
        "SELL": invalidation_level(patterns, "SELL"),
    }
    return salida


def validate_entry(
    entry: float,
    sl: float,
    target: Optional[float],
    direction: str,
    current_price: Optional[float] = None,
    invalidate_level: Optional[float] = None,
    created_at: Optional[float] = None,
    now: Optional[float] = None,
    cfg: Optional[Dict[str, Any]] = None,
    balance: Optional[float] = None,
    risk_pct: Optional[float] = None,
    loss_per_lot: Optional[float] = None,
    lot: Optional[float] = None,
) -> Dict[str, Any]:
    """Valida la entrada antes de ejecutar: R:R mínimo, invalidez estructural y TTL.

    - R:R: exige (|target-entry| / |entry-sl|) >= min_rr (default 2.0).
    - Invalidez: si current_price cruzó invalidate_level (BUY <= nivel, SELL >= nivel)
      el setup murió y se rechaza.
    - TTL: si now - created_at > ttl_minutes el setup expiró.
    - Cabeza de riesgo: con loss_per_lot y lot verifica que la pérdida máxima no
      exceda balance*risk_pct/100.
    """
    c = dict(cfg or {})
    min_rr = float(c.get("min_rr") or 2.0)
    ttl_min = float(c.get("setup_ttl_minutes") or 45.0)
    now_ts = now if now is not None else datetime.now(timezone.utc).timestamp()
    direction = str(direction).upper()

    rejected = False
    reasons: List[str] = []

    if sl is None or entry is None:
        rejected = True
        reasons.append("Faltan entry/SL para validar.")
    else:
        dist_s = abs(float(entry) - float(sl))
        if target is not None and dist_s > 0:
            rr = abs(float(target) - float(entry)) / dist_s
            if rr + 1e-9 < min_rr:
                rejected = True
                reasons.append(f"R:R {rr:.2f} < mínimo {min_rr:.2f}.")

    if invalidate_level is not None and current_price is not None:
        if direction == "BUY" and current_price <= float(invalidate_level):
            rejected = True
            reasons.append(
                f"Estructura rota: el precio {current_price} cruzó el nivel de "
                f"invalidez alcista {invalidate_level}."
            )
        elif direction == "SELL" and current_price >= float(invalidate_level):
            rejected = True
            reasons.append(
                f"Estructura rota: el precio {current_price} cruzó el nivel de "
                f"invalidez bajista {invalidate_level}."
            )

    if created_at is not None:
        expirado = (now_ts - creado_ts) > ttl_min * 60.0 if (
            creado_ts := _as_epoch(created_at)
        ) is not None else False
        if expirado:
            rejected = True
            reasons.append(
                f"Setup expirado: supera el TTL de {ttl_min:.0f} min."
            )

    if loss_per_lot is not None and lot is not None and balance is not None and risk_pct:
        max_risk = float(balance) * float(risk_pct) / 100.0
        loss = abs(float(loss_per_lot)) * float(lot)
        if loss > max_risk * (1.0 + 1e-9):
            rejected = True
            reasons.append(
                f"Riesgo ${loss:.2f} excede el presupuesto ${max_risk:.2f} "
                f"({risk_pct}%)."
            )

    return {
        "approved": not rejected,
        "rejected": rejected,
        "reasons": reasons,
        "entry": entry,
        "sl": sl,
        "target": target,
        "direction": direction,
    }


def _as_epoch(value: Any) -> Optional[float]:
    """Un instante a epoch (segundos). Acepta epoch, datetime o ISO 8601."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    try:
        texto = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def effective_risk_pct(score: Optional[float] = None,
                       verdict: Optional[str] = None,
                       cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Riesgo efectivo (% del balance) con el que se dimensiona el lote.

    La regla, explícita:
      - sin score ni verdict (ruta manual) -> riesgo NOMINAL, y se declara que es
        nominal, porque quien pulsa el botón no ha pedido un score.
      - verdict ALTA_PROBABILIDAD -> riesgo nominal.
      - cualquier otro verdict -> riesgo reducido (`reduced_risk_pct`, o la mitad
        del nominal si no está configurado).

    Módulo puro: no lee ficheros ni el reloj. Devuelve el desglose para que la UI
    pueda declararlo en vez de dejar que se suponga.
    """
    c = dict(cfg or {})
    nominal = float(c.get("risk_pct") or 0.0)
    reducido = c.get("reduced_risk_pct")
    reducido = float(reducido) if reducido is not None else nominal / 2.0
    v = str(verdict or "").upper()

    if not v:
        valor = nominal
        reason = "Sin score: riesgo nominal (ruta manual)."
    elif v == VERDICT_ALTA:
        valor = nominal
        reason = f"Verdict {v}: riesgo nominal."
    else:
        valor = reducido
        reason = f"Verdict {v}: riesgo reducido."

    return {
        "risk_pct": round(valor, 4),
        "nominal_pct": round(nominal, 4),
        "reduced_pct": round(reducido, 4),
        "reduced": valor != nominal,
        "verdict": v or None,
        "score": score,
        "reason": reason,
        "label": f"riesgo {valor:g}% "
                 f"({'reducido' if valor != nominal else 'nominal'}"
                 f"{'' if v else ', sin score'})",
    }


__all__ = [
    "DEFAULT_GRADE_THRESHOLDS",
    "DEFAULT_KILLZONES",
    "DEFAULT_WEIGHTS",
    "VERDICT_ALTA",
    "VERDICT_MEDIA",
    "VERDICT_SIN",
    "component_share",
    "effective_risk_pct",
    "grade_score",
    "invalidation_level",
    "killzone_score",
    "score_directions",
    "setup_score",
    "smc_component",
    "validate_entry",
]

"""Calidad de ejecución: lo que la decisión NO controla y el dinero sí paga.

Módulo PURO, misma disciplina que `core.risk_engine` y `core.setup_gate`: sin
MT5, sin app, sin store, sin red. Recibe precios ya resueltos y devuelve números,
para poder probarlo entero sin terminal.

**El hueco que tapa.** El gate de entrada (`risk_engine.validate_entry`) valida el
R:R sobre los precios de la orden, y esos precios ya son `ask`/`bid`: el spread
está *dentro* de la entrada, así que el R:R sale bien. Pero hay dos factores que ni
el score ni el R:R miran, y los dos se pagan en dinero:

1. **La deriva de la entrada.** El gate justifica el trade en un NIVEL (el borde de
   un FVG, el cierre de un order block): "compro aquí porque hay descuento". Una
   orden a mercado no entra en ese nivel, entra al precio del instante. Si entre el
   gate y el envío el precio se ha movido, la tesis se ha desplazado: en un BUY,
   entrar por encima del nivel que justificaba la compra es exactamente lo contrario
   de lo que se aprobó. Con una deriva de la mitad del stop, el nivel que motivó la
   orden ya está medio stop más lejos, y el R:R sobre el papel sigue siendo 2.0.

2. **La distancia a la invalidez.** `validate_entry` comprueba si el precio YA cruzó
   el nivel de invalidez, pero no si queda a un centímetro de él. Una entrada con el
   90 % de su recorrido antes de que la estructura muera no es una entrada con buen
   ratio: es una entrada con casi todo el riesgo concentrado en el primer tick.

Los dos son diferencias entre dos números que el sistema YA tenía y no miraba: el
precio que el gate planeó y el nivel de invalidez. Este módulo los mide, y el
servicio de ejecución (M5) los usa como puerta antes de llamar a `order_send`.

**De AVISO a PUERTA.** En el proyecto de referencia este módulo existía pero el gate
de setup lo usaba solo como AVISO: medía y no rechazaba. Aquí, en M5, `evaluate` SÍ
rechaza (`approved=False`): una orden cuyo llenado invalida lo que el gate aprobó no
se manda, porque el operador que confirma (D-004) no puede re-evaluar a mano la
deriva en el tick exacto del envío. Se conserva que un dato no medible NO rechaza.

**Lo que NO hace**: no puntúa, no toca el score, no decide el lote y no predice. Solo
dice si las condiciones de llenado invalidan lo que el gate aprobó, y lo dice con la
medida a la vista para que el motivo sea auditable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Umbrales por defecto (se sobrescriben desde strategy.yaml / trading config)
# ---------------------------------------------------------------------------

# La deriva de la entrada no puede superar esta fracción de la distancia de SL.
# 0.5 = "el nivel que justificaba el trade puede estar como mucho medio stop más
# lejos". Es la lectura correcta del riesgo: a partir de ahí, lo que se está
# comprando no es el setup que se aprobó.
DEFAULT_MAX_SLIPPAGE_SL_SHARE = 0.5

# El spread no puede superar esta fracción de la distancia de SL. 0.25 = un cuarto
# del stop se va en la entrada antes de que el trade pueda trabajar.
DEFAULT_MAX_SPREAD_SHARE_OF_SL = 0.25

# La entrada debe quedar al menos a esta fracción del SL del nivel de invalidez.
# 0.25 = al menos un cuarto del recorrido antes de que la estructura declare muerto
# el setup. Por debajo, el riesgo está casi entero en el primer tick aunque el R:R
# sobre el papel sea perfecto.
DEFAULT_MIN_INVALIDATION_SL_SHARE = 0.25

# Tolerancia de las COMPARACIONES con umbral, no de las medidas. Existe porque las
# fracciones salen de restas y divisiones de coma flotante: media distancia de SL
# dividida por la distancia de SL da 0.500000000000167, no 0.5. Sin esta tolerancia
# un setup exactamente en el límite se rechaza o se acepta según el último bit, y un
# gate cuyo resultado en el borde no se puede reproducir es peor que no tener gate.
# Es 1e-9: cuatro órdenes de magnitud por debajo de cualquier diferencia que una
# persona pueda decidir, y muy por encima del ruido de la aritmética.
EPSILON = 1e-9

DIRECTION_LONG = "BUY"
DIRECTION_SHORT = "SELL"

# Códigos de motivo. Se exportan porque quien los LEA (el watcher los vuelve a
# expresar en pips, la auditoría los cuenta por tipo) tiene que poder distinguir las
# reglas sin parsear prosa: un motivo es texto para el operador y un código para la
# máquina, y el texto puede cambiar sin romper el conteo.
REASON_INVALIDEZ = "INVALIDEZ_ENTRADA"
REASON_DERIVA = "DERIVA_ENTRADA"
REASON_SPREAD = "SPREAD_sobre_SL"


def reason_code(reason: str) -> str:
    """Código de un motivo: 'SPREAD_sobre_SL: ...' -> 'SPREAD_sobre_SL'."""
    return str(reason or "").split(":", 1)[0].strip()


def _num(value: Any) -> Optional[float]:
    """float() o None, y NUNCA 0.0 en lugar de un hueco.

    El 0 SÍ es un número medido —un spread de 0 puntos, una deriva de 0 pips— y por
    eso se conserva: confundirlo con "no medido" haría que una entrada exactamente en
    el nivel planificado pareciera un dato que falta. Lo que sí es un hueco es None,
    un texto no numérico, o un bool (que en Python es un int y colaría como 0 o 1 sin
    querer).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _sign(direction: str) -> Optional[int]:
    d = str(direction or "").strip().upper()
    if d == DIRECTION_LONG:
        return 1
    if d == DIRECTION_SHORT:
        return -1
    return None


def spread_points(bid: Optional[float], ask: Optional[float],
                  point: Optional[float]) -> Optional[float]:
    """ask - bid en puntos del símbolo. None si falta algo o si la cita no cierra."""
    b, a, p = _num(bid), _num(ask), _num(point)
    if b is None or a is None or p is None:
        return None
    if p <= 0 or b <= 0 or a <= 0:
        return None
    sp = (a - b) / p
    if sp < 0:
        return None
    return sp


def measure(entry: Optional[float],
            sl: Optional[float],
            tp: Optional[float],
            direction: str,
            point: Optional[float],
            planned_entry: Optional[float] = None,
            sl_distance: Optional[float] = None,
            invalidate_level: Optional[float] = None,
            bid: Optional[float] = None,
            ask: Optional[float] = None) -> Dict[str, Any]:
    """Mide las condiciones de llenado. NUNCA lanza. NO dictamina.

    Devuelve un dict con las medidas crudas (fracciones incluidas) y deja el
    veredicto a `evaluate`. Un dato que no se puede medir sale como `None`; el
    denominador de las fracciones es la distancia de SL PLANEADA si se conoce, y si
    no la efectiva (`|entry - sl|`). Ver `evaluate` para el porqué del planeado.
    """
    e, s, t = _num(entry), _num(sl), _num(tp)
    pt = _num(point)
    d = _sign(direction)
    nd = str(direction or "").strip().upper() or None

    sl_dist = abs(e - s) if (e is not None and s is not None) else None
    tp_dist = abs(t - e) if (t is not None and e is not None) else None
    inv = _num(invalidate_level)
    planned = _num(planned_entry)

    # Distancia de referencia: la que el gate presupuestó. Cae a la efectiva si no
    # se conoce (o no es positiva).
    dist = _num(sl_distance)
    ref_dist = dist if (dist and dist > 0) else sl_dist

    sp_pts = spread_points(bid, ask, pt)
    sp_price = sp_pts * pt if (sp_pts is not None and pt) else None

    out: Dict[str, Any] = {
        "direction": nd,
        "entry": e,
        "planned_entry": planned,
        "sl": s,
        "tp": t,
        "point": pt,
        "sl_distance": sl_dist,
        "tp_distance": tp_dist,
        "planned_sl_distance": dist,
        "ref_sl_distance": ref_dist,
        "invalidate_level": inv,
        "rr_actual": (tp_dist / sl_dist) if (tp_dist is not None and sl_dist) else None,
        "spread_points": sp_pts,
        "spread_price": sp_price,
    }

    if d is not None and e is not None and planned is not None:
        # Deriva con signo: positiva si el llenado es PEOR que lo planificado (BUY
        # por encima, SELL por debajo). Es lo que permite comparar los dos lados con
        # el mismo criterio.
        out["slippage_price"] = e - planned
        out["slippage_adverse_price"] = (e - planned) * d
    else:
        out["slippage_price"] = None
        out["slippage_adverse_price"] = None

    slip = out["slippage_adverse_price"]
    out["slippage_share_of_sl"] = (
        abs(slip) / ref_dist if (slip is not None and ref_dist) else None
    )
    out["spread_share_of_sl"] = (
        sp_price / ref_dist if (sp_price is not None and ref_dist) else None
    )

    if d is not None and e is not None and inv is not None:
        signed = (e - inv) * d
        out["invalidation_distance"] = signed
        out["invalidation_share_of_sl"] = (
            signed / ref_dist if ref_dist else None
        )
    else:
        out["invalidation_distance"] = None
        out["invalidation_share_of_sl"] = None

    return out


def evaluate(entry: Optional[float],
             sl: Optional[float],
             tp: Optional[float],
             direction: str,
             point: Optional[float],
             planned_entry: Optional[float] = None,
             sl_distance: Optional[float] = None,
             invalidate_level: Optional[float] = None,
             bid: Optional[float] = None,
             ask: Optional[float] = None,
             cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Mide y dictamina. Clave: `approved` False con `reasons` explicados.

    Tres reglas, en orden de importancia para el dinero:

    1. `INVALIDEZ_ENTRADA`: el llenado queda más cerca del nivel que mata el setup
       que del recorrido que le da margen (fracción del SL menor que el mínimo).
    2. `DERIVA_ENTRADA`: el llenado se ha ido demasiado lejos, EN CONTRA, del nivel
       planificado (fracción del SL por encima del máximo).
    3. `SPREAD_sobre_SL`: el spread se ha comido demasiado del stop.

    **El epsilon de los umbrales.** Las fracciones salen de dividir números de coma
    flotante: media distancia de SL dividida por la distancia de SL da
    0.500000000000167, no 0.5. Sin tolerancia, un setup exactamente EN el límite se
    rechaza o se acepta según el último bit, y un gate así —irreproducible en el
    borde— no es un gate. `EPSILON` se aplica a la COMPARACIÓN con el umbral, nunca a
    la medida que se publica.

    **El denominador PLANEADO.** Las tres fracciones se miden contra la distancia de
    SL que el gate presupuestó (`sl_distance`), no contra `|entry - sl|`. Es la
    diferencia entre una regla y una letra muerta: si el denominador fuera la
    geometría ya desplazada, una deriva de medio stop daría `0.5/1.5 = 0.33` y solo
    superaría un umbral de 0.5 tras moverse un stop ENTERO, cuando la constante dice
    "medio stop". Además el lote se dimensiona con la distancia planificada, así que
    el denominador planificado es también el del riesgo real.
    """
    c = dict(cfg or {})
    m = measure(entry=entry, sl=sl, tp=tp, direction=direction, point=point,
                planned_entry=planned_entry, sl_distance=sl_distance,
                invalidate_level=invalidate_level, bid=bid, ask=ask)

    max_slip = float(c.get("max_slippage_sl_share", DEFAULT_MAX_SLIPPAGE_SL_SHARE))
    max_spread = float(c.get("max_spread_share_of_sl", DEFAULT_MAX_SPREAD_SHARE_OF_SL))
    min_inv = float(c.get("min_invalidation_sl_share", DEFAULT_MIN_INVALIDATION_SL_SHARE))

    reasons: List[str] = []
    rejected = False
    unknown: List[str] = []

    if m["direction"] not in (DIRECTION_LONG, DIRECTION_SHORT):
        unknown.append("direccion")
    if m["sl_distance"] is None or m["sl_distance"] <= 0:
        # Sin geometría no hay nada que juzgar; que lo handle validate_entry.
        m["approved"] = True
        m["verdict"] = "sin_geometria"
        m["reasons"] = []
        m["unknown"] = ["sl_distance"]
        return m

    # 1. Invalidez alcanzable antes que el stop
    share = m["invalidation_share_of_sl"]
    if share is None:
        unknown.append("invalidation_level")
    elif share < min_inv - EPSILON:
        rejected = True
        reasons.append(
            "%s: la entrada queda a %.0f%% del SL del nivel que "
            "invalida el setup (minimo %.0f%%); el riesgo se concentra en el "
            "primer tick." % (REASON_INVALIDEZ, share * 100, min_inv * 100)
        )

    # 2. Deriva de la entrada
    slip = m["slippage_share_of_sl"]
    if slip is None:
        if planned_entry is None:
            unknown.append("planned_entry")
    elif slip > max_slip + EPSILON:
        rejected = True
        if m["slippage_adverse_price"] and m["slippage_adverse_price"] > 0:
            reasons.append(
                "%s: la entrada se ha ido %.0f pips en contra "
                "(%.0f%% del SL, maximo %.0f%%); el nivel que justificaba el "
                "setup ya no es este." % (
                    REASON_DERIVA,
                    (m["slippage_adverse_price"] / m["point"]) if m["point"] else 0.0,
                    slip * 100, max_slip * 100)
            )
        else:
            reasons.append(
                "%s: la entrada se ha ido %.0f%% del SL respecto al "
                "nivel planificado (maximo %.0f%%)." % (
                    REASON_DERIVA, slip * 100, max_slip * 100)
            )

    # 3. Spread sobre el stop
    sp = m["spread_share_of_sl"]
    if sp is None:
        unknown.append("spread")
    elif sp > max_spread + EPSILON:
        rejected = True
        reasons.append(
            "%s: el spread es el %.0f%% de la distancia de SL (maximo %.0f%%)." % (
                REASON_SPREAD, sp * 100, max_spread * 100)
        )

    if rejected:
        verdict = "rechazado"
    elif unknown:
        verdict = "sin_medir"
    else:
        verdict = "ok"

    m["approved"] = not rejected
    m["verdict"] = verdict
    m["reasons"] = reasons
    m["unknown"] = unknown
    m["thresholds"] = {
        "max_slippage_sl_share": max_slip,
        "max_spread_share_of_sl": max_spread,
        "min_invalidation_sl_share": min_inv,
    }
    return m


__all__ = [
    "DEFAULT_MAX_SLIPPAGE_SL_SHARE",
    "DEFAULT_MAX_SPREAD_SHARE_OF_SL",
    "DEFAULT_MIN_INVALIDATION_SL_SHARE",
    "DIRECTION_LONG",
    "DIRECTION_SHORT",
    "EPSILON",
    "REASON_DERIVA",
    "REASON_INVALIDEZ",
    "REASON_SPREAD",
    "evaluate",
    "measure",
    "reason_code",
    "spread_points",
]

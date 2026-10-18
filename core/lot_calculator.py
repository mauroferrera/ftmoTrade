"""Cálculo del tamaño de posición (lotes) para los símbolos del alcance.

Modulo PURO, como el resto de `core/`: no lee la cuenta, no habla con el bróker y no
importa `adapters`. Recibe el `SymbolSpec` (o los cuatro números que hacen falta) y el
riesgo en dinero, y devuelve los lotes. Los datos de la cuenta los pone quien llama.

Por qué vive aquí y no en el servicio de ejecución
--------------------------------------------------
En REF el dimensionado vivía mezclado con la ejecución de órdenes y con lógica de los
mercados brasileños (B3), que quedaron fuera de alcance. La parte que sobrevive es un
único cálculo de dinero: cuánto vale un movimiento de una unidad de precio para un lote,
cuántos lotes cubren el riesgo permitido sin pasarse, y cómo redondear hacia abajo al
`volume_step` del símbolo porque pasarse del riesgo es el único error que no se corrige.

Lo que este módulo NO hace
--------------------------
- No consulta el bróker ni la tasa de cambio. Los símbolos del alcance (EURUSD y
  XAUUSD, cotizados en USD con cuenta en USD) no necesitan conversión; para otros
  mercados el cálculo está a un `SymbolSpec` de distancia pero no se pretende cubrirlos.
- No decide si se opera. El riesgo por operación, el máximo de operaciones del día y el
  gate del setup son de `core.risk`/`core.setup_gate`; aquí solo se traduce dinero a
  lotes.

Siempre hacia abajo
-------------------
`math.floor` sobre el número de `volume_step`, nunca `round`: redondear hacia arriba
pondría el riesgo por encima del permitido. Si el cálculo no llega ni al `volume_min`
del símbolo, se devuelve `clamped_to_min=True` con el mínimo, para que quien llama sepa
que el riesgo real excede el presupuesto y pueda rechazarlo en vez de operandolo en
silencio.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Any, Dict, Optional

#: Lote mínimo por defecto cuando el símbolo no lo declara. 0.01 es el mínimo estándar
#: de las cuentas de forex retail y el que usan los símbolos del alcance.
MIN_LOT = 0.01


@dataclass(frozen=True)
class LotSpec:
    """Los números que hacen falta para dimensionar un símbolo.

    Se construye a partir del `SymbolSpec` con `lot_spec_from_symbol`; aquí no se
    importa `adapters` para poder testear el cálculo con valores inventados.
    """
    name: str = ""
    contract_size: float = 0.0
    tick_size: float = 0.0
    tick_value: float = 0.0
    digits: int = 5
    volume_min: float = MIN_LOT
    volume_max: float = 0.0
    volume_step: float = MIN_LOT


def _num(value: Any) -> Optional[float]:
    """A `float`, o `None` si no es convertible. Nunca lanza."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def lot_spec_from_symbol(spec: Any) -> LotSpec:
    """Construye un `LotSpec` desde un `SymbolSpec` (o algo parecido) por `getattr`.

    Cada campo cae a su valor por defecto si el símbolo no lo trae. No se importa
    `SymbolSpec` aquí: `core` no depende de `adapters`.
    """
    name = getattr(spec, "symbol", "") or getattr(spec, "name", "") or ""
    contract = _num(getattr(spec, "contract_size", None))
    tick_size = _num(getattr(spec, "tick_size", None))
    tick_value = _num(getattr(spec, "tick_value", None))
    vmin = _num(getattr(spec, "volume_min", None))
    vmax = _num(getattr(spec, "volume_max", None))
    vstep = _num(getattr(spec, "volume_step", None))
    digits = getattr(spec, "digits", None)
    return LotSpec(
        name=str(name),
        contract_size=contract if contract and contract > 0 else 0.0,
        tick_size=tick_size if tick_size and tick_size > 0 else 0.0,
        tick_value=tick_value if tick_value and tick_value > 0 else 0.0,
        digits=int(digits) if isinstance(digits, int) else 5,
        volume_min=vmin if vmin and vmin > 0 else MIN_LOT,
        volume_max=vmax if vmax and vmax > 0 else 0.0,
        volume_step=vstep if vstep and vstep > 0 else MIN_LOT,
    )


def risk_per_lot(sl_distance: Optional[float], spec: LotSpec) -> Optional[float]:
    """Pérdida en dinero por un lote si el precio recorre `sl_distance`.

    Se mide en valor de tick: `(sl_distance / tick_size) * tick_value`, que es lo que
    publica el bróker y no depende de en cuántas "unidades" se mida el símbolo. Si el
    bróker no publica `tick_size`/`tick_value`, se cae a `sl_distance * contract_size`
    (lineal en precio, válido para los símbolos cotizados en la divisa de la cuenta).
    `None` si no hay ninguna de las dos vías: un 0.0 haría parecer que un lote no
    arriesga nada.
    """
    d = _num(sl_distance)
    if d is None or d <= 0:
        return None
    ts = _num(spec.tick_size)
    tv = _num(spec.tick_value)
    if ts and ts > 0 and tv and tv > 0:
        return round((d / ts) * tv, 8)
    cs = _num(spec.contract_size)
    if cs and cs > 0:
        return round(d * cs, 8)
    return None


def _step_floor(lotes: float, step: float) -> float:
    """Redondea `lotes` hacia abajo al múltiplo de `step`, sin errores de coma flotante.

    `floor(x / step) * step` arrastra basura binaria (0.01 * 3 puede dar 0.03000000...);
    pasar por el entero de pasos y volver a dividir da el valor limpio.
    """
    if step <= 0:
        return lotes
    pasos = floor(lotes / step + 1e-9)
    return pasos * step


def standard_forex_lots(balance: Optional[float],
                        risk_pct: Optional[float],
                        sl_distance: Optional[float],
                        spec: Any) -> Dict[str, Any]:
    """Lotes para arriesgar `risk_pct` de `balance` con ese `sl_distance`.

    Devuelve `{lots, risk_money, risk_per_lot, clamped_to_min, clamped_to_max, spec}`.
    `lots` es `None` si faltan datos; si el cálculo no llega al mínimo del símbolo,
    `clamped_to_min=True` y se devuelve el mínimo.

    Fórmula: `riesgo_permitido / pérdida_por_lote`, redondeado hacia abajo al
    `volume_step`.
    """
    ls = spec if isinstance(spec, LotSpec) else lot_spec_from_symbol(spec)
    bal = _num(balance)
    pct = _num(risk_pct)
    per_lot = risk_per_lot(sl_distance, ls)

    resultado: Dict[str, Any] = {
        "lots": None,
        "risk_money": None,
        "risk_per_lot": per_lot,
        "clamped_to_min": False,
        "clamped_to_max": False,
        "spec": ls,
    }
    if bal is None or pct is None or bal <= 0 or pct <= 0 or per_lot is None:
        return resultado

    riesgo = bal * (pct / 100.0)
    resultado["risk_money"] = round(riesgo, 2)

    bruto = riesgo / per_lot
    lotes = _step_floor(bruto, ls.volume_step)

    if lotes < ls.volume_min:
        resultado["lots"] = round(ls.volume_min, 4)
        resultado["clamped_to_min"] = True
        return resultado

    if ls.volume_max and lotes > ls.volume_max:
        lotes = _step_floor(ls.volume_max, ls.volume_step)
        resultado["clamped_to_max"] = True

    resultado["lots"] = round(lotes, 4)
    return resultado


__all__ = [
    "MIN_LOT",
    "LotSpec",
    "lot_spec_from_symbol",
    "risk_per_lot",
    "standard_forex_lots",
]

"""Reglas de prop firm (FTMO) por cuenta: perfiles, topes y progreso.

Modulo PURO, como el resto de `core/`: no lee ficheros, no habla con el bróker y
no importa los adaptadores. Recibe el estado de la cuenta ya medido por quien
llama y devuelve el veredicto (bloqueado y por qué), el room que queda y el
progreso hacia el objetivo.

Por qué un módulo dedicado y no un medidor genérico
---------------------------------------------------
El proyecto de referencia tenía un `prop_firm_state` genérico: HWM diario y total
con denominador en el pico, más un tope de ganancia diaria como proxy de
consistencia, todo leído de claves planas. Ese modelo NO es el de FTMO:

  - FTMO mide la pérdida diaria contra el **balance de inicio de día** (el de las
    00:00 CE(S)T), no contra un pico intradía. Un pico que sube el suelo del día
    bloquea antes de lo que FTMO bloquea: es conservador, pero deja de ser la
    regla que se dice estar siguiendo.
  - FTMO tiene DOS productos con reglas distintas: 2-Step (DD total ESTÁTICO
    sobre el capital inicial, 4 días mínimos, sin consistencia) y 1-Step (DD total
    TRAILING sobre el balance EOD más alto, con **Best Day Rule**).
  - El tope de ganancia diaria no es una regla de FTMO: es al revés, el Best Day
    Rule limita el mejor día a <=50% de los días positivos, y solo en 1-Step.
  - FTMO se mide sobre el **buffer** (el room de DD total, $2.500 en un $25k), no
    sobre el balance actual, para dimensionar riesgo.

La regla vive aquí, junto y explícita, en vez de repartida como banderas de un
medidor genérico que tendría que conocer los productos de un proveedor concreto.

Qué NO decide este módulo
-------------------------
- No calcula lotes. Devuelve el DINERO en riesgo; `lot_calculator` (M4) lo
  convierte a tamaño con el spec del símbolo.
- No lee el reloj ni decide cuándo cambia el día: el caller pasa `day_key` y llama
  a `roll_day` en el cambio. Sin eso, un "día" aquí no significa nada.
- No es un edge ni una señal: el shell de disciplina reduce las pérdidas por
  indisciplina (~90% de los fallos), pero no convierte una estrategia sin ventaja
  en una con ella.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
    "FTMO_2STEP",
    "FTMO_1STEP",
    "PRODUCTS",
    "PropProfile",
    "ftmo_profile",
    "new_state",
    "evaluate",
    "roll_day",
    "buffer_remaining_usd",
    "daily_room_usd",
    "nominal_risk_usd",
    "buffer_risk_usd",
    "sizing_base_usd",
]

#: Producto FTMO de dos fases: Challenge (objetivo 10%) + Verification (5%).
FTMO_2STEP = "2step"

#: Producto FTMO de una fase: objetivo 10% con DD total trailing y Best Day Rule.
FTMO_1STEP = "1step"

#: Reglas de cada producto, EN PORCENTAJE DEL CAPITAL INICIAL. Son las de FTMO,
#: verificadas el 2026-10-14 (ftmo.com). Cambian con el tiempo y con la cuenta;
#: el dashboard de FTMO manda. Re-verificar antes de pagar.
PRODUCTS: Dict[str, Dict[str, Any]] = {
    FTMO_2STEP: {
        "daily_pct": 5.0,          # Maximum Daily Loss
        "total_pct": 10.0,         # Maximum Loss, ESTÁTICO sobre el capital inicial
        "total_trailing": False,
        "targets_pct": {"challenge": 10.0, "verification": 5.0},
        "min_trading_days": 4,
        "best_day_rule": False,
    },
    FTMO_1STEP: {
        "daily_pct": 3.0,
        "total_pct": 10.0,
        "total_trailing": True,    # sobre el balance EOD más alto
        "targets_pct": {"challenge": 10.0},
        "min_trading_days": 0,
        "best_day_rule": True,     # mejor día <= 50% de los días positivos
    },
}

#: Cortes internos por defecto, más estrechos que los de FTMO, para no morir en el
#: borde. 2-Step: 4% diario / 7% total. 1-Step: 2.5% diario / 7% total.
_INTERNAL_DEFAULT = {
    FTMO_2STEP: {"daily_pct": 4.0, "total_pct": 7.0},
    FTMO_1STEP: {"daily_pct": 2.5, "total_pct": 7.0},
}

#: Riesgo nominal por operación, en % del capital. 0.5% de $25.000 = $125, que es
#: el 5% del buffer ($2.500). Se dimensiona sobre el buffer, no sobre el balance.
RISK_PCT_PER_TRADE = 0.5

#: Fracción del buffer que se arriesga por operación cuando se dimensiona por
#: buffer. 5% de $2.500 = $125: la misma cifra que el 0.5% del capital.
BUFFER_RISK_FRACTION = 0.05

#: Tope del mejor día del Best Day Rule (1-Step), en fracción de los días positivos.
BEST_DAY_MAX_FRACTION = 0.5


class PropProfile:
    """Perfil de cuenta FTMO con sus topes, ya resueltos en divisa.

    Se construye desde `ftmo_profile` (producto + capital + ajustes) y no desde un
    dict suelto: los nombres de los campos son el contrato con el resto del
    sistema, y un dict mal escrito pasaría sin que nada lo notara.

    Los topes de FTMO y los INTERNOS se guardan por separado y con nombres que no
    se confunden: `ftmo_*` es lo que rompe la cuenta; `internal_*` es donde el
    shell para antes. Perseguir el tope de FTMO es la forma de rebasarlo.
    """

    __slots__ = (
        "product", "initial_capital",
        "ftmo_daily_pct", "ftmo_total_pct", "ftmo_total_trailing",
        "internal_daily_pct", "internal_total_pct",
        "target_pct", "target_usd",
        "ftmo_daily_usd", "ftmo_total_usd",
        "internal_daily_usd", "internal_total_usd",
        "min_trading_days", "best_day_rule", "phase",
        "risk_pct_per_trade", "risk_usd",
    )

    def __init__(
        self,
        product: str,
        initial_capital: float,
        *,
        ftmo_daily_pct: float,
        ftmo_total_pct: float,
        ftmo_total_trailing: bool,
        internal_daily_pct: float,
        internal_total_pct: float,
        target_pct: float,
        min_trading_days: int,
        best_day_rule: bool,
        phase: str = "challenge",
        risk_pct_per_trade: float = RISK_PCT_PER_TRADE,
    ):
        if initial_capital <= 0:
            raise ValueError(f"initial_capital debe ser > 0, recibido {initial_capital}")
        if target_pct <= 0:
            raise ValueError(f"target_pct debe ser > 0, recibido {target_pct}")
        if internal_daily_pct <= 0 or internal_total_pct <= 0:
            raise ValueError(
                f"los cortes internos deben ser > 0, recibidos "
                f"daily={internal_daily_pct} total={internal_total_pct}"
            )
        # Un corte interno que no es más estrecho que el de FTMO no protege de
        # nada: se alcanzaría el uno y el otro a la vez, o antes el de FTMO.
        if internal_daily_pct >= ftmo_daily_pct:
            raise ValueError(
                f"internal_daily_pct ({internal_daily_pct}) debe ser < ftmo "
                f"daily_pct ({ftmo_daily_pct}): el corte interno tiene que llegar "
                "antes que el de FTMO o no sirve de nada."
            )
        if internal_total_pct >= ftmo_total_pct:
            raise ValueError(
                f"internal_total_pct ({internal_total_pct}) debe ser < ftmo "
                f"total_pct ({ftmo_total_pct})."
            )

        self.product = str(product)
        self.initial_capital = float(initial_capital)
        self.ftmo_daily_pct = float(ftmo_daily_pct)
        self.ftmo_total_pct = float(ftmo_total_pct)
        self.ftmo_total_trailing = bool(ftmo_total_trailing)
        self.internal_daily_pct = float(internal_daily_pct)
        self.internal_total_pct = float(internal_total_pct)
        self.target_pct = float(target_pct)
        self.min_trading_days = int(min_trading_days)
        self.best_day_rule = bool(best_day_rule)
        self.phase = str(phase)
        self.risk_pct_per_trade = float(risk_pct_per_trade)

        cap = self.initial_capital
        self.ftmo_daily_usd = round(cap * self.ftmo_daily_pct / 100.0, 4)
        self.ftmo_total_usd = round(cap * self.ftmo_total_pct / 100.0, 4)
        self.internal_daily_usd = round(cap * self.internal_daily_pct / 100.0, 4)
        self.internal_total_usd = round(cap * self.internal_total_pct / 100.0, 4)
        self.target_usd = round(cap * self.target_pct / 100.0, 4)
        self.risk_usd = round(cap * self.risk_pct_per_trade / 100.0, 4)

    @property
    def buffer_usd(self) -> float:
        """El room de DD total de FTMO: la base sobre la que se dimensiona."""
        return self.ftmo_total_usd

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return (
            f"PropProfile({self.product!r}, capital={self.initial_capital}, "
            f"phase={self.phase!r}, target={self.target_usd}, "
            f"internal={self.internal_daily_usd}/{self.internal_total_usd})"
        )


def ftmo_profile(
    product: str,
    initial_capital: float,
    *,
    phase: str = "challenge",
    internal_daily_pct: Optional[float] = None,
    internal_total_pct: Optional[float] = None,
    risk_pct_per_trade: float = RISK_PCT_PER_TRADE,
) -> PropProfile:
    """Construye el perfil de una cuenta FTMO.

    `product` es `"2step"` o `"1step"`. `phase` solo aplica a 2-Step
    (`"challenge"` o `"verification"`), donde el objetivo cambia de 10% a 5%; en
    1-Step la única fase válida es `"challenge"`.

    Los cortes internos se pueden afinar; por defecto salen de `_INTERNAL_DEFAULT`
    y el constructor los rechaza si no son más estrechos que los de FTMO.
    """
    reglas = PRODUCTS.get(str(product))
    if reglas is None:
        raise ValueError(
            f"producto FTMO desconocido: {product!r}. Productos: {sorted(PRODUCTS)}"
        )
    targets = reglas["targets_pct"]
    if phase not in targets:
        raise ValueError(
            f"fase {phase!r} no válida para {product!r}. "
            f"Fases: {sorted(targets)}"
        )
    defecto = _INTERNAL_DEFAULT[product]
    return PropProfile(
        product,
        initial_capital,
        ftmo_daily_pct=reglas["daily_pct"],
        ftmo_total_pct=reglas["total_pct"],
        ftmo_total_trailing=reglas["total_trailing"],
        internal_daily_pct=(
            internal_daily_pct if internal_daily_pct is not None
            else defecto["daily_pct"]
        ),
        internal_total_pct=(
            internal_total_pct if internal_total_pct is not None
            else defecto["total_pct"]
        ),
        target_pct=float(targets[phase]),
        min_trading_days=reglas["min_trading_days"],
        best_day_rule=reglas["best_day_rule"],
        phase=phase,
        risk_pct_per_trade=risk_pct_per_trade,
    )


def new_state(profile: PropProfile, equity: float, *, day_key: str) -> Dict[str, Any]:
    """Estado inicial de seguimiento para una cuenta.

    `hwm_balance` arranca en la equity actual: para el producto trailing es el
    balance EOD más alto visto, y solo `roll_day` lo sube. `best_day_profit` y
    `positive_days_profit` alimentan el Best Day Rule (solo 1-Step).
    """
    eq = float(equity)
    return {
        "initial_capital": profile.initial_capital,
        "day_key": str(day_key),
        "day_start_balance": eq,
        "hwm_balance": eq,
        "best_day_profit": 0.0,
        "positive_days_profit": 0.0,
        "trading_days": 0,
    }


def _daily_loss_usd(day_start_balance: float, equity: float) -> float:
    """Pérdida del día según FTMO: desde el balance de inicio de día.

    NO se mide desde un pico intradía. Si la cuenta sube y devuelve parte de la
    ganancia, sigue sin haber "pérdida del día" mientras la equity no caiga por
    debajo del balance de las 00:00. Subir el suelo con el pico bloquearía antes de
    lo que FTMO bloquea: conservador, pero deja de ser la regla declarada.
    """
    return max(0.0, float(day_start_balance) - float(equity))


def _total_loss_usd(profile: PropProfile, state: Dict[str, Any], equity: float) -> float:
    """Pérdida total acumulada: estática (2-Step) o trailing (1-Step).

    - 2-Step (estático): contra el CAPITAL INICIAL. El suelo no se mueve.
    - 1-Step (trailing): contra el balance EOD más alto (`hwm_balance`). El suelo
      sube con los cierres, nunca baja.
    """
    eq = float(equity)
    if profile.ftmo_total_trailing:
        ref = float(state.get("hwm_balance", profile.initial_capital))
    else:
        ref = profile.initial_capital
    return max(0.0, ref - eq)


def buffer_remaining_usd(profile: PropProfile, state: Dict[str, Any],
                         equity: float) -> float:
    """Room que queda hasta el corte interno total. Puede ser negativo si ya se cruzó."""
    return round(profile.internal_total_usd - _total_loss_usd(profile, state, equity), 4)


def daily_room_usd(profile: PropProfile, state: Dict[str, Any], equity: float) -> float:
    """Room que queda hasta el corte interno diario. Puede ser negativo si ya se cruzó."""
    perdido = _daily_loss_usd(state.get("day_start_balance", equity), equity)
    return round(profile.internal_daily_usd - perdido, 4)


def nominal_risk_usd(profile: PropProfile) -> float:
    """Riesgo por operación en % del capital inicial (0.5% -> $125 en un $25k)."""
    return profile.risk_usd


def buffer_risk_usd(profile: PropProfile,
                    fraction: float = BUFFER_RISK_FRACTION) -> float:
    """Riesgo por operación como fracción del buffer de DD total.

    5% del buffer ($2.500) = $125: la misma cifra que el 0.5% del capital. Existe
    como pregunta aparte porque el dimensionado del shell se ancla al buffer, y
    confundir la base (buffer vs balance) desplaza el tamaño sin avisar.
    """
    return round(profile.buffer_usd * float(fraction), 4)


def sizing_base_usd(profile: PropProfile) -> float:
    """Balance de referencia del shell para dimensionar: el CAPITAL FIJO, no el vivo.

    El buffer de DD total es una fracción FIJA del capital (10% en FTMO, $2.500 en
    un $25k), así que multiplicar este capital por el % de riesgo del setup equivale
    a dimensionar sobre el buffer —$125 = 0.5% del capital = 5% del buffer— y NO
    deriva con el balance. Con el balance vivo, la cuenta que va ganando arriesga
    más dinero en cada operación y la que va perdiendo arriesga menos justo cuando
    el buffer se estrecha; ninguna de las dos es la regla que se dice seguir.

    Se deja como función (y no `profile.initial_capital` a pelo en la puerta) para
    que el "por qué" viva en un solo sitio: el día que el anclaje cambie, cambia
    aquí y no en cada punto que dimensiona.
    """
    return profile.initial_capital


def _best_day_ratio(state: Dict[str, Any]) -> Optional[float]:
    """Mejor día / suma de días positivos. `None` si aún no hay días positivos."""
    positivos = float(state.get("positive_days_profit", 0.0))
    if positivos <= 0:
        return None
    return round(float(state.get("best_day_profit", 0.0)) / positivos, 4)


def evaluate(profile: PropProfile, state: Dict[str, Any], equity: float) -> Dict[str, Any]:
    """Veredicto del shell para el estado actual de la cuenta.

    Bloquea por los cortes INTERNOS (más estrechos que los de FTMO), no por los de
    FTMO: el objetivo es parar con margen, no rozar el límite que rompe la cuenta.

    El Best Day Rule (solo 1-Step) NO bloquea: es un requisito para APROBAR, no un
    stop de operación. Se devuelve como ratio y como aviso, y quien llama decide si
    lo muestra; tratarlo como bloqueo impediría operar el día que arreglaría el
    ratio.
    """
    eq = float(equity)
    day_start = float(state.get("day_start_balance", eq))
    dd_daily = _daily_loss_usd(day_start, eq)
    dd_total = _total_loss_usd(profile, state, eq)

    reasons: List[str] = []
    if dd_daily >= profile.internal_daily_usd - 1e-9:
        reasons.append(
            "Corte interno diario alcanzado: pérdida ${0:.2f} >= tope ${1:.2f} "
            "({2}% de {3:.0f}).".format(
                dd_daily, profile.internal_daily_usd,
                profile.internal_daily_pct, profile.initial_capital)
        )
    if dd_total >= profile.internal_total_usd - 1e-9:
        tipo = "trailing" if profile.ftmo_total_trailing else "estático"
        reasons.append(
            "Corte interno total alcanzado ({0}): pérdida ${1:.2f} >= tope ${2:.2f} "
            "({3}%).".format(
                tipo, dd_total, profile.internal_total_usd, profile.internal_total_pct)
        )

    ratio = _best_day_ratio(state) if profile.best_day_rule else None
    avisos: List[str] = []
    if profile.best_day_rule and ratio is not None and ratio > BEST_DAY_MAX_FRACTION:
        avisos.append(
            "Best Day Rule pendiente: el mejor día es el {0:.0%} de los días "
            "positivos (debe bajar a <= {1:.0%}); no bloquea, se corrige operando "
            "días más pequeños.".format(ratio, BEST_DAY_MAX_FRACTION)
        )

    trading_days = int(state.get("trading_days", 0))
    return {
        "enabled": True,
        "product": profile.product,
        "phase": profile.phase,
        "blocked": bool(reasons),
        "reasons": reasons,
        "warnings": avisos,
        "dd_daily_usd": round(dd_daily, 4),
        "dd_total_usd": round(dd_total, 4),
        "daily_room_usd": daily_room_usd(profile, state, eq),
        "buffer_remaining_usd": buffer_remaining_usd(profile, state, eq),
        "target_progress_pct": _target_progress_pct(profile, state, eq),
        "target_remaining_usd": round(
            profile.target_usd - (eq - profile.initial_capital), 4),
        "best_day_ratio": ratio,
        "trading_days": trading_days,
        "min_trading_days": profile.min_trading_days,
        "min_trading_days_met": trading_days >= profile.min_trading_days,
        "state": state,
    }


def _target_progress_pct(profile: PropProfile, state: Dict[str, Any], equity: float) -> float:
    """Progreso hacia el objetivo de la fase, en %. Negativo si se está en pérdida."""
    base = float(state.get("initial_capital", profile.initial_capital))
    return round((float(equity) - base) / profile.target_usd * 100.0, 2)


def roll_day(
    profile: PropProfile,
    state: Dict[str, Any],
    *,
    new_day_key: str,
    day_close_balance: float,
    day_pnl: float,
    traded_today: bool,
) -> Dict[str, Any]:
    """Cierra el día y abre el siguiente. Devuelve el estado nuevo (no muta).

    `day_start_balance` pasa a ser el cierre de hoy: es la referencia de la pérdida
    diaria de mañana. `hwm_balance` solo sube (producto trailing). Un día CUENTA
    para `min_trading_days` si hubo al menos una operación (`traded_today`), y
    cuenta para los días positivos del Best Day Rule si cerró en verde (`day_pnl`):
    son dos contadores distintos y mezclarlos daría un ratio inventado.

    `day_close_balance` es el BALANCE (cerrado), no la equity: FTMO mide el trailing
    y el inicio de día sobre balance. Pasar equity aquí mueve el suelo con el
    flotante, que es justo lo que la regla no hace.
    """
    nuevo = dict(state)
    nuevo["day_key"] = str(new_day_key)
    nuevo["day_start_balance"] = float(day_close_balance)
    if traded_today:
        nuevo["trading_days"] = int(state.get("trading_days", 0)) + 1
    if float(day_pnl) > 0:
        nuevo["positive_days_profit"] = round(
            float(state.get("positive_days_profit", 0.0)) + float(day_pnl), 4)
        nuevo["best_day_profit"] = round(
            max(float(state.get("best_day_profit", 0.0)), float(day_pnl)), 4)
    if profile.ftmo_total_trailing:
        nuevo["hwm_balance"] = round(
            max(float(state.get("hwm_balance", day_close_balance)),
                float(day_close_balance)), 4)
    return nuevo

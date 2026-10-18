"""El estado del challenge de la cuenta activa, contra su perfil FTMO.

Este módulo junta tres piezas que no pueden vivir en `core/` (puro) ni en el
router:

1. **La cuenta** (`account_info` por MT5): balance, equity, login.
2. **El perfil de la cuenta** (`PropProfiles.for_account(login)`), resuelto desde
   `config/prop_profiles.yaml` por la fuente de `settings`.
3. **El estado del día** (`prop_state`, tabla clave/valor del store): día, balance
   de inicio, HWM, días operados.

Con eso llama a `core.prop_rules.evaluate` y devuelve el veredicto. La regla FTMO
(2-Step vs 1-Step, cortes internos, Best Day Rule) vive en `core/`; aquí solo se
resuelve QUÉ cuenta es, se lee/crea/siembra su estado y se le pasa.

Por qué NO vive en el router
---------------------------
Los endpoints de Trinity resolvían esto dentro del handler. Si la resolución del
perfil y la evaluación viven ahí, el único test posible es "el JSON tiene
llaves". Aquí cada caso es un método que se llama con un `account_info` doble.

Fail-closed
-----------
Una cuenta sin perfil mapeado NO se evalúa: se devuelve `tracked=False` con el
motivo. Sin el capital inicial de la cuenta no hay buffer, y sin buffer no se
dimensiona con las reglas de FTMO. Es el comportamiento que pide la config
(`accounts:` vacío a propósito).

El "día" de FTMO
----------------
`day_key` es la fecha UTC. Es una simplificación DOCUMENTADA: FTMO cuenta el día
natural del servidor del bróker (00:00 CE(S)T), no UTC. El offset de broker-day
llega cuando haya reloj de broker; hasta entonces UTC es el límite conocido y se
dice, en vez de fingir un día de broker que no se tiene.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from core import clock, prop_rules

#: Dónde se persiste el estado del día, dentro del store (`prop_state`).
_TABLA = "prop_state"


def _hoy_utc() -> str:
    """Fecha UTC de hoy, `YYYY-MM-DD`. El `day_key` por defecto."""
    return clock.now_utc().date().isoformat()


class PropService:
    """Resuelve el perfil de la cuenta activa y evalúa su estado FTMO."""

    def __init__(
        self,
        market: Optional[Any] = None,
        store: Optional[Any] = None,
        now: Optional[Callable[[], str]] = None,
    ) -> None:
        self._market = market
        self._store = store
        self._now = now if now is not None else _hoy_utc

    # -- lectura ----------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        """El veredicto del shell prop para la cuenta activa.

        Forma estable, haya o no perfil:

            {"account": {...}, "tracked": bool, "reason": None|str,
             "profile": None|{...}, "evaluation": None|{...}}

        Lanza si el bróker no responde (lo traduce `api/errors.py`) o si el YAML
        de perfiles está mal formado (`PropProfilesError`, que es `ValueError`).
        """
        cuenta = self._market.account_info() if self._market is not None else {}
        if self._store is None:
            return _sin_seguimiento(
                cuenta,
                "no hay store cableado: no se puede resolver el perfil de la cuenta",
            )
        return self._con_store(cuenta)

    def _con_store(self, cuenta: Dict[str, Any]) -> Dict[str, Any]:
        login = cuenta.get("login")
        perfiles = self._store.get_prop_profiles()
        perfil = perfiles.for_account(login) if perfiles is not None else None
        if perfil is None:
            return _sin_seguimiento(
                cuenta,
                "la cuenta {0!r} no tiene perfil en config/prop_profiles.yaml; no "
                "se sabe qué topes aplican (fail-closed)".format(login),
            )

        equity = _num(cuenta.get("equity"))
        balance = _num(cuenta.get("balance"))
        if equity is None and balance is None:
            return _sin_seguimiento(
                cuenta,
                "la cuenta no publica balance ni equity; no hay con qué medir la "
                "pérdida contra el inicio del día",
            )
        if equity is None:
            equity = balance
        base = balance if balance is not None else equity

        estado = self._estado_vigente(login, perfil, float(base))
        evaluado = prop_rules.evaluate(perfil, estado, float(equity))
        return {
            "account": _cuenta(cuenta),
            "tracked": True,
            "reason": None,
            "profile": _perfil_dict(perfil),
            "evaluation": evaluado,
        }

    # -- estado del día ---------------------------------------------------------

    def _estado_vigente(self, login: Any, perfil: Any, base: float) -> Dict[str, Any]:
        """El estado del día: leído, sembrado o re-baselineado. Persiste al cambiar."""
        guardado = self._leer(login)
        day_key = str(self._now())
        if guardado is None:
            estado = prop_rules.new_state(perfil, base, day_key=day_key)
            self._escribir(login, estado)
            return estado
        if state_day_key(guardado) != day_key:
            estado = _rebaseline(guardado, day_key=day_key, balance=base)
            self._escribir(login, estado)
            return estado
        return guardado

    def _leer(self, login: Any) -> Optional[Dict[str, Any]]:
        valor = self._store.get_setting(_clave(login), default=None, table=_TABLA)
        return valor if isinstance(valor, dict) else None

    def _escribir(self, login: Any, estado: Dict[str, Any]) -> None:
        self._store.set_setting(_clave(login), estado, table=_TABLA)

    # -- catálogo (diagnóstico) -------------------------------------------------

    def perfiles(self) -> Dict[str, Any]:
        """Los perfiles definidos y los logins mapeados, para la UI."""
        perfiles = self._store.get_prop_profiles() if self._store is not None else None
        if perfiles is None:
            return {"profiles": [], "accounts": [], "configured": False}
        return {
            "profiles": [
                {"name": nombre, **_perfil_dict(perfiles.profile(nombre))}
                for nombre in perfiles.nombres()
            ],
            "accounts": perfiles.account_ids(),
            "configured": bool(perfiles),
        }


# ============================================================================
# Helpers de módulo (sin estado)
# ============================================================================


def _clave(login: Any) -> str:
    """La clave de `prop_state` para un login. El estado es POR cuenta."""
    return "account:{0}".format("" if login is None else login)


def _num(valor: Any) -> Optional[float]:
    """A número, o `None`. El balance de MT5 a veces llega como texto."""
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _cuenta(cuenta: Dict[str, Any]) -> Dict[str, Any]:
    """El subset de la cuenta que la UI necesita. Sin inventar lo que no hay."""
    return {
        k: cuenta.get(k)
        for k in ("login", "balance", "equity", "margin", "margin_free",
                  "profit", "currency", "leverage")
    }


def _sin_seguimiento(cuenta: Dict[str, Any], motivo: str) -> Dict[str, Any]:
    """La respuesta fail-closed: no se sigue la cuenta y se dice por qué."""
    return {
        "account": _cuenta(cuenta),
        "tracked": False,
        "reason": motivo,
        "profile": None,
        "evaluation": None,
    }


def state_day_key(state: Any) -> Optional[str]:
    """El `day_key` de un estado guardado, o `None` si no es un dict usable."""
    if not isinstance(state, dict):
        return None
    valor = state.get("day_key")
    return str(valor) if valor is not None else None


def _rebaseline(state: Dict[str, Any], *, day_key: str, balance: float) -> Dict[str, Any]:
    """Reconcilia un estado de un día anterior con el de hoy.

    `day_start_balance` pasa al balance actual (la referencia del DD diario de
    hoy). `hwm_balance`, `trading_days`, `best_day_profit` y `positive_days_profit`
    se PRESERVAN: son acumulados del challenge, no del día. Perderlos reiniciaría
    los días mínimos y el Best Day Rule.

    No se usa `prop_rules.roll_day` porque este cambio lo detecta el reloj, no un
    cierre de sesión: no hay `day_pnl` ni un "operó hoy" que reportar.
    """
    nuevo = dict(state)
    nuevo["day_key"] = str(day_key)
    nuevo["day_start_balance"] = float(balance)
    nuevo.setdefault("hwm_balance", float(balance))
    nuevo.setdefault("trading_days", 0)
    nuevo.setdefault("best_day_profit", 0.0)
    nuevo.setdefault("positive_days_profit", 0.0)
    return nuevo


def _perfil_dict(perfil: Any) -> Dict[str, Any]:
    """El `PropProfile` como dict plano para el JSON.

    No se recalcula nada: los USD ya vienen resueltos del perfil. Volver a
    dividir aquí sería una segunda fuente de los mismos números.
    """
    return {
        "product": perfil.product,
        "phase": perfil.phase,
        "initial_capital": perfil.initial_capital,
        "ftmo_daily_usd": perfil.ftmo_daily_usd,
        "ftmo_total_usd": perfil.ftmo_total_usd,
        "ftmo_total_trailing": perfil.ftmo_total_trailing,
        "internal_daily_pct": perfil.internal_daily_pct,
        "internal_total_pct": perfil.internal_total_pct,
        "internal_daily_usd": perfil.internal_daily_usd,
        "internal_total_usd": perfil.internal_total_usd,
        "buffer_usd": perfil.buffer_usd,
        "target_usd": perfil.target_usd,
        "risk_usd": perfil.risk_usd,
        "min_trading_days": perfil.min_trading_days,
        "best_day_rule": perfil.best_day_rule,
    }


__all__ = ["PropService", "state_day_key"]

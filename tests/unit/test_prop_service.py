"""Tests de `PropService`: resolución de perfil, estado del día y veredicto.

No se abre MT5 ni la base real: el mercado y el store son dobles. Lo que se fija
aquí son las decisiones que un shell prop tiene que tomar bien:

1. Cuenta sin perfil -> `tracked=False` con motivo (fail-closed), NO un default.
2. Cuenta mapeada -> se resuelve el perfil y se evalúa contra SUS topes.
3. El estado del día se siembra una vez y se reusa; al cambiar de día se
   re-baselinea el inicio pero se PRESERVAN los acumulados del challenge.
4. La pérdida diaria se mide contra el balance de inicio de día, no contra el
   equity flotante del momento.
"""

from __future__ import annotations

import pytest

from api.services.prop import PropService
from core import prop_profiles

PROFILES = prop_profiles.parse_profiles({
    "profiles": {
        "ftmo_2step_25k": {
            "product": "2step",
            "initial_capital": 25000,
            "phase": "challenge",
            "internal_daily_pct": 4.0,
            "internal_total_pct": 7.0,
        },
        "ftmo_1step_25k": {
            "product": "1step",
            "initial_capital": 25000,
        },
    },
    "accounts": {12345678: "ftmo_2step_25k"},
})

HOY = "2026-10-16"


class FakeMarket:
    def __init__(self, cuenta):
        self._cuenta = cuenta

    def account_info(self):
        return dict(self._cuenta)


class FakeStore:
    def __init__(self, perfiles=PROFILES):
        self._perfiles = perfiles
        self.datos = {}  # (tabla, clave) -> valor

    def get_prop_profiles(self):
        return self._perfiles

    def get_setting(self, key, default=None, table="agent_settings"):
        return self.datos.get((table, key), default)

    def set_setting(self, key, value, table="agent_settings"):
        self.datos[(table, key)] = value


def _cuenta(login=12345678, balance=25000.0, equity=25000.0):
    return {"login": login, "balance": balance, "equity": equity, "currency": "USD"}


def _svc(cuenta=None, store=None, perfiles=PROFILES):
    store = store if store is not None else FakeStore(perfiles)
    return PropService(market=FakeMarket(cuenta or _cuenta()), store=store,
                       now=lambda: HOY), store


class TestCuentaSinPerfil:
    def test_cuenta_desconocida_no_se_sigue(self):
        svc, _ = _svc(cuenta=_cuenta(login=99999999))
        datos = svc.state()
        assert datos["tracked"] is False
        assert datos["profile"] is None
        assert datos["evaluation"] is None
        assert "99999999" in datos["reason"]

    def test_sin_store_no_se_puede_resolver_el_perfil(self):
        svc = PropService(market=FakeMarket(_cuenta()), store=None, now=lambda: HOY)
        datos = svc.state()
        assert datos["tracked"] is False
        assert "store" in datos["reason"]

    def test_cuenta_sin_balance_ni_equity_no_se_mide(self):
        svc, _ = _svc(cuenta={"login": 12345678, "balance": None, "equity": None})
        datos = svc.state()
        assert datos["tracked"] is False
        assert "equity" in datos["reason"] or "balance" in datos["reason"]


class TestCuentaMapeada:
    def test_se_resuelve_el_perfil_y_se_evalua(self):
        svc, _ = _svc()
        datos = svc.state()
        assert datos["tracked"] is True
        assert datos["reason"] is None
        assert datos["profile"]["product"] == "2step"
        assert datos["profile"]["internal_daily_usd"] == pytest.approx(1_000.0)
        assert datos["profile"]["internal_total_usd"] == pytest.approx(1_750.0)
        assert datos["evaluation"]["blocked"] is False

    def test_el_perfil_trailing_del_1step_se_preserva(self):
        perfiles = prop_profiles.parse_profiles({
            "profiles": {"p": {"product": "1step", "initial_capital": 25000}},
            "accounts": {12345678: "p"},
        })
        svc, _ = _svc(perfiles=perfiles)
        datos = svc.state()
        assert datos["profile"]["ftmo_total_trailing"] is True
        assert datos["profile"]["best_day_rule"] is True

    def test_bloquea_cuando_se_cruza_el_corte_interno_diario(self):
        # Inicio de día 25.000; equity 24.000 => pérdida 1.000 = corte interno.
        store = FakeStore()
        store.set_setting("account:12345678",
                          {"day_key": HOY, "day_start_balance": 25000.0,
                           "hwm_balance": 25000.0, "trading_days": 0,
                           "best_day_profit": 0.0, "positive_days_profit": 0.0},
                          table="prop_state")
        svc, _ = _svc(cuenta=_cuenta(equity=24000.0), store=store)
        datos = svc.state()
        assert datos["evaluation"]["blocked"] is True
        assert any("diario" in r for r in datos["evaluation"]["reasons"])

    def test_la_perdida_diaria_ignora_el_pico_intradia(self):
        # Sube a 25.500 y cae a 25.200: sigue por encima del inicio de día (25.000).
        store = FakeStore()
        store.set_setting("account:12345678",
                          {"day_key": HOY, "day_start_balance": 25000.0,
                           "hwm_balance": 25000.0, "trading_days": 0,
                           "best_day_profit": 0.0, "positive_days_profit": 0.0},
                          table="prop_state")
        svc, _ = _svc(cuenta=_cuenta(equity=25200.0), store=store)
        datos = svc.state()
        assert datos["evaluation"]["dd_daily_usd"] == pytest.approx(0.0)
        assert datos["evaluation"]["blocked"] is False


class TestEstadoDelDia:
    def test_se_siembra_y_persiste_la_primera_vez(self):
        svc, store = _svc()
        svc.state()
        guardado = store.datos[("prop_state", "account:12345678")]
        assert guardado["day_key"] == HOY
        assert guardado["day_start_balance"] == pytest.approx(25000.0)

    def test_el_mismo_dia_se_reusa_el_estado_guardado(self):
        store = FakeStore()
        store.set_setting("account:12345678",
                          {"day_key": HOY, "day_start_balance": 25000.0,
                           "hwm_balance": 26000.0, "trading_days": 3,
                           "best_day_profit": 500.0, "positive_days_profit": 800.0},
                          table="prop_state")
        svc, _ = _svc(store=store)
        datos = svc.state()
        assert datos["evaluation"]["trading_days"] == 3
        # El HWM guardado NO se pisa al reusar el estado.
        assert store.datos[("prop_state", "account:12345678")]["hwm_balance"] == 26000.0

    def test_un_dia_anterior_rebaselina_el_inicio_y_conserva_acumulados(self):
        store = FakeStore()
        store.set_setting("account:12345678",
                          {"day_key": "1999-01-01", "day_start_balance": 1.0,
                           "hwm_balance": 26000.0, "trading_days": 3,
                           "best_day_profit": 500.0, "positive_days_profit": 800.0},
                          table="prop_state")
        svc, _ = _svc(cuenta=_cuenta(balance=25100.0, equity=25100.0), store=store)
        datos = svc.state()
        guardado = store.datos[("prop_state", "account:12345678")]
        assert guardado["day_key"] == HOY
        assert guardado["day_start_balance"] == pytest.approx(25100.0)
        assert guardado["hwm_balance"] == pytest.approx(26000.0)
        assert guardado["trading_days"] == 3
        assert datos["evaluation"]["trading_days"] == 3


class TestPerfiles:
    def test_lista_los_perfiles_y_las_cuentas(self):
        svc, _ = _svc()
        datos = svc.perfiles()
        nombres = {p["name"] for p in datos["profiles"]}
        assert nombres == {"ftmo_2step_25k", "ftmo_1step_25k"}
        assert datos["accounts"] == ["12345678"]
        assert datos["configured"] is True

    def test_sin_store_no_hay_catalogo(self):
        svc = PropService(market=FakeMarket(_cuenta()), store=None)
        datos = svc.perfiles()
        assert datos["profiles"] == []
        assert datos["configured"] is False

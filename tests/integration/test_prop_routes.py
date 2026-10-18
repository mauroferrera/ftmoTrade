"""Tests de las rutas `/api/prop` contra la app real, con dobles inyectados.

No se abre MT5 ni la base real: se construye un `Runtime` con mercado y prop
dobles y se pasa a `create_app(runtime=...)`. Así el test recorre router ->
dependencia -> servicio sin terminal.

Lo que se fija es la CONTRATACIÓN HTTP: `tracked=false` es un 200 (un hecho, no
un error); sin prop cableado es 503; un YAML de perfiles mal formado es 400 (se
ve, no se esconde); una terminal caída es 503.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from adapters.base_adapter import AdapterError
from api.app import create_app
from api.runtime import Runtime
from api.services.prop import PropService
from core import prop_profiles

PROFILES = prop_profiles.parse_profiles({
    "profiles": {"ftmo_2step_25k": {"product": "2step", "initial_capital": 25000}},
    "accounts": {12345678: "ftmo_2step_25k"},
})

HOY = "2026-10-16"


class FakeMarket:
    def __init__(self, cuenta=None, *, cae=False):
        self._cuenta = cuenta or {"login": 12345678, "balance": 25000.0,
                                  "equity": 25000.0, "currency": "USD"}
        self._cae = cae

    def account_info(self):
        if self._cae:
            raise AdapterError("el terminal no responde")
        return dict(self._cuenta)


class FakeStore:
    def __init__(self, perfiles=PROFILES):
        self._perfiles = perfiles
        self._settings = {}

    def list_db_tables(self):
        return [{"name": "t%d" % i} for i in range(12)]

    def get_trading_config(self):
        return {"symbols": ["EURUSD"], "execution": {}}

    def get_prop_profiles(self):
        return self._perfiles

    def get_setting(self, key, default=None, table="agent_settings"):
        return self._settings.get((table, key), default)

    def set_setting(self, key, value, table="agent_settings"):
        self._settings[(table, key)] = value


def _client(*, market=None, store=None, prop="auto"):
    market = market or FakeMarket()
    store = store if store is not None else FakeStore()
    if prop == "auto":
        prop = PropService(market=market, store=store, now=lambda: HOY)
    rt = Runtime(store=store, market=market, prop=prop)
    return TestClient(create_app(runtime=rt))


class TestPropState:
    def test_cuenta_mapeada_se_sigue(self):
        with _client() as c:
            r = c.get("/api/prop/state")
        assert r.status_code == 200
        datos = r.json()
        assert datos["tracked"] is True
        assert datos["profile"]["product"] == "2step"
        assert datos["evaluation"]["blocked"] is False

    def test_cuenta_sin_perfil_es_200_con_tracked_false(self):
        with _client(market=FakeMarket({"login": 99999999,
                                        "balance": 1.0, "equity": 1.0})) as c:
            r = c.get("/api/prop/state")
        assert r.status_code == 200
        datos = r.json()
        assert datos["tracked"] is False
        assert datos["evaluation"] is None

    def test_sin_prop_cableado_es_503(self):
        with _client(prop=None) as c:
            r = c.get("/api/prop/state")
        assert r.status_code == 503

    def test_un_yaml_de_perfiles_mal_formado_es_400(self):
        class StoreMalo(FakeStore):
            def get_prop_profiles(self):
                raise prop_profiles.PropProfilesError("perfil 'p' inválido")

        with _client(store=StoreMalo()) as c:
            r = c.get("/api/prop/state")
        assert r.status_code == 400

    def test_una_terminal_caida_es_503(self):
        with _client(market=FakeMarket(cae=True)) as c:
            r = c.get("/api/prop/state")
        assert r.status_code == 503


class TestPropProfiles:
    def test_lista_perfiles_y_cuentas(self):
        with _client() as c:
            r = c.get("/api/prop/profiles")
        assert r.status_code == 200
        datos = r.json()
        assert datos["accounts"] == ["12345678"]
        assert {p["name"] for p in datos["profiles"]} == {"ftmo_2step_25k"}

"""Tests de las rutas de setup contra la app real, con dobles inyectados.

No se abre MT5: se construye un `Runtime` con un mercado doble y se pasa a
`create_app(runtime=...)`, de modo que el test recorre router -> dependencia ->
servicio sin terminal. Lo que se fija es la CONTRATACIÓN HTTP de las dos rutas:

  - `/api/risk/setup` publica el `risk_engine` entero; sin scores es 422 (no un 200
    con ceros) y `synthetic=1` es 503 (no se finge un simulador).
  - `/api/chart/setup-eval` publica el plan con overlay; un setup aprobado trae 3
    `markLine` (Entry/SL/TP) y `min_score_used` declara el umbral aplicado.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from adapters.base_adapter import SymbolNotFound
from api.app import create_app
from api.runtime import Runtime
from core import risk_engine

PATRONES_BUY = {
    "fvgs": [{"type": "BULLISH_FVG", "bottom": 1.1000, "top": 1.1005}],
    "order_blocks": [{"type": "BULLISH_OB", "bottom": 1.0995, "top": 1.1005}],
    "sweeps": [{"type": "PDL_SWEEP"}],
}

#: Solo un FVG a favor: score ~57.5, por debajo del umbral de producción. Sirve para
#: probar que `min_score` filtra de verdad y no solo se declara.
PATRONES_FVG = {"fvgs": [{"type": "BULLISH_FVG", "bottom": 1.1000, "top": 1.1005}],
                "order_blocks": [], "sweeps": []}

CFG = {
    "sl_distance_by_symbol": {"EURUSD": 0.0012},
    "tp_ratio_r": 2.0,
    "min_score": 0.0,
    "score": {"weights": {"smc": 85, "killzone": 15}},
}

SPEC = SimpleNamespace(
    symbol="EURUSD", digits=5, pip=0.0001, point=0.00001, unit_label="pips",
    contract_size=100000.0, tick_size=0.00001, tick_value=1.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
)


class FakeMarket:
    def __init__(self, *, sin_velas=False, sin_scores=False, no_such=False,
                 patrones=None):
        self._sin_velas = sin_velas
        self._sin_scores = sin_scores
        self._no_such = no_such
        self._patrones = patrones if patrones is not None else PATRONES_BUY
        self.last_kwargs = {}

    def snapshot(self, symbol, timeframe="M15", bars=300, cfg=None,
                 force_killzone=False, now=None):
        self.last_kwargs = {"force_killzone": force_killzone, "cfg": cfg}
        if self._no_such:
            raise SymbolNotFound("no publicado")
        if self._sin_velas:
            return None
        re_ = risk_engine.score_directions(
            self._patrones, {"smc": 85, "killzone": 15},
            risk_engine.DEFAULT_KILLZONES,
            datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
        )
        return {
            "symbol": symbol, "timeframe": timeframe, "current_price": 1.1010,
            "last_time": 1_700_000_000, "analysis": {"patterns": self._patrones},
            "risk_engine": None if self._sin_scores else re_,
            "killzone_forced": bool(force_killzone),
        }

    def spec(self, symbol):
        return SPEC


class FakeStore:
    def get_trading_config(self):
        return dict(CFG)


def _client(*, market=None, store=None):
    market = market or FakeMarket()
    store = store if store is not None else FakeStore()
    rt = Runtime(store=store, market=market)
    return TestClient(create_app(runtime=rt))


class TestRiskSetup:
    def test_publica_el_risk_engine_entero(self):
        with _client() as c:
            r = c.get("/api/risk/setup?symbol=EURUSD")
        assert r.status_code == 200
        datos = r.json()
        assert datos["symbol"] == "EURUSD"
        assert datos["current_price"] == 1.1010
        assert set(datos["bull"]["components"]) == {"smc", "killzone"}
        assert datos["bear"]["score"] <= datos["bull"]["score"]

    def test_sin_velas_es_422(self):
        with _client(market=FakeMarket(sin_velas=True)) as c:
            r = c.get("/api/risk/setup")
        assert r.status_code == 422

    def test_sin_scores_es_422(self):
        with _client(market=FakeMarket(sin_scores=True)) as c:
            r = c.get("/api/risk/setup")
        assert r.status_code == 422

    def test_simbolo_desconocido_es_404(self):
        with _client(market=FakeMarket(no_such=True)) as c:
            r = c.get("/api/risk/setup?symbol=ZZZUSD")
        assert r.status_code == 404

    def test_synthetic_es_503(self):
        with _client() as c:
            r = c.get("/api/risk/setup?synthetic=1")
        assert r.status_code == 503


class TestChartSetupEval:
    def test_setup_aprobado_trae_overlay(self):
        with _client() as c:
            r = c.get("/api/chart/setup-eval?symbol=EURUSD")
        assert r.status_code == 200
        datos = r.json()
        assert datos["direction"] == "BUY"
        assert datos["approved"] is True
        assert datos["entry"] == 1.1000
        assert datos["sl"] == 1.0988 and datos["tp"] == 1.1024
        assert len(datos["echarts"]["markLine"]) == 3
        assert datos["view"]["price_min"] == 1.0988
        assert datos["min_score_used"] == 0.0
        assert datos["unit_label"] == "pips"

    def test_min_score_por_parametro_queda_declarado(self):
        with _client(market=FakeMarket(patrones=PATRONES_FVG)) as c:
            r = c.get("/api/chart/setup-eval?min_score=60")
        datos = r.json()
        assert datos["approved"] is False
        assert datos["min_score_used"] == 60.0
        assert datos["echarts"]["markLine"] == []

    def test_killzone_forzada_lo_declara(self):
        with _client() as c:
            r = c.get("/api/chart/setup-eval?killzone=1")
        datos = r.json()
        assert datos["in_killzone"] is True

    def test_sin_scores_es_422(self):
        with _client(market=FakeMarket(sin_scores=True)) as c:
            r = c.get("/api/chart/setup-eval")
        assert r.status_code == 422

    def test_synthetic_es_503(self):
        with _client() as c:
            r = c.get("/api/chart/setup-eval?synthetic=1")
        assert r.status_code == 503

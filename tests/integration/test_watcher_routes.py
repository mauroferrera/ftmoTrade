"""Tests de las rutas del watcher contra la app real, con dobles inyectados.

Sin MT5 y sin base real: un `Runtime` con mercado y store dobles, pasado a
`create_app(runtime=...)`. Se fija la CONTRATACIÓN:

  - `GET /status` es lectura (sin token) y publica `auto_execute: false` SIEMPRE.
  - `POST /scan` es escritura (pide token) y audita en `setup_log`: un setup nuevo
    deja fila y se re-escanea sin reavisar (la memoria de estado lo frena).
  - Un descarte se audita UNA vez por firma (dedup del ciclo), no una por escaneo.
  - `POST /auto-execute` es 501 con el motivo, nunca un 200 que finja encender algo.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.app import create_app
from api.runtime import Runtime
from api.services.watcher import WatcherService
from core import clock, risk_engine

PATRONES_BUY = {
    "fvgs": [{"type": "BULLISH_FVG", "bottom": 1.1000, "top": 1.1005}],
    "order_blocks": [{"type": "BULLISH_OB", "bottom": 1.0995, "top": 1.1005}],
    "sweeps": [{"type": "PDL_SWEEP"}],
}

CFG_OK = {
    "sl_distance_by_symbol": {"EURUSD": 0.0012},
    "tp_ratio_r": 2.0,
    "min_score": 0.0,
    "score": {"weights": {"smc": 85, "killzone": 15}},
}

# Sin distancia de SL: el gate no aprueba, pero sí produce motivos -> descarte.
CFG_SIN_SL = {"tp_ratio_r": 2.0, "min_score": 0.0}

SPEC = SimpleNamespace(
    symbol="EURUSD", digits=5, pip=0.0001, point=0.00001, unit_label="pips",
    contract_size=100000.0, tick_size=0.00001, tick_value=1.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
)


class FakeMarket:
    def __init__(self):
        self.scans = 0

    def snapshot(self, symbol, timeframe="M15", bars=300, cfg=None,
                 force_killzone=False, now=None):
        self.scans += 1
        re_ = risk_engine.score_directions(
            PATRONES_BUY, {"smc": 85, "killzone": 15},
            risk_engine.DEFAULT_KILLZONES,
            datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
        )
        return {
            "symbol": symbol, "timeframe": timeframe, "current_price": 1.1010,
            "last_time": 1_700_000_000, "analysis": {"patterns": PATRONES_BUY},
            "risk_engine": re_, "killzone_forced": bool(force_killzone),
        }

    def spec(self, symbol):
        return SPEC


class FakeStore:
    def __init__(self, cfg=None):
        self._cfg = cfg if cfg is not None else dict(CFG_OK)
        self.estados = {}
        self.setup_log = []

    def get_trading_config(self):
        return dict(self._cfg)

    def get_watcher_config(self):
        return {"symbols": [{"symbol": "EURUSD", "timeframe": "M15"}],
                "dedup_ttl_sec": 900}

    def get_setup_state(self, symbol, timeframe):
        return self.estados.get((symbol, timeframe))

    def new_setup_state(self, symbol, timeframe, row):
        self.estados[(symbol, timeframe)] = {
            **row, "created_at": clock.now_iso(), "updated_at": clock.now_iso(),
        }

    def update_setup_state(self, symbol, timeframe, patch):
        self.estados.setdefault((symbol, timeframe), {}).update(patch)

    def log_setup(self, row):
        self.setup_log.append(row)
        return len(self.setup_log)

    def update_trade_result(self, setup_id, result):
        return None


def _client(*, market=None, store=None, token=None, monkeypatch=None):
    market = market or FakeMarket()
    store = store if store is not None else FakeStore()
    watcher = WatcherService(market=market, store=store)
    rt = Runtime(store=store, market=market, watcher=watcher)
    if monkeypatch is not None:
        if token is not None:
            monkeypatch.setenv("API_TOKEN", token)
        else:
            monkeypatch.delenv("API_TOKEN", raising=False)
    return TestClient(create_app(runtime=rt))


class TestStatus:
    def test_estado_publica_auto_execute_false(self):
        with _client() as c:
            r = c.get("/api/watcher/status")
        assert r.status_code == 200
        datos = r.json()
        assert datos["auto_execute"] is False
        assert datos["auto_execute_disponible"] is False
        assert datos["auto_execute_conf"] is False
        assert datos["symbols"] == [{"symbol": "EURUSD", "timeframe": "M15"}]

    def test_status_no_pide_token(self, monkeypatch):
        with _client(token="secreto", monkeypatch=monkeypatch) as c:
            r = c.get("/api/watcher/status")
        assert r.status_code == 200


class TestScan:
    def test_setup_nuevo_se_audita_y_no_reavisa(self):
        store = FakeStore()
        with _client(store=store) as c:
            r1 = c.post("/api/watcher/scan")
            r2 = c.post("/api/watcher/scan")
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["audit_logged"] == 1
        assert [e["event"] for e in d1["events"]] == ["new"]
        assert d1["events"][0]["auto_ejecutado"] is False
        assert store.setup_log[0]["source"] == "watcher"
        assert store.setup_log[0]["trade_result_json"] == {"executed": False, "dry_run": True}
        d2 = r2.json()
        assert d2["events"] == []
        assert len(store.setup_log) == 1

    def test_descarte_se_audita_una_vez_por_firma(self):
        store = FakeStore(cfg=CFG_SIN_SL)
        with _client(store=store) as c:
            r1 = c.post("/api/watcher/scan")
            r2 = c.post("/api/watcher/scan")
        d1 = r1.json()
        assert [e["event"] for e in d1["events"]] == ["rejected"]
        assert d1["audit_logged"] == 0
        assert len(d1["rejects"]) == 1
        assert store.setup_log[0]["validated"] is False
        d2 = r2.json()
        assert d2["rejects"] == []
        assert len(store.setup_log) == 1

    def test_scan_pide_token(self, monkeypatch):
        with _client(token="secreto", monkeypatch=monkeypatch) as c:
            r = c.post("/api/watcher/scan")
        assert r.status_code == 401

    def test_scan_con_token_pasa(self, monkeypatch):
        with _client(token="secreto", monkeypatch=monkeypatch) as c:
            r = c.post("/api/watcher/scan", headers={"x-api-token": "secreto"})
        assert r.status_code == 200

    def test_sin_simbolos_lo_declara(self):
        class StoreVacio(FakeStore):
            def get_watcher_config(self):
                return {}

            def get_trading_config(self):
                return {}

        with _client(store=StoreVacio()) as c:
            r = c.post("/api/watcher/scan")
        datos = r.json()
        assert datos["symbols"] == 0
        assert datos["events"] == []
        assert "no hay símbolos" in datos["motivo"]


class TestAutoExecute:
    def test_exige_enabled(self):
        with _client() as c:
            r = c.post("/api/watcher/auto-execute", json={})
        assert r.status_code == 422

    def test_auto_execute_es_501_con_motivo(self):
        with _client() as c:
            r = c.post("/api/watcher/auto-execute", json={"enabled": True})
        assert r.status_code == 501
        detalle = r.json()["detail"]
        assert detalle["enabled"] is True
        assert detalle["auto_execute"] is False
        assert "no manda órdenes" in detalle["motivo"]

    def test_auto_execute_pide_token(self, monkeypatch):
        with _client(token="secreto", monkeypatch=monkeypatch) as c:
            r = c.post("/api/watcher/auto-execute", json={"enabled": True})
        assert r.status_code == 401

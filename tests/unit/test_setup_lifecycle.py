"""Tests de `core.setup_lifecycle`: transiciones y deduplicación de setups.

La máquina de estados es pura: recibe la fila de estado anterior y el `gate_ok` ya
resuelto, y devuelve `"new"`, `"closed"` o `None`. Aquí se fijan los contratos:

  - Sin ciclo previo, o con uno TERMINADO, un gate que aprueba abre un ciclo nuevo.
  - Un ciclo abierto NO se reavisa mientras siga aprobando y no haya caducado.
  - El gate que deja de aprobar, o el TTL, cierran el ciclo.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core import setup_lifecycle


def _fila(status="active", creado="2026-01-01T08:00:00+00:00"):
    return {"symbol": "EURUSD", "timeframe": "M15", "status": status, "created_at": creado}


AHORA = datetime(2026, 1, 1, 8, 10, tzinfo=timezone.utc)


class TestDecideTransition:
    def test_sin_fila_previa_y_gate_ok_es_nuevo(self):
        assert setup_lifecycle.decide_transition(None, True, ahora=AHORA) == "new"

    def test_sin_fila_previa_y_gate_no_ok_no_hace_nada(self):
        assert setup_lifecycle.decide_transition(None, False, ahora=AHORA) is None

    def test_estado_terminado_con_gate_ok_es_nuevo(self):
        fila = {"status": "closed", "created_at": "2026-01-01T07:00:00+00:00"}
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA) == "new"

    def test_ciclo_abierto_y_gate_ok_no_reavisa(self):
        fila = {"status": "active", "created_at": "2026-01-01T08:00:00+00:00"}
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA, ttl_sec=3600) is None

    def test_ciclo_abierto_y_gate_caido_se_cierra(self):
        fila = {"status": "active", "created_at": "2026-01-01T08:00:00+00:00"}
        assert setup_lifecycle.decide_transition(fila, False, ahora=AHORA) == "closed"

    def test_ciclo_abierto_expirado_por_ttl_se_cierra(self):
        fila = {"status": "active", "created_at": "2026-01-01T07:00:00+00:00"}
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA, ttl_sec=900) == "closed"

    def test_estado_futuro_no_terminal_se_trata_como_abierto(self):
        """Un estado de una fase futura NO se reabre mientras no sea terminal."""
        fila = {"status": "ejecutado", "created_at": "2026-01-01T08:00:00+00:00"}
        assert setup_lifecycle.decide_transition(fila, True, ahora=AHORA) is None


class TestFirmas:
    def test_clave_de_motivo_ignora_numeros(self):
        a = setup_lifecycle.clave_de_motivo("Score 41.0 < umbral 59.5.")
        b = setup_lifecycle.clave_de_motivo("Score 43.0 < umbral 59.5.")
        assert a == b

    def test_firma_estable_por_direccion_banda_y_motivos(self):
        g1 = {"direction": "BUY", "score": 41.0, "reasons": ["Score 41.0 < umbral 59.5."]}
        g2 = {"direction": "BUY", "score": 43.0, "reasons": ["Score 43.0 < umbral 59.5."]}
        g3 = {"direction": "SELL", "score": 41.0, "reasons": ["Score 41.0 < umbral 59.5."]}
        assert setup_lifecycle.firma_de_rechazo(g1) == setup_lifecycle.firma_de_rechazo(g2)
        assert setup_lifecycle.firma_de_rechazo(g1) != setup_lifecycle.firma_de_rechazo(g3)


class TestComponentes:
    def test_mapea_buy_a_bull(self):
        snap = {"risk_engine": {"bull": {"components": {"smc": {"value": 1.0}}}}}
        comps = setup_lifecycle.componentes_del_score(snap, "BUY")
        assert comps == {"smc": {"value": 1.0}}

    def test_copia_profunda_no_comparte_subdicts(self):
        original = {"smc": {"value": 1.0}}
        snap = {"risk_engine": {"bull": {"components": original}}}
        comps = setup_lifecycle.componentes_del_score(snap, "BUY")
        comps["smc"]["value"] = 99.0
        assert original["smc"]["value"] == 1.0

    def test_direccion_desconocida_devuelve_vacio(self):
        assert setup_lifecycle.componentes_del_score({}, "LONG") == {}

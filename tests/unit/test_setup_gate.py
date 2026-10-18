"""Tests de `core.setup_gate`: zona de entrada, niveles y veredicto.

El gate es puro: se le pasa un snapshot ya armado y la config plana. Aquí se fijan
los contratos que el watcher y las rutas esperan:

  - Elige la dirección de mayor score, no una fijada de antemano.
  - La zona de entrada es la más CERCANA del lado correcto (debajo en BUY, encima
    en SELL) y se descarta si está más lejos que el máximo permitido.
  - El plan (entry/sl/tp) sale de la distancia de SL configurada y del `tp_ratio_r`.
  - La killzone NO es un AND: un score alto fuera de ventana sigue aprobando (D-007).
  - Sin snapshot o sin scores, `None`: no se inventa un veredicto.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core import risk_engine, setup_gate


def _patrones_favor(direccion: str):
    """Un juego de patrones a favor de la dirección (FVG + OB + sweep)."""
    alcista = direccion.upper() == "BUY"
    return {
        "fvgs": [{"type": "BULLISH_FVG" if alcista else "BEARISH_FVG",
                  "bottom": 1.1000 if alcista else 1.1010, "top": 1.1005 if alcista else 1.1015}],
        "order_blocks": [{"type": "BULLISH_OB" if alcista else "BEARISH_OB",
                          "bottom": 1.0995 if alcista else 1.1010, "top": 1.1005 if alcista else 1.1020}],
        "sweeps": [{"type": "PDL_SWEEP" if alcista else "PDH_SWEEP"}],
    }


def _snapshot(direccion="BUY", price=1.1010, now=None, killzones=None):
    patrones = _patrones_favor(direccion)
    re_ = risk_engine.score_directions(
        patrones, {"smc": 85, "killzone": 15},
        killzones if killzones is not None else risk_engine.DEFAULT_KILLZONES,
        now or datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
    )
    return {"symbol": "EURUSD", "current_price": price,
            "analysis": {"patterns": patrones}, "risk_engine": re_}


CFG = {"sl_distance_by_symbol": {"EURUSD": 0.0012}, "tp_ratio_r": 2.0, "min_score": 0.0}


# ---------------------------------------------------------------------------
# entry_zone
# ---------------------------------------------------------------------------


class TestEntryZone:
    def test_buy_elige_la_zona_mas_cercana_por_debajo(self):
        patrones = {
            "fvgs": [
                {"type": "BULLISH_FVG", "bottom": 1.0900, "top": 1.0905},
                {"type": "BULLISH_FVG", "bottom": 1.0990, "top": 1.0995},
            ],
            "order_blocks": [],
            "sweeps": [],
        }
        out = setup_gate.entry_zone(patrones, 1.1000, "BUY", max_span=0.01)
        assert out["zone"]["price"] == 1.0990
        assert out["reason"] == setup_gate.ZONE_OK

    def test_sell_elige_la_zona_mas_cercana_por_encima(self):
        patrones = {
            "fvgs": [
                {"type": "BEARISH_FVG", "bottom": 1.1100, "top": 1.1105},
                {"type": "BEARISH_FVG", "bottom": 1.1010, "top": 1.1015},
            ],
            "order_blocks": [],
            "sweeps": [],
        }
        out = setup_gate.entry_zone(patrones, 1.1000, "SELL", max_span=0.01)
        assert out["zone"]["price"] == 1.1015

    def test_zona_demasiado_lejos_se_rechaza(self):
        patrones = {"fvgs": [{"type": "BULLISH_FVG", "bottom": 1.0000, "top": 1.0005}],
                    "order_blocks": [], "sweeps": []}
        out = setup_gate.entry_zone(patrones, 1.1000, "BUY", max_span=0.01)
        assert out["zone"] is None
        assert out["reason"] == setup_gate.ZONE_TOO_FAR

    def test_sin_zona_en_el_eje(self):
        out = setup_gate.entry_zone({"fvgs": [], "order_blocks": [], "sweeps": []},
                                    1.1000, "BUY", max_span=0.01)
        assert out["zone"] is None
        assert out["reason"] == setup_gate.ZONE_NO_AXIS


# ---------------------------------------------------------------------------
# max_zone_span / plan_levels
# ---------------------------------------------------------------------------


class TestPlanLevels:
    def test_buy_sl_debajo_tp_arriba(self):
        out = setup_gate.plan_levels(1.1000, "BUY", 0.0012, 2.0, digits=5)
        assert out["sl"] == 1.0988
        assert out["tp"] == 1.1024

    def test_sell_espejo(self):
        out = setup_gate.plan_levels(1.1000, "SELL", 0.0012, 2.0, digits=5)
        assert out["sl"] == 1.1012
        assert out["tp"] == 1.0976

    def test_sin_distancia_no_hay_niveles(self):
        out = setup_gate.plan_levels(1.1000, "BUY", None, 2.0, digits=5)
        assert out["sl"] is None and out["tp"] is None

    def test_span_es_multiplo_de_sl(self):
        assert setup_gate.max_zone_span(CFG, 0.0012) == 0.0012 * 8.0

    def test_span_absoluto_sin_sl(self):
        assert setup_gate.max_zone_span({}, None) == setup_gate.ENTRY_ZONE_MAX_SPAN


# ---------------------------------------------------------------------------
# evaluate_gate
# ---------------------------------------------------------------------------


class TestEvaluateGate:
    def test_setup_limpio_aprueba(self):
        plan = setup_gate.evaluate_gate(_snapshot(), CFG)
        assert plan["direction"] == "BUY"
        assert plan["approved"] is True
        assert plan["entry"] == 1.1000
        assert plan["sl"] == 1.0988 and plan["tp"] == 1.1024
        assert plan["killzone"]["in_killzone"] is True

    def test_killzone_no_es_un_and(self):
        """Fuera de ventana el score baja, pero un umbral 0 sigue aprobando (D-007)."""
        fuera = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
        plan = setup_gate.evaluate_gate(_snapshot(now=fuera), CFG)
        assert plan["killzone"]["in_killzone"] is False
        assert plan["approved"] is True

    def test_umbral_alto_rechaza_por_score(self):
        cfg = dict(CFG, min_score=99.0)
        plan = setup_gate.evaluate_gate(_snapshot(now=datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)), cfg)
        assert plan["approved"] is False
        assert any("umbral" in r for r in plan["reasons"])
        assert plan["min_score_used"] == 99.0

    def test_sin_sl_configurado_rechaza(self):
        plan = setup_gate.evaluate_gate(_snapshot(), {"tp_ratio_r": 2.0, "min_score": 0.0})
        assert plan["approved"] is False
        assert plan["sl"] is None

    def test_sin_snapshot_devuelve_none(self):
        assert setup_gate.evaluate_gate(None, CFG) is None

    def test_sin_scores_devuelve_none(self):
        assert setup_gate.evaluate_gate({"symbol": "EURUSD"}, CFG) is None

    def test_el_umbral_por_parametro_manda(self):
        plan = setup_gate.evaluate_gate(_snapshot(), CFG, min_score=0.0)
        assert plan["min_score_used"] == 0.0

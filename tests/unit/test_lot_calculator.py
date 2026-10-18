"""Tests de `core.lot_calculator`: dinero -> lotes, siempre hacia abajo.

El cálculo es puro: se le pasa el balance, el riesgo en % y el `SymbolSpec` (o los
números). Aquí se fijan los contratos:

  - Pérdida por lote en valor de tick, con caída a `contract_size` si falta tick.
  - Los lotes se redondean SIEMPRE hacia abajo al `volume_step` (nunca se pasa del
    riesgo).
  - Por debajo del mínimo del símbolo, `clamped_to_min=True` (el riesgo real excede
    el presupuesto y quien llama tiene que saberlo).
  - Sin datos suficientes, `lots=None`: no se inventa un tamaño.
"""

from __future__ import annotations

from types import SimpleNamespace

from core import lot_calculator as lot


def _eur() -> SimpleNamespace:
    return SimpleNamespace(
        symbol="EURUSD", contract_size=100000.0, tick_size=0.00001, tick_value=1.0,
        digits=5, volume_min=0.01, volume_max=100.0, volume_step=0.01,
    )


def _oro() -> SimpleNamespace:
    return SimpleNamespace(
        symbol="XAUUSD", contract_size=100.0, tick_size=0.01, tick_value=1.0,
        digits=2, volume_min=0.01, volume_max=50.0, volume_step=0.01,
    )


class TestRiskPerLot:
    def test_por_tick(self):
        assert lot.risk_per_lot(0.0012, lot.lot_spec_from_symbol(_eur())) == 120.0

    def test_oro_por_tick(self):
        assert lot.risk_per_lot(3.0, lot.lot_spec_from_symbol(_oro())) == 300.0

    def test_fallback_a_contract_size_sin_tick(self):
        spec = SimpleNamespace(symbol="EURUSD", contract_size=100000.0,
                               tick_size=0.0, tick_value=0.0, digits=5,
                               volume_min=0.01, volume_max=100.0, volume_step=0.01)
        assert lot.risk_per_lot(0.0012, lot.lot_spec_from_symbol(spec)) == 120.0

    def test_sin_datos_devuelve_none(self):
        spec = SimpleNamespace(symbol="X", contract_size=0.0, tick_size=0.0,
                               tick_value=0.0, digits=2)
        assert lot.risk_per_lot(0.0012, lot.lot_spec_from_symbol(spec)) is None


class TestStandardForexLots:
    def test_eur_redondea_hacia_abajo(self):
        out = lot.standard_forex_lots(25000, 0.5, 0.0012, _eur())
        assert out["lots"] == 1.04
        assert out["risk_money"] == 125.0
        assert out["clamped_to_min"] is False

    def test_riesgo_nunca_excede_el_presupuesto(self):
        out = lot.standard_forex_lots(25000, 0.5, 0.0012, _eur())
        assert out["lots"] * out["risk_per_lot"] <= out["risk_money"]

    def test_oro(self):
        out = lot.standard_forex_lots(25000, 0.5, 3.0, _oro())
        assert out["lots"] == 0.41

    def test_por_debajo_del_minimo_marca_clamped(self):
        out = lot.standard_forex_lots(100, 0.5, 0.0012, _eur())
        assert out["lots"] == 0.01
        assert out["clamped_to_min"] is True

    def test_balance_invalido_no_inventa_lotes(self):
        assert lot.standard_forex_lots(0, 0.5, 0.0012, _eur())["lots"] is None
        assert lot.standard_forex_lots(25000, 0, 0.0012, _eur())["lots"] is None

    def test_sin_sl_no_hay_lotes(self):
        assert lot.standard_forex_lots(25000, 0.5, None, _eur())["lots"] is None

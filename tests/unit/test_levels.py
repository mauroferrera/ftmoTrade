"""Tests de `core.levels`: las líneas de soporte/resistencia.

Se fijan los contratos de cada capa: swings más recientes, PDH/PDL, y números
redondos ADAPTADOS al rango (no un paso fijo que en oro serían céntimos y en
EURUSD miles de líneas).
"""

from __future__ import annotations

from core import levels


def _serie_triangular(n: int = 15, base: float = 1.10, wick: float = 0.005,
                      amp: float = None):
    """Velas con un único swing high y un único swing low (ambos en el centro).

    `high` decrece con la distancia al centro y `low` crece con ella, así que el
    centro es a la vez el máximo y el mínimo de la ventana: un pivote limpio de
    cada lado. El rango visible es `[base-wick, base+wick]`.
    """
    centro = n // 2
    if amp is None:
        amp = wick / 2.0  # < wick para que en los extremos high siga por encima de low
    velas = []
    for i in range(n):
        f = abs(i - centro) / centro
        h = (base + wick) - amp * f
        l = (base - wick) + amp * f
        velas.append({"time": i, "open": (h + l) / 2, "high": h,
                      "low": l, "close": (h + l) / 2})
    return velas


class TestSwingLevels:
    def test_devuelve_el_swing_high_y_el_swing_low(self):
        velas = _serie_triangular(base=1.10, wick=0.005)
        swings = levels.swing_levels(velas)
        highs = [s for s in swings if s["kind"] == levels.KIND_SWING_HIGH]
        lows = [s for s in swings if s["kind"] == levels.KIND_SWING_LOW]
        assert len(highs) == 1
        assert len(lows) == 1
        assert highs[0]["price"] == 1.105
        assert lows[0]["price"] == 1.095

    def test_cada_nivel_lleva_kind_price_y_label(self):
        for lv in levels.swing_levels(_serie_triangular()):
            assert set(lv) == {"kind", "price", "label"}

    def test_sin_velas_no_hay_swings(self):
        assert levels.swing_levels([]) == []


class TestPdLevels:
    def test_devuelve_pdh_y_pdl(self):
        out = levels.pd_levels(1.1234, 1.1000)
        assert [lv["label"] for lv in out] == ["PDH", "PDL"]
        assert out[0]["price"] == 1.1234
        assert out[1]["price"] == 1.1000

    def test_los_niveles_desconocidos_se_omiten(self):
        assert levels.pd_levels(None, None) == []
        assert [lv["label"] for lv in levels.pd_levels(1.2, None)] == ["PDH"]
        assert [lv["label"] for lv in levels.pd_levels(None, 1.1)] == ["PDL"]


class TestRoundLevels:
    def test_los_niveles_caen_dentro_del_rango_y_son_crecientes(self):
        velas = _serie_triangular()
        out = levels.round_levels(velas)
        assert out, "se esperaba al menos un número redondo"
        precios = [lv["price"] for lv in out]
        assert precios == sorted(precios)
        lo = min(c["low"] for c in velas)
        hi = max(c["high"] for c in velas)
        assert all(lo - 1e-6 <= p <= hi + 1e-6 for p in precios)

    def test_el_paso_se_adapta_al_rango(self):
        """Un rango estrecho (forex) y uno ancho (oro) no dan el mismo número de líneas."""
        estrecho = _serie_triangular(base=1.0, wick=0.002)   # span 0.004
        ancho = _serie_triangular(base=2595.0, wick=5.0)     # span 10
        n_estrecho = len(levels.round_levels(estrecho))
        n_ancho = len(levels.round_levels(ancho))
        # El paso "bonito" mantiene el conteo en un orden de magnitud parecido:
        # el rango ancho no genera cientos de líneas ni el estrecho una sola.
        assert 2 <= n_estrecho <= 20
        assert 2 <= n_ancho <= 20

    def test_sin_velas_no_hay_redondos(self):
        assert levels.round_levels([]) == []


class TestBuildLevels:
    def test_junta_pd_swings_y_redondos(self):
        velas = _serie_triangular()
        out = levels.build_levels(velas, pdh=1.31, pdl=0.99)
        kinds = {lv["kind"] for lv in out}
        assert levels.KIND_PD in kinds
        assert levels.KIND_SWING_HIGH in kinds
        assert levels.KIND_SWING_LOW in kinds
        assert levels.KIND_ROUND in kinds
        for lv in out:
            assert set(lv) == {"kind", "price", "label"}

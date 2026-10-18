"""Tests de las reglas de prop firm FTMO (`core/prop_rules.py`).

Lo que se verifica no es la aritmética de un porcentaje, sino las decisiones que
distinguen un shell de FTMO de un tracker genérico de drawdown:

1. Los DOS productos tienen reglas distintas (2-Step estático / 1-Step trailing).
2. La pérdida diaria se mide desde el BALANCE DE INICIO DE DÍA, no desde un pico.
3. Los cortes internos son más estrechos que los de FTMO y son los que bloquean.
4. El Best Day Rule existe solo en 1-Step y AVISA, no bloquea.
5. El dimensionado se ancla al buffer de DD total, no al balance.

Run:  python -m pytest tests/unit/test_prop_rules.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import prop_rules as pr


CAPITAL = 25_000.0


def _2step(**kw):
    return pr.ftmo_profile(pr.FTMO_2STEP, CAPITAL, **kw)


def _1step(**kw):
    return pr.ftmo_profile(pr.FTMO_1STEP, CAPITAL, **kw)


class TestPerfil:
    def test_2step_25k_numeros_congelados(self):
        p = _2step()
        assert p.ftmo_daily_usd == pytest.approx(1_250.0)
        assert p.ftmo_total_usd == pytest.approx(2_500.0)
        assert p.internal_daily_usd == pytest.approx(1_000.0)
        assert p.internal_total_usd == pytest.approx(1_750.0)
        assert p.target_usd == pytest.approx(2_500.0)
        assert p.risk_usd == pytest.approx(125.0)
        assert p.min_trading_days == 4
        assert p.best_day_rule is False
        assert p.ftmo_total_trailing is False

    def test_1step_25k_numeros_congelados(self):
        p = _1step()
        assert p.ftmo_daily_usd == pytest.approx(750.0)
        assert p.internal_daily_usd == pytest.approx(625.0)
        assert p.internal_total_usd == pytest.approx(1_750.0)
        assert p.target_usd == pytest.approx(2_500.0)
        assert p.min_trading_days == 0
        assert p.best_day_rule is True
        assert p.ftmo_total_trailing is True

    def test_verification_baja_el_objetivo_a_la_mitad(self):
        p = pr.ftmo_profile(pr.FTMO_2STEP, CAPITAL, phase="verification")
        assert p.target_usd == pytest.approx(1_250.0)

    def test_1step_no_tiene_fase_verification(self):
        with pytest.raises(ValueError) as exc:
            pr.ftmo_profile(pr.FTMO_1STEP, CAPITAL, phase="verification")
        assert "challenge" in str(exc.value)

    def test_producto_desconocido_lista_los_validos(self):
        with pytest.raises(ValueError) as exc:
            pr.ftmo_profile("3step", CAPITAL)
        assert "2step" in str(exc.value) and "1step" in str(exc.value)

    def test_capital_no_positivo_es_error(self):
        with pytest.raises(ValueError):
            pr.ftmo_profile(pr.FTMO_2STEP, 0.0)
        with pytest.raises(ValueError):
            pr.ftmo_profile(pr.FTMO_2STEP, -1.0)


class TestCortesInternos:
    """El corte interno que no es más estrecho que el de FTMO no protege de nada."""

    def test_interno_diario_no_puede_igualar_al_de_ftmo(self):
        with pytest.raises(ValueError) as exc:
            _2step(internal_daily_pct=5.0)
        assert "internal_daily_pct" in str(exc.value)

    def test_interno_total_no_puede_igualar_al_de_ftmo(self):
        with pytest.raises(ValueError) as exc:
            _2step(internal_total_pct=10.0)
        assert "internal_total_pct" in str(exc.value)

    def test_se_pueden_afinar_mas_estrechos(self):
        p = _2step(internal_daily_pct=2.0, internal_total_pct=5.0)
        assert p.internal_daily_usd == pytest.approx(500.0)
        assert p.internal_total_usd == pytest.approx(1_250.0)

    def test_interno_no_positivo_es_error(self):
        with pytest.raises(ValueError):
            _2step(internal_daily_pct=0.0)


class TestPerdidaDiariaDesdeInicioDeDia:
    """FTMO mide la pérdida diaria contra el balance de las 00:00, no contra un pico."""

    def _estado(self, equity=CAPITAL):
        return pr.new_state(_2step(), equity, day_key="2026-10-14")

    def test_sin_perdida_el_room_es_el_tope_completo(self):
        p = _2step()
        r = pr.evaluate(p, self._estado(), CAPITAL)
        assert r["blocked"] is False
        assert r["dd_daily_usd"] == pytest.approx(0.0)
        assert r["daily_room_usd"] == pytest.approx(1_000.0)

    def test_bloquea_al_tocar_el_corte_interno_diario(self):
        p = _2step()
        r = pr.evaluate(p, self._estado(), CAPITAL - 1_000.0)
        assert r["blocked"] is True
        assert any("diario" in x for x in r["reasons"])

    def test_justo_por_encima_del_corte_no_bloquea(self):
        p = _2step()
        r = pr.evaluate(p, self._estado(), CAPITAL - 999.0)
        assert r["blocked"] is False

    def test_subir_y_devolver_no_cuenta_como_perdida_del_dia(self):
        """Un pico intradía NO sube el suelo: solo cuenta bajar del inicio de día.

        Si la equity sube a 25.500 y luego cae a 25.200, sigue por encima del
        balance de inicio (25.000) y la pérdida del día es CERO. Medir desde el
        pico daría 300 USD de "drawdown" y bloquearía antes de lo que FTMO bloquea.
        """
        p = _2step()
        estado = self._estado()
        pr.evaluate(p, estado, CAPITAL + 500.0)
        r = pr.evaluate(p, estado, CAPITAL + 200.0)
        assert r["dd_daily_usd"] == pytest.approx(0.0)
        assert r["blocked"] is False


class TestPerdidaTotal:
    def _estado_ya_en_perdida(self, p, day_start):
        """Estado de un día que YA arranca en pérdida, para aislar el tope TOTAL.

        Arrancar el día a 23.2xx significa que la pérdida de HOY es cero: así el
        único tope que se puede disparar es el total. Sin esto, bajar a 23.250 en
        un solo día también cruzaría el corte diario (1.000) y el test no
        distinguiría qué regla bloqueó.
        """
        estado = pr.new_state(p, CAPITAL, day_key="d2")
        estado["day_start_balance"] = float(day_start)
        return estado

    def test_2step_estatico_se_mide_contra_el_capital_inicial(self):
        p = _2step()
        # 25.000 - 1.750 = 23.250 es el corte interno total.
        assert pr.evaluate(p, self._estado_ya_en_perdida(p, 23_250.0),
                           23_250.0)["blocked"] is True
        assert pr.evaluate(p, self._estado_ya_en_perdida(p, 23_251.0),
                           23_251.0)["blocked"] is False

    def test_2step_el_suelo_no_sube_aunque_la_cuenta_suba(self):
        """Estático: ganar 3.000 no mueve el suelo de pérdida total."""
        p = _2step()
        pr.evaluate(p, self._estado_ya_en_perdida(p, CAPITAL), CAPITAL + 3_000.0)
        # El corte sigue siendo 23.250: por encima, no bloqueado.
        assert pr.evaluate(p, self._estado_ya_en_perdida(p, 23_500.0),
                           23_500.0)["blocked"] is False

    def test_1step_trailing_sube_el_suelo_con_el_cierre_mas_alto(self):
        p = _1step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        estado = pr.roll_day(p, estado, new_day_key="d2",
                             day_close_balance=26_000.0, day_pnl=1_000.0,
                             traded_today=True)
        estado["day_start_balance"] = 23_500.0  # aísla el tope total del diario
        # Suelo nuevo: 26.000 - 1.750 = 24.250. Un 23.500 que en estático pasaría,
        # aquí ya bloquea.
        r = pr.evaluate(p, estado, 23_500.0)
        assert r["blocked"] is True
        assert any("trailing" in x for x in r["reasons"])

    def test_buffer_restante_es_el_tope_interno_menos_la_perdida(self):
        p = _2step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        assert pr.buffer_remaining_usd(p, estado, CAPITAL - 500.0) == pytest.approx(1_250.0)


class TestBestDayRule:
    def test_solo_aplica_a_1step(self):
        p = _2step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        estado["best_day_profit"] = 900.0
        estado["positive_days_profit"] = 1_000.0
        r = pr.evaluate(p, estado, CAPITAL)
        assert r["best_day_ratio"] is None
        assert r["warnings"] == []

    def test_1step_avisa_pero_no_bloquea(self):
        p = _1step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        estado["best_day_profit"] = 500.0
        estado["positive_days_profit"] = 800.0  # 62.5% > 50%
        r = pr.evaluate(p, estado, CAPITAL)
        assert r["blocked"] is False
        assert r["best_day_ratio"] == pytest.approx(0.625)
        assert any("Best Day" in x for x in r["warnings"])

    def test_ratio_dentro_del_limite_no_avisa(self):
        p = _1step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        estado["best_day_profit"] = 400.0
        estado["positive_days_profit"] = 1_000.0  # 40% <= 50%
        r = pr.evaluate(p, estado, CAPITAL)
        assert r["best_day_ratio"] == pytest.approx(0.4)
        assert r["warnings"] == []

    def test_sin_dias_positivos_no_hay_ratio(self):
        p = _1step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        assert pr.evaluate(p, estado, CAPITAL)["best_day_ratio"] is None


class TestRollDay:
    def test_un_dia_operado_cuenta_para_los_dias_minimos(self):
        p = _2step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        estado = pr.roll_day(p, estado, new_day_key="d2",
                             day_close_balance=CAPITAL, day_pnl=-100.0,
                             traded_today=True)
        assert estado["trading_days"] == 1
        assert estado["day_key"] == "d2"
        assert estado["day_start_balance"] == pytest.approx(CAPITAL)

    def test_un_dia_sin_operar_no_cuenta(self):
        p = _2step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        estado = pr.roll_day(p, estado, new_day_key="d2",
                             day_close_balance=CAPITAL, day_pnl=0.0,
                             traded_today=False)
        assert estado["trading_days"] == 0

    def test_dia_positivo_alimenta_el_best_day(self):
        p = _1step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        for key, pnl in (("d2", 300.0), ("d3", 500.0), ("d4", 150.0)):
            estado = pr.roll_day(p, estado, new_day_key=key,
                                 day_close_balance=CAPITAL + pnl, day_pnl=pnl,
                                 traded_today=True)
        assert estado["positive_days_profit"] == pytest.approx(950.0)
        assert estado["best_day_profit"] == pytest.approx(500.0)

    def test_dia_negativo_no_alimenta_los_positivos(self):
        p = _1step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        estado = pr.roll_day(p, estado, new_day_key="d2",
                             day_close_balance=CAPITAL - 200.0, day_pnl=-200.0,
                             traded_today=True)
        assert estado["positive_days_profit"] == 0.0
        assert estado["trading_days"] == 1

    def test_trailing_hwm_solo_sube(self):
        p = _1step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        estado = pr.roll_day(p, estado, new_day_key="d2",
                             day_close_balance=26_000.0, day_pnl=1_000.0,
                             traded_today=True)
        estado = pr.roll_day(p, estado, new_day_key="d3",
                             day_close_balance=25_200.0, day_pnl=-800.0,
                             traded_today=True)
        assert estado["hwm_balance"] == pytest.approx(26_000.0)

    def test_estatico_no_mueve_el_hwm(self):
        p = _2step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        estado = pr.roll_day(p, estado, new_day_key="d2",
                             day_close_balance=26_000.0, day_pnl=1_000.0,
                             traded_today=True)
        assert estado["hwm_balance"] == pytest.approx(CAPITAL)

    def test_no_muta_el_estado_original(self):
        p = _2step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        pr.roll_day(p, estado, new_day_key="d2",
                    day_close_balance=CAPITAL, day_pnl=0.0, traded_today=True)
        assert estado["day_key"] == "d1"


class TestDimensionadoSobreBuffer:
    """El riesgo se ancla al buffer de DD total, no al balance."""

    def test_riesgo_nominal_es_el_05_por_ciento_del_capital(self):
        assert pr.nominal_risk_usd(_2step()) == pytest.approx(125.0)

    def test_riesgo_por_buffer_coincide_con_el_nominal(self):
        # 5% de $2.500 de buffer = $125, la misma cifra por dos caminos.
        assert pr.buffer_risk_usd(_2step()) == pytest.approx(125.0)

    def test_fraccion_de_buffer_es_escalable(self):
        assert pr.buffer_risk_usd(_2step(), 0.10) == pytest.approx(250.0)

    def test_buffer_es_el_diez_por_ciento_del_capital(self):
        p = _2step()
        assert p.buffer_usd == pytest.approx(2_500.0)


class TestProgreso:
    def test_sin_cambios_el_progreso_es_cero(self):
        p = _2step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        r = pr.evaluate(p, estado, CAPITAL)
        assert r["target_progress_pct"] == pytest.approx(0.0)
        assert r["target_remaining_usd"] == pytest.approx(2_500.0)

    def test_mitad_del_objetivo(self):
        p = _2step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        r = pr.evaluate(p, estado, CAPITAL + 1_250.0)
        assert r["target_progress_pct"] == pytest.approx(50.0)
        assert r["target_remaining_usd"] == pytest.approx(1_250.0)

    def test_verification_mide_contra_su_propio_objetivo(self):
        p = pr.ftmo_profile(pr.FTMO_2STEP, CAPITAL, phase="verification")
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        r = pr.evaluate(p, estado, CAPITAL + 625.0)
        assert r["target_progress_pct"] == pytest.approx(50.0)

    def test_dias_minimos_se_reportan(self):
        p = _2step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        assert pr.evaluate(p, estado, CAPITAL)["min_trading_days_met"] is False
        estado["trading_days"] = 4
        assert pr.evaluate(p, estado, CAPITAL)["min_trading_days_met"] is True

    def test_1step_no_exige_dias_minimos(self):
        p = _1step()
        estado = pr.new_state(p, CAPITAL, day_key="d1")
        assert pr.evaluate(p, estado, CAPITAL)["min_trading_days_met"] is True


class TestPureza:
    def test_no_importa_la_configuracion_ni_el_adaptador(self):
        source = Path(pr.__file__).read_text(encoding="utf-8")
        for prohibido in ("open(", "import yaml", "adapters", "import api"):
            assert prohibido not in source

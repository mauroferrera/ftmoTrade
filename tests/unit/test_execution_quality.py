"""`core/execution_quality`: lo que mide la calidad de llenado y por qué bloquea.

El módulo es puro y se prueba entero sin bróker. Lo que se comprueba aquí no es que
calcule bien la aritmética —eso lo hace cualquier calculadora— sino que:

1. **La deriva se mide en CONTRA.** Un BUY que entra por encima de lo planificado y
   un SELL que entra por debajo son el mismo error con signos distintos. Si el signo
   se calculara por la dirección equivocada, un drift favorable —entrar mejor de lo
   previsto— se contaría como deriva y bloquearía una operación buena. Es el error
   que hace que un gate de calidad termine siendo ruido.
2. **El denominador es la distancia PLANEADA**, no la efectiva. Es la diferencia
   entre una regla y una letra muerta (ver el docstring de `evaluate`).
3. **Un dato que no se puede medir NO rechaza.** Bloquear por falta de información
   convierte un hueco de datos en un sistema parado, que es el peor sitio para
   descubrir que faltaba un campo.
"""

from __future__ import annotations

import pytest

from core import execution_quality as eq
from core.execution_quality import (
    DEFAULT_MAX_SLIPPAGE_SL_SHARE,
    DEFAULT_MAX_SPREAD_SHARE_OF_SL,
    DEFAULT_MIN_INVALIDATION_SL_SHARE,
    evaluate,
    measure,
    reason_code,
    spread_points,
)

PIP = 0.0001
POINT = 0.00001


# ---------------------------------------------------------------------------
# La medida
# ---------------------------------------------------------------------------


def test_deriva_adversa_misma_magnitud_en_buy_y_en_sell():
    """Entrar 3 pips peor de lo previsto es +3 pips en BUY y +3 pips en SELL.

    Es lo que hace que las dos ramas comparen con el mismo criterio. Con el signo
    mal puesto, uno de los dos lados contaría como favorable.
    """
    buy = measure(entry=1.10100, sl=1.09900, tp=1.10500, direction="BUY",
                  point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                  invalidate_level=1.09800)
    sell = measure(entry=1.09900, sl=1.10100, tp=1.09500, direction="SELL",
                   point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                   invalidate_level=1.10200)
    assert buy["slippage_adverse_price"] == pytest.approx(0.00100)
    assert sell["slippage_adverse_price"] == pytest.approx(0.00100)
    assert buy["slippage_share_of_sl"] == pytest.approx(1.0)
    assert sell["slippage_share_of_sl"] == pytest.approx(1.0)


def test_entrar_mejor_de_lo_planificado_no_es_deriva_adversa():
    """Un BUY por DEBAJO de lo planificado es drift favorable: no bloquea.

    Se mide la magnitud con el signo del precio, pero la regla mira
    `slippage_share_of_sl`, que es el valor absoluto. Y en este caso concreto da
    exactamente el umbral (medio stop), así que este test también fija que en el
    LÍMITE la operación se admite: la regla dice "no puede superar".
    """
    m = measure(entry=1.09950, sl=1.09800, tp=1.10250, direction="BUY",
                point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                invalidate_level=1.09700)
    assert m["slippage_adverse_price"] == pytest.approx(-0.00050)
    assert m["slippage_share_of_sl"] == pytest.approx(0.5)
    v = evaluate(entry=1.09950, sl=1.09800, tp=1.10250, direction="BUY",
                 point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                 invalidate_level=1.09700)
    assert v["approved"] is True


def test_el_umbral_exacto_no_depende_del_redondeo_de_la_coma_flotante():
    """El borde del umbral se decide por la regla, no por el último bit.

    `abs(1.10050 - 1.10000) / 0.00100` da `0.500000000000167` en coma flotante. Con
    un `>` sin tolerancia, esa entrada —que la regla dice que se admite porque dice
    "no puede superar"— se rechazaba. Y el mismo motivo hacía que el test anterior
    pasara o no según la plataforma, que es la forma de que un gate en el borde deje
    de ser un gate.

    El signo del ruido depende de hacia dónde resten los bits, así que la misma
    distancia exacta da 0.500000000000167 con una geometría y 0.49999999999994493
    con otra. Las dos están "en el umbral" y las dos se admiten.
    """
    v_ok = evaluate(entry=1.09950, sl=1.09800, tp=1.10250, direction="BUY",
                    point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                    invalidate_level=1.09700)
    assert v_ok["slippage_share_of_sl"] > 0.5  # la medida cruda SÍ excede...
    assert v_ok["approved"] is True  # ...pero la comparación con tolerancia no lo ve

    v_otro_lado = evaluate(entry=1.10050, sl=1.09900, tp=1.10350,
                           direction="BUY", point=POINT, planned_entry=1.10000,
                           sl_distance=0.00100, invalidate_level=1.09800)
    assert v_otro_lado["slippage_share_of_sl"] < 0.5  # ruido al revés
    assert v_otro_lado["approved"] is True  # y también se admite

    # Y al otro lado del umbral, el rechazo sigue funcionando.
    v2 = evaluate(entry=1.10051, sl=1.09900, tp=1.10352, direction="BUY",
                  point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                  invalidate_level=1.09800)
    assert v2["approved"] is False
    assert eq.reason_code(v2["reasons"][0]) == eq.REASON_DERIVA


def test_drift_a_medio_stop_se_aproxima_al_umbral_por_arriba():
    """El umbral de 0.5 significa "medio stop más lejos", medido así.

    Con el denominador equivocado (la geometría efectiva) una deriva de medio stop
    daría 0.33 y no superaría el umbral hasta un stop entero, que es exactamente lo
    contrario de lo que dice la constante.
    """
    v = evaluate(entry=1.10050, sl=1.09900, tp=1.10350, direction="BUY",
                 point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                 invalidate_level=1.09800)
    assert v["slippage_share_of_sl"] == pytest.approx(0.5)
    # Justo en el umbral no se rechaza (es `>`, no `>=`): la regla dice "no puede
    # superar", y en el límite exacto sigue siendo el setup aprobado.
    assert v["approved"] is True

    v2 = evaluate(entry=1.10060, sl=1.09900, tp=1.10380, direction="BUY",
                  point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                  invalidate_level=1.09800)
    assert v2["approved"] is False
    assert eq.reason_code(v2["reasons"][0]) == eq.REASON_DERIVA
    assert "pips en contra" in v2["reasons"][0]


def test_el_umbral_se_puede_bajar_desde_la_config():
    """Con `max_slippage_sl_share` alto, una deriva que bloquearía deja pasar."""
    v_base = evaluate(entry=1.10060, sl=1.09900, tp=1.10380, direction="BUY",
                      point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                      invalidate_level=1.09800)
    assert v_base["approved"] is False
    v = evaluate(entry=1.10060, sl=1.09900, tp=1.10380, direction="BUY",
                 point=POINT, planned_entry=1.10000, sl_distance=0.00100,
                 invalidate_level=1.09800, cfg={"max_slippage_sl_share": 0.75})
    assert v["approved"] is True
    assert v["thresholds"]["max_slippage_sl_share"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Invalidez
# ---------------------------------------------------------------------------


def test_invalidez_demasiado_cerca_rechaza():
    """Entrada a menos de un cuarto del stop del nivel que mata el setup."""
    v = evaluate(entry=1.09900, sl=1.10000, tp=1.09700, direction="BUY",
                 point=POINT, sl_distance=0.00100,
                 invalidate_level=1.09880, bid=1.09900, ask=1.09905)
    assert v["approved"] is False
    assert eq.reason_code(v["reasons"][0]) == eq.REASON_INVALIDEZ
    assert v["invalidation_share_of_sl"] == pytest.approx(0.2)


def test_invalidez_con_margen_pasa():
    v = evaluate(entry=1.10000, sl=1.09900, tp=1.10200, direction="BUY",
                 point=POINT, sl_distance=0.00100,
                 invalidate_level=1.09800, bid=1.10000, ask=1.10005)
    assert v["approved"] is True
    assert v["invalidation_share_of_sl"] == pytest.approx(2.0)


def test_invalidez_ya_cruzada_es_distancia_negativa():
    """La entrada YA CRUZÓ la invalidez: distancia negativa.

    `risk_engine.validate_entry` también lo rechaza por su puerta; aquí se mide
    para que la fila diga por qué.
    """
    v = evaluate(entry=1.09750, sl=1.09650, tp=1.09950, direction="BUY",
                 point=POINT, sl_distance=0.00100,
                 invalidate_level=1.09800, bid=1.09750, ask=1.09755)
    assert v["invalidation_share_of_sl"] == pytest.approx(-0.5)
    assert v["approved"] is False


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------


def test_spread_que_se_come_un_tercio_del_stop_se_rechaza():
    """Un tercio del stop se va en el spread antes de que el trade trabaje."""
    v = evaluate(entry=1.10000, sl=1.09900, tp=1.10200, direction="BUY",
                 point=POINT, sl_distance=0.00100,
                 bid=1.10000, ask=1.10034)
    assert v["spread_points"] == pytest.approx(34.0)
    assert v["spread_share_of_sl"] == pytest.approx(0.34)
    assert v["approved"] is False
    assert eq.reason_code(v["reasons"][0]) == eq.REASON_SPREAD
    assert "spread" in v["reasons"][0]


def test_spread_pequeno_pasa():
    v = evaluate(entry=1.10000, sl=1.09900, tp=1.10200, direction="BUY",
                 point=POINT, sl_distance=0.00100,
                 bid=1.10000, ask=1.10008)
    assert v["approved"] is True


def test_spread_en_puntos_usa_el_point_del_simbolo():
    """En EURUSD (5 decimales) 1.2 pips son 12 puntos, y al revés."""
    assert spread_points(1.10000, 1.10012, POINT) == pytest.approx(12.0)
    assert spread_points(109.330, 109.336, 0.001) == pytest.approx(6.0)


def test_spread_cero_es_un_dato_measure_no_un_hueco():
    """Un spread de 0 puntos SÍ es un número medido y se conserva.

    Confundirlo con "no medido" haría que una entrada exactamente en el nivel
    planificado pareciera un dato que falta, y `unknown` llenaría la fila.
    """
    m = measure(entry=1.10000, sl=1.09900, tp=1.10200, direction="BUY",
                point=POINT, sl_distance=0.00100, bid=1.10000, ask=1.10000)
    assert m["spread_points"] == pytest.approx(0.0)
    assert m["spread_share_of_sl"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Datos que faltan: NO rechazan
# ---------------------------------------------------------------------------


def test_sin_planned_entry_no_rechaza_y_marca_unknown():
    v = evaluate(entry=1.10000, sl=1.09900, tp=1.10200, direction="BUY",
                 point=POINT, sl_distance=0.00100,
                 invalidate_level=1.09800, bid=1.10000, ask=1.10005)
    assert v["approved"] is True
    assert v["verdict"] == "sin_medir"
    assert "planned_entry" in v["unknown"]


def test_sin_direccion_valida_no_rechaza_y_marca_unknown():
    v = evaluate(entry=1.10000, sl=1.09900, tp=1.10200, direction="",
                 point=POINT, sl_distance=0.00100,
                 invalidate_level=1.09800, bid=1.10000, ask=1.10005)
    assert v["approved"] is True
    assert "direccion" in v["unknown"]


def test_sin_invalidacion_no_rechaza_y_marca_unknown():
    v = evaluate(entry=1.10000, sl=1.09900, tp=1.10200, direction="BUY",
                 point=POINT, sl_distance=0.00100,
                 bid=1.10000, ask=1.10005)
    assert v["approved"] is True
    assert "invalidation_level" in v["unknown"]


def test_sin_spread_no_rechaza_y_marca_unknown():
    v = evaluate(entry=1.10000, sl=1.09900, tp=1.10200, direction="BUY",
                 point=POINT, sl_distance=0.00100,
                 invalidate_level=1.09800, bid=None, ask=None)
    assert v["approved"] is True
    assert "spread" in v["unknown"]


def test_sin_geometria_no_dictamina_y_lo_dice():
    v = evaluate(entry=1.10000, sl=None, tp=1.10200, direction="BUY",
                 point=POINT, sl_distance=0.00100, invalidate_level=1.09800)
    assert v["approved"] is True
    assert v["verdict"] == "sin_geometria"
    assert v["reasons"] == []
    assert v["unknown"] == ["sl_distance"]


# ---------------------------------------------------------------------------
# Normalización y bordes
# ---------------------------------------------------------------------------


def test_direccion_se_normaliza_para_medir_y_para_publicar():
    v = measure(entry=1.09900, sl=1.10100, tp=1.09500, direction=" sell ",
                point=POINT, planned_entry=1.10000, sl_distance=0.00100)
    assert v["direction"] == "SELL"
    assert v["slippage_adverse_price"] == pytest.approx(0.00100)


def test_razon_code_extrae_el_codigo_del_texto():
    assert reason_code("SPREAD_sobre_SL: ...") == "SPREAD_sobre_SL"
    assert reason_code("DERIVA_ENTRADA: x") == "DERIVA_ENTRADA"
    assert reason_code("") == ""
    assert reason_code(None) == ""


def test_bool_no_cola_como_numero():
    assert spread_points(True, 1.10005, POINT) is None
    v = evaluate(entry=True, sl=1.09900, tp=1.10200, direction="BUY",
                 point=POINT, sl_distance=0.00100, invalidate_level=1.09800)
    # entry no numérico -> sin geometría; no se inventa un 1.0
    assert v["verdict"] == "sin_geometria"


def test_spread_negativo_o_point_invalido_es_no_medido():
    assert spread_points(1.10008, 1.10000, POINT) is None
    assert spread_points(1.10000, 1.10008, 0) is None
    assert spread_points(1.10000, 1.10008, None) is None


def test_defaults_de_produccion():
    assert DEFAULT_MAX_SLIPPAGE_SL_SHARE == 0.5
    assert DEFAULT_MAX_SPREAD_SHARE_OF_SL == 0.25
    assert DEFAULT_MIN_INVALIDATION_SL_SHARE == 0.25

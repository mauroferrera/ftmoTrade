"""El puerto de ejecución: lo que se manda, lo que NO se manda, y lo que se dice.

Estos tests son la última línea antes de que una orden llegue al bróker. Están
escritos contra la FORMA REAL de MT5 (`order_send` devuelve un objeto o `None`,
las posiciones son objetos, el enum de llenado vale 0 y 1), porque un doble
más cómodo que la realidad certifica como bueno un código que no funciona: ya
pasó con `symbols_get(group="\\*\\*\\*")`, que daba verde en toda la suite y
devolvía una lista vacía en el bróker.

Cada puerta tiene su test de rechazo. Un "no se manda" sin test es una intención,
y las intenciones se degradan solas la primera vez que hay prisa.
"""

from __future__ import annotations

import pytest

from adapters.base_adapter import TerminalUnavailable
from adapters.mt5 import mt5_execution
from adapters.mt5.mt5_execution import MT5ExecutionAdapter, adapter, fillings_for
from adapters.mt5.mt5_forex import Session
from tests.unit.mt5_fake import (
    FakeMT5,
    FakePosition,
    FakeSymbolInfo,
    FakeSymbolInfoTrade,
    FakeTick,
    FakeTradeResult,
)

EURUSD_TICK = FakeTick(time=1_700_000_000, bid=1.10000, ask=1.10012)
EURUSD_TICK_2 = FakeTick(time=1_700_000_001, bid=1.10030, ask=1.10042)


@pytest.fixture
def mt5(monkeypatch):
    """Doble de MT5 con un EURUSD cotizado y listo para recibir órdenes.

    `filling_mode=1` (solo FOK) es lo que publica MetaQuotes-Demo para EURUSD,
    comprobado contra el bróker real. No es un valor arbitrario: con 3 (FOK+IOC)
    el ciclo de llenado ni se miraría en el símbolo que de verdad se opera.
    """
    fake = FakeMT5.install(monkeypatch)
    fake.symbols_list = [FakeSymbolInfo("EURUSD"), FakeSymbolInfo("DE40")]
    fake.info_by_symbol["EURUSD"] = FakeSymbolInfo("EURUSD", digits=5, point=0.00001)
    fake.info_by_symbol["DE40"] = FakeSymbolInfo("DE40", digits=2, point=0.01)
    fake.trade_by_symbol["EURUSD"] = FakeSymbolInfoTrade(
        "EURUSD", trade_tick_size=0.00001, trade_tick_value=1.0, filling_mode=1
    )
    fake.trade_by_symbol["DE40"] = FakeSymbolInfoTrade(
        "DE40", trade_tick_size=0.01, trade_tick_value=1.0, filling_mode=2
    )
    fake.tick_by_symbol["EURUSD"] = EURUSD_TICK
    fake.tick_by_symbol["DE40"] = FakeTick(time=1_700_000_000, bid=18000.0, ask=18000.5)
    fake.order_send_results = FakeTradeResult(retcode=10009, deal=777, order=778, price=1.10012)
    fake.calc_profit_value = -12.30
    return fake


@pytest.fixture
def session():
    s = Session()
    yield s
    s.shutdown_executor()


@pytest.fixture
def ejec(session):
    return MT5ExecutionAdapter(session)


# ---------------------------------------------------------------------------
# Constantes: el sitio donde el error cuesta dinero
# ---------------------------------------------------------------------------


def test_constantes_coinciden_con_el_paquete(mt5):
    """Los literales del puerto son los del paquete MetaTrader5.

    Se comparan contra el DOBLE, que a su vez los fija a los valores medidos en el
    bróker. Si alguien "corrige" `ORDER_FILLING_FOK` a 1 creyendo que FOK es el
    primero del enum, este test cae — y con él, el envío real, que empezaría a
    mandar IOC a un símbolo que solo admite FOK y recibiría 10030 sin saber por qué.
    """
    assert mt5_execution.ORDER_TYPE_BUY == mt5.ORDER_TYPE_BUY == 0
    assert mt5_execution.ORDER_TYPE_SELL == mt5.ORDER_TYPE_SELL == 1
    assert mt5_execution.TRADE_ACTION_DEAL == mt5.TRADE_ACTION_DEAL == 1
    assert mt5_execution.TRADE_ACTION_SLTP == mt5.TRADE_ACTION_SLTP == 3
    assert mt5_execution.ORDER_TIME_GTC == mt5.ORDER_TIME_GTC == 0
    assert mt5_execution.ORDER_FILLING_FOK == mt5.ORDER_FILLING_FOK == 0
    assert mt5_execution.ORDER_FILLING_IOC == mt5.ORDER_FILLING_IOC == 1
    assert mt5_execution.RETCODE_OK == (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)


def test_mascara_de_llenado_se_lee_con_los_bits_no_con_el_enum():
    """La máscara de `SYMBOL_FILLING_MODE` usa bits; el enum usa valores.

    Son dos numeraciones distintas que se parecen, y confundirlas hace que
    `mask & 0` sea siempre 0: `fillings_for` degeneraría a "probar los dos" para
    cualquier símbolo, que es el fallo silencioso más caro de los dos porque
    produce un envío extra en el bróker en lugar de un error.
    """
    # EURUSD en MetaQuotes-Demo: mask 1 = solo FOK.
    assert fillings_for(1) == [mt5_execution.ORDER_FILLING_FOK]
    # DE40: mask 2 = solo IOC.
    assert fillings_for(2) == [mt5_execution.ORDER_FILLING_IOC]
    # XAUUSD: mask 3 = FOK + IOC.
    assert fillings_for(3) == [mt5_execution.ORDER_FILLING_FOK, mt5_execution.ORDER_FILLING_IOC]
    # Sin máscara publicada: los dos, que es lo único que se puede probar.
    assert fillings_for(0) == [mt5_execution.ORDER_FILLING_FOK, mt5_execution.ORDER_FILLING_IOC]
    assert fillings_for(None) == [mt5_execution.ORDER_FILLING_FOK, mt5_execution.ORDER_FILLING_IOC]


# ---------------------------------------------------------------------------
# Envío: la forma de la petición
# ---------------------------------------------------------------------------


def test_envia_deal_con_la_forma_que_mt5_exige(ejec, mt5):
    res = ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.09900, tp=1.10200,
                                 magic=8882026, comment="ILOF Exec")
    assert res["ok"] is True
    assert res["status"] == "sent"
    assert res["deal"] == 777 and res["order"] == 778

    peticion = mt5.orders_sent[0]
    assert peticion["action"] == mt5_execution.TRADE_ACTION_DEAL
    assert peticion["symbol"] == "EURUSD"
    assert peticion["volume"] == 0.10
    assert peticion["type"] == mt5_execution.ORDER_TYPE_BUY
    assert peticion["magic"] == 8882026
    assert peticion["comment"] == "ILOF Exec"
    assert peticion["type_time"] == mt5_execution.ORDER_TIME_GTC


def test_buy_entra_al_ask_y_sell_al_bid(ejec, mt5):
    """El precio de entrada es el que se PAGA, no el que se muestra.

    Mandar una BUY al bid es un precio que el bróker nunca ha cotizado. Con un
    spread de 1.2 pips, la diferencia no es un detalle de redondeo: es el spread
    del que el gate de entrada no se entera.
    """
    res = ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.099, tp=1.102)
    assert res["ok"]
    assert mt5.orders_sent[0]["price"] == pytest.approx(EURUSD_TICK.ask)

    res = ejec.send_market_order("EURUSD", "SELL", 0.10, sl=1.102, tp=1.099)
    assert res["ok"]
    assert mt5.orders_sent[1]["price"] == pytest.approx(EURUSD_TICK.bid)


def test_niveles_se_alinean_a_la_rejilla_del_simbolo(ejec, mt5):
    """SL y TP se mandan en la rejilla de `tick_size`, no con decimales de más.

    Un SL con un decimal de más llega al bróker y se rechaza con 10016
    `ERR_INVALID_STOPS`, que es un rechazo opaco: el motivo parece de distancia y
    no de formato.
    """
    ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.099_004, tp=1.102_007)
    peticion = mt5.orders_sent[0]
    assert peticion["sl"] == pytest.approx(1.09900, abs=1e-9)
    assert peticion["tp"] == pytest.approx(1.10201, abs=1e-9)
    # Cinco dígitos: es EURUSD, no un índice.
    assert len(str(peticion["sl"]).split(".")[1]) <= 5


def test_simbolo_indice_usa_su_rejilla_de_un_centimo(ejec, mt5):
    """El redondeo es POR SÍMBOLO: DE40 cotiza a 2 decimales.

    Alinear un índice a la rejilla de un par de divisas mandaría 17999.999999.
    """
    ejec.send_market_order("DE40", "BUY", 0.10, sl=17990.004, tp=18010.0)
    assert mt5.orders_sent[0]["sl"] == pytest.approx(17990.00, abs=1e-9)


# ---------------------------------------------------------------------------
# Puertas: lo que NO sale
# ---------------------------------------------------------------------------


def test_accion_invalida_no_toca_el_bróker(ejec, mt5):
    res = ejec.send_market_order("EURUSD", "LONG", 0.10)
    assert res["ok"] is False
    assert res["status"] == "rejected"
    assert mt5.orders_sent == []


def test_simbolo_que_no_publica_el_bróker_no_manda_nada(ejec, mt5):
    res = ejec.send_market_order("INVENTADO", "BUY", 0.10)
    assert res["ok"] is False
    assert res["status"] == "symbol_not_found"
    assert mt5.orders_sent == []


def test_sin_cotizacion_no_hay_orden_a_mercado(ejec, mt5):
    """Sin precio no se manda. Una orden a mercado contra un precio desconocido
    es una orden a precio desconocido."""
    del mt5.tick_by_symbol["EURUSD"]
    res = ejec.send_market_order("EURUSD", "BUY", 0.10)
    assert res["ok"] is False
    assert res["status"] == "sin_precio"
    assert mt5.orders_sent == []


def test_cotizacion_a_cero_no_se_trata_como_precio(ejec, mt5):
    """Un tick a 0 no es un precio: es la señal de que el bróker no ha cotizado.

    Mandar con `price=0` no es una orden al mercado, es una orden que el bróker
    rechaza (10015) o, peor, que acepta como "a mercado" a un precio imposible.
    """
    mt5.tick_by_symbol["EURUSD"] = FakeTick(time=1, bid=0.0, ask=0.0)
    res = ejec.send_market_order("EURUSD", "BUY", 0.10)
    assert res["ok"] is False
    assert res["status"] == "sin_precio"
    assert mt5.orders_sent == []


def test_lote_por_debajo_del_minimo_del_broker_no_manda(ejec, mt5):
    res = ejec.send_market_order("EURUSD", "BUY", 0.004)
    assert res["ok"] is False
    assert res["status"] == "volume_invalido"
    assert mt5.orders_sent == []
    assert "0.01" in res["error"]


def test_lote_por_encima_del_maximo_no_manda(ejec, mt5):
    res = ejec.send_market_order("EURUSD", "BUY", 500.0)
    assert res["ok"] is False
    assert res["status"] == "volume_invalido"
    assert mt5.orders_sent == []


def test_lote_se_ajusta_a_la_rejilla_y_lo_declara(ejec, mt5):
    """Un lote que no cae en el paso se alinea ABAJO y se dice.

    Alinear a 0.02 cuando se pidieron 0.019 sería inventar exposición que nadie
    pidió; alinear a 0.01 es lo que el bróker acepta. El aviso importa porque es
    una decisión que cambia el dinero en juego.
    """
    res = ejec.send_market_order("EURUSD", "BUY", 0.019)
    assert res["ok"] is True
    assert mt5.orders_sent[0]["volume"] == pytest.approx(0.02)
    assert res["volume_adjusted"] is True
    assert "0.02" in res["volume_note"]


def test_lote_no_numerico_no_revisa_ni_manda(ejec, mt5):
    res = ejec.send_market_order("EURUSD", "BUY", "mucho")
    assert res["ok"] is False
    assert mt5.orders_sent == []


# ---------------------------------------------------------------------------
# Relleno: el ciclo FOK -> IOC
# ---------------------------------------------------------------------------


def test_eurusd_manda_fok_porque_es_lo_unico_que_admite(ejec, mt5):
    """`filling_mode=1` en EURUSD. Mandar IOC ahí es un 10030 garantizado."""
    res = ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.099, tp=1.102)
    assert res["filling"] == "FOK"
    assert mt5.orders_sent[0]["type_filling"] == mt5_execution.ORDER_FILLING_FOK


def test_si_rechaza_el_llenado_prueba_el_otro(ejec, mt5):
    """10030 `INVALID_FILL` -> se prueba el modo contrario, y se cuentan los dos.

    Un único retcode en el log borraría el primer intento, y el rechazo parecería
    de un modo cualquiera en lugar de "el símbolo no admite FOK, se probó IOC".
    """
    mt5.order_send_results = [
        FakeTradeResult(retcode=10030, comment="Invalid filling mode"),
        FakeTradeResult(retcode=10009, deal=999, order=1000, price=1.10012),
    ]
    res = ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.099, tp=1.102)
    assert res["ok"] is True
    assert res["filling"] == "IOC"
    assert len(mt5.orders_sent) == 2
    assert mt5.orders_sent[0]["type_filling"] == mt5_execution.ORDER_FILLING_FOK
    assert mt5.orders_sent[1]["type_filling"] == mt5_execution.ORDER_FILLING_IOC
    assert [a["retcode"] for a in res["attempts"]] == [10030, 10009]
    assert res["attempts"][0]["ok"] is False


def test_un_rechazo_distinto_no_prueba_otro_modo(ejec, mt5):
    """Sin money (10019) no se reintenta con otro relleno: no es un problema de
    relleno. Reintentar sería una segunda orden de la misma que también va a fallar.
    """
    mt5.order_send_results = FakeTradeResult(retcode=10019, comment="No money")
    res = ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.099, tp=1.102)
    assert res["ok"] is False
    assert res["retcode"] == 10019
    assert len(mt5.orders_sent) == 1
    assert "Sin dinero" in res["error"]


def test_order_send_que_devuelve_none_no_es_un_rechazo(ejec, mt5):
    """None es ausencia de respuesta, y se dice como tal.

    Tratarlo como un retcode sería inventar un número de error que nadie ha dado.
    Se reintenta el MISMO modo de llenado, no el otro: un None no dice nada sobre
    el relleno, así que rotar a IOC para descubrir que tampoco contesta gastaría
    los dos intentos sin aprender nada.
    """
    mt5.order_send_results = None
    res = ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.099, tp=1.102)
    assert res["ok"] is False
    assert res["retcode"] is None
    assert res["status"] == "rejected"
    assert all(a["retcode"] is None for a in res["attempts"])
    assert "None" in res["error"]
    # Todos los intentos con FOK: EURUSD solo admite FOK, y el motivo del reintento
    # no es de relleno.
    assert {a["filling"] for a in res["attempts"]} == {"FOK"}
    assert len(mt5.orders_sent) == mt5_execution.MAX_PRECIO_REINTENTOS + 1


def test_el_reeintento_por_precio_es_un_presupuesto_y_no_un_bucle(ejec, mt5):
    """Un bróker que siempre devuelve requote no genera órdenes infinitas.

    Son órdenes de mercado: cada reintento es una orden nueva que puede llenarse
    dos veces si la primera llegó al bróker y se perdió la respuesta. El límite no
    es un detalle defensivo, es la exposición máxima que se acepta en ese hueco.
    """
    mt5.order_send_results = FakeTradeResult(retcode=10004, comment="Requote")
    res = ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.099, tp=1.102)
    assert res["ok"] is False
    assert res["retcode"] == 10004
    assert len(mt5.orders_sent) == mt5_execution.MAX_PRECIO_REINTENTOS + 1


def test_requote_reintenta_con_el_precio_nuevo(ejec, mt5):
    """10004 `REQUOTE` significa que el precio se movió: se reintenta con el nuevo.

    Reintentar con el precio caducado es mandar la misma orden obsoleseta otra
    vez, y en un bróker rápido agota el `deviation` sin motivo aparente.
    """
    mt5.order_send_results = [
        FakeTradeResult(retcode=10004, comment="Requote"),
        FakeTradeResult(retcode=10009, deal=1, order=2, price=1.10012),
    ]
    res = ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.099, tp=1.102)
    assert res["ok"] is True
    assert len(mt5.orders_sent) == 2


# ---------------------------------------------------------------------------
# Pérdida por lote
# ---------------------------------------------------------------------------


def test_perdida_por_lote_se_pregunta_al_broker(ejec, mt5):
    """`order_calc_profit` y no una estimación con el tick value.

    La estimación con el tick value falla en cuentas con conversión de divisa y en
    símbolos con tick value no lineal, y su error va del lado peligroso:
    subestimar la pérdida.
    """
    assert ejec.profit_per_lot("EURUSD", 0.10, 1.10012, 1.09900) == pytest.approx(-12.30)
    assert mt5.calc_profit_calls == [("EURUSD", 0.10, 1.10012, 1.09900)]


def test_perdida_por_lote_none_no_se_convierte_en_cero(ejec, mt5):
    """Que el bróker no conteste devuelve None, no 0.

    Un 0 se lee como "esta orden no puede perder dinero", que es el peor error
    posible en una comprobación de riesgo: desactiva la puerta sin decir nada.
    """
    mt5.calc_profit_value = None
    assert ejec.profit_per_lot("EURUSD", 0.10, 1.10012, 1.09900) is None


# ---------------------------------------------------------------------------
# Cierre
# ---------------------------------------------------------------------------


def test_cierra_la_posicion_del_ticket_conservando_su_magic(ejec, mt5):
    """El `magic` de la posición se preserva al cerrarla.

    No es un detalle: el EA cuenta sus operaciones por magic, y un cierre con el
    magic de la web le borra del conteo una operación que sí ocurrió.
    """
    mt5.positions = [FakePosition(ticket=555, symbol="EURUSD", type=0, volume=0.10,
                                  magic=8882026, price_open=1.10000)]
    res = ejec.close_position(ticket=555)
    assert res["ok"] is True
    assert res["status"] == "closed"
    peticion = mt5.orders_sent[0]
    assert peticion["magic"] == 8882026
    assert peticion["position"] == 555
    assert peticion["volume"] == 0.10
    assert peticion["type"] == mt5_execution.ORDER_TYPE_SELL  # cerrar una BUY = vender
    assert peticion["price"] == pytest.approx(EURUSD_TICK.bid)  # se recibe al bid


def test_cerrar_una_sell_se_compra_al_ask(ejec, mt5):
    mt5.positions = [FakePosition(ticket=556, symbol="EURUSD", type=1, volume=0.20,
                                  magic=8882026, price_open=1.10500)]
    ejec.close_position(ticket=556)
    peticion = mt5.orders_sent[0]
    assert peticion["type"] == mt5_execution.ORDER_TYPE_BUY
    assert peticion["price"] == pytest.approx(EURUSD_TICK.ask)
    assert peticion["volume"] == 0.20  # el volumen de la posición, no un 0.1 a dedo


def test_cierre_por_symbol_es_el_ultimo_filtro(ejec, mt5):
    """Sin ticket, se cierra la primera del símbolo y se devuelve su ticket.

    En una cuenta hedging hay varias del mismo símbolo; por eso el puerto expone
    `ticket` en el resultado, para que quien llama pueda decir cuál se cerró y no
    creer que se han cerrado todas.
    """
    mt5.positions = [FakePosition(ticket=1, symbol="EURUSD", volume=0.10, magic=8882026),
                     FakePosition(ticket=2, symbol="EURUSD", volume=0.30, magic=8882026)]
    res = ejec.close_position(symbol="EURUSD")
    assert res["ok"] is True
    assert res["ticket"] == 1


def test_ticket_inexistente_no_inventa_una_orden(ejec, mt5):
    res = ejec.close_position(ticket=999999)
    assert res["ok"] is False
    assert res["status"] == "no_position"
    assert mt5.orders_sent == []


def test_cierre_con_el_emoji_del_ea_no_rompe_nada(ejec, mt5):
    """Posición sin magic: se usa el de la config como respaldo, no se manda 0.

    Un 0 en el magic hace la operación invisible para cualquier conteo por magic.
    """
    mt5.positions = [FakePosition(ticket=7, symbol="EURUSD", magic=0)]
    ejec.close_position(ticket=7, magic=8882026)
    assert mt5.orders_sent[0]["magic"] == 8882026


def test_cierre_rechazado_no_cierra_nada_falso(ejec, mt5):
    mt5.positions = [FakePosition(ticket=8, symbol="EURUSD", magic=8882026)]
    mt5.order_send_results = FakeTradeResult(retcode=10018, comment="Market closed")
    res = ejec.close_position(ticket=8)
    assert res["ok"] is False
    assert res["status"] == "rejected"
    assert res["retcode"] == 10018
    assert "cerrado" in res["error"].lower()


# ---------------------------------------------------------------------------
# Modificación de stops: el trailing del CTA (F5)
# ---------------------------------------------------------------------------


def test_modificar_manda_sltp_con_los_niveles_y_sin_lo_que_no_toca(ejec, mt5):
    """`TRADE_ACTION_SLTP` mueve campos de una posición que YA existe.

    La petición lleva `symbol` y `position`, y nada de volumen, precio ni magic:
    no se abre ni se cierra nada, y la posición sigue siendo la misma. Preservar
    el magic aquí sería un error de bulto: SLTP no lo lleva.
    """
    mt5.positions = [FakePosition(ticket=555, symbol="EURUSD", type=0, volume=0.10,
                                  magic=8882026, price_open=1.10000,
                                  sl=1.09900, tp=1.10200)]
    res = ejec.modify_position(555, sl=1.09950, tp=1.10250)
    assert res["ok"] is True
    assert res["status"] == "modified"
    assert res["magic"] == 8882026
    assert res["sl"] == pytest.approx(1.09950)
    assert res["tp"] == pytest.approx(1.10250)
    peticion = mt5.orders_sent[0]
    assert peticion["action"] == mt5_execution.TRADE_ACTION_SLTP == 3
    assert peticion["symbol"] == "EURUSD"
    assert peticion["position"] == 555
    assert "volume" not in peticion
    assert "price" not in peticion
    assert "magic" not in peticion


def test_modificar_sin_tp_conserva_el_que_tiene(ejec, mt5):
    """El bróker pisa los DOS campos con lo que recibe: `None` se manda como el actual.

    Enviar solo `sl` sin `tp` (o al revés) con el campo ausente le pondría cero
    al nivel que no se quería tocar, que es una forma silenciosa de quitar un
    objetivo que nadie pidió quitar.
    """
    mt5.positions = [FakePosition(ticket=555, symbol="EURUSD", sl=1.09900, tp=1.10200)]
    ejec.modify_position(555, sl=1.09950)
    assert mt5.orders_sent[0]["sl"] == pytest.approx(1.09950)
    assert mt5.orders_sent[0]["tp"] == pytest.approx(1.10200)


def test_modificar_sin_sl_conserva_el_que_tiene(ejec, mt5):
    mt5.positions = [FakePosition(ticket=555, symbol="EURUSD", sl=1.09900, tp=1.10200)]
    ejec.modify_position(555, tp=1.10300)
    assert mt5.orders_sent[0]["sl"] == pytest.approx(1.09900)
    assert mt5.orders_sent[0]["tp"] == pytest.approx(1.10300)


def test_un_cero_explícito_quita_el_nivel(ejec, mt5):
    """0.0 es la forma en que MT5 entiende "sin nivel"; None es "no lo toques"."""
    mt5.positions = [FakePosition(ticket=555, symbol="EURUSD", sl=1.09900, tp=1.10200)]
    ejec.modify_position(555, sl=1.09950, tp=0.0)
    assert mt5.orders_sent[0]["sl"] == pytest.approx(1.09950)
    assert mt5.orders_sent[0]["tp"] == 0.0


def test_el_stop_nuevo_se_alinea_a_la_rejilla(ejec, mt5):
    """Un stop off-grid se rechaza con 10016: se redondea ANTES de mandarlo."""
    mt5.positions = [FakePosition(ticket=555, symbol="EURUSD", sl=1.09900, tp=1.10200)]
    ejec.modify_position(555, sl=1.0987654321)
    assert mt5.orders_sent[0]["sl"] == pytest.approx(1.09877)


def test_modificar_un_ticket_que_no_existe_no_inventa_nada(ejec, mt5):
    res = ejec.modify_position(999999, sl=1.09950)
    assert res["ok"] is False
    assert res["status"] == "no_position"
    assert mt5.orders_sent == []


def test_modificar_un_ticket_invalido_no_toca_el_broker(ejec, mt5):
    res = ejec.modify_position("lo-que-sea", sl=1.09950)
    assert res["ok"] is False
    assert res["status"] == "no_position"
    assert mt5.orders_sent == []


def test_modificar_rechazado_dice_el_motivo_del_broker(ejec, mt5):
    mt5.positions = [FakePosition(ticket=555, symbol="EURUSD", sl=1.09900, tp=1.10200)]
    mt5.order_send_results = FakeTradeResult(retcode=10016, comment="Invalid stops")
    res = ejec.modify_position(555, sl=1.10500)
    assert res["ok"] is False
    assert res["status"] == "rejected"
    assert res["retcode"] == 10016
    assert "Invalid stops" in res["error"]


def test_modificar_sin_respuesta_no_es_un_rechazo(ejec, mt5):
    """`None` = ausencia de respuesta, dicha como tal para que quien llama reintente.

    Y es UNA sola petición, no un bucle: mover un stop no compra ni vende, no hay
    relleno que rotar ni precio que actualizar, reintentar sería duplicar esfuerzo
    sin aprender nada.
    """
    mt5.positions = [FakePosition(ticket=555, symbol="EURUSD", sl=1.09900, tp=1.10200)]
    mt5.order_send_results = None
    res = ejec.modify_position(555, sl=1.09950)
    assert res["ok"] is False
    assert res["status"] == "sin_respuesta"
    assert res["retcode"] is None
    assert "None" in res["error"]
    assert len(mt5.orders_sent) == 1


def test_modificar_con_la_terminal_caida_es_sin_terminal(ejec, mt5):
    mt5.initialize_ok = False
    res = ejec.modify_position(555, sl=1.09950)
    assert res["ok"] is False
    assert res["status"] == "sin_terminal"
    assert mt5.orders_sent == []


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


def test_terminal_caido_no_es_un_500(ejec, mt5):
    mt5.initialize_ok = False
    res = ejec.send_market_order("EURUSD", "BUY", 0.10, sl=1.099, tp=1.102)
    assert res["ok"] is False
    assert res["status"] == "sin_terminal"
    assert mt5.orders_sent == []


def test_positions_normalizadas_y_vacias_si_cae(monkeypatch):
    """`positions()` devuelve filas planas, y vacío (no una excepción) sin terminal."""
    fake = FakeMT5.install(monkeypatch)
    fake.positions = [FakePosition(ticket=1, symbol="EURUSD", type=1, volume=0.5,
                                   magic=42, profit=-3.5)]
    s = Session()
    try:
        p = adapter(s)
        filas = p.positions("EURUSD")
        assert filas == [{"ticket": 1, "symbol": "EURUSD", "type": "SELL",
                          "volume": 0.5, "price_open": 0.0, "price_current": 0.0,
                          "sl": 0.0, "tp": 0.0, "profit": -3.5, "magic": 42, "comment": ""}]
    finally:
        s.shutdown_executor()

    fake.initialize_ok = False
    s2 = Session()
    try:
        assert adapter(s2).positions("EURUSD") == []
    finally:
        s2.shutdown_executor()

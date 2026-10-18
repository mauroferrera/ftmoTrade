"""Tests del adaptador de MT5, con el paquete `MetaTrader5` sustituido por un doble.

El test más importante del fichero es `TestLaSesionEsLaUnicaPuerta`: es el que
impide que se vuelva a introducir el bug de Trinity, donde `patterns_service`,
`cvd_service` y `research/data.py` tenían cada uno su executor, su lock y su
propio ciclo de `initialize()`/`shutdown()`.

Los demás tests comprueban que se llame a la función correcta del bróker con los
argumentos correctos, y que lo que devuelve MT5 llegue normalizado (velas con
`time` en segundos epoch, specs con el pip resuelto).

Alcance M1: solo LECTURA (velas, precio, spec, símbolos, posiciones, cuenta). El
envío de órdenes no existe todavía y por eso no hay tests de ejecución.
"""

from __future__ import annotations

import threading
import time

import pytest

from adapters.base_adapter import (
    SymbolNotFound,
    TerminalUnavailable,
    TIMEFRAMES,
)
from adapters.mt5 import mt5_forex
from adapters.mt5.mt5_forex import MT5ForexAdapter, Session, adapter
from tests.unit.mt5_fake import (
    FakeMT5,
    FakePosition,
    FakeSymbolInfo,
    FakeSymbolInfoTrade,
    FakeTick,
    rates_fixture,
)


@pytest.fixture
def mt5(monkeypatch):
    """Doble de MT5 instalado en `sys.modules`, listo para configurar."""
    fake = FakeMT5.install(monkeypatch)
    fake.symbols_list = [
        FakeSymbolInfo("EURUSD"),
        FakeSymbolInfo("USDJPY"),
        FakeSymbolInfo("OCULTO", visible=False),
    ]
    fake.info_by_symbol["EURUSD"] = FakeSymbolInfo("EURUSD", digits=5, point=0.00001)
    fake.info_by_symbol["USDJPY"] = FakeSymbolInfo("USDJPY", digits=3, point=0.001)
    fake.trade_by_symbol["EURUSD"] = FakeSymbolInfoTrade(
        "EURUSD", trade_tick_size=0.00001, trade_tick_value=1.0
    )
    return fake


@pytest.fixture
def session():
    """Sesión real (con su hilo) que se cierra al terminar."""
    s = Session()
    yield s
    s.shutdown_executor()


@pytest.fixture
def adp(session, mt5):
    """Adaptador con sesión propia y el doble de MT5 ya instalado."""
    return MT5ForexAdapter(session)


# ---------------------------------------------------------------------------
# El bug de las tres puertas
# ---------------------------------------------------------------------------


class TestLaSesionEsLaUnicaPuerta:
    """Lo que Trinity tenía mal, escrito como tests que no pueden volver a pasar."""

    def test_solo_hay_un_hilo_que_habla_con_mt5(self, session):
        """`max_workers=1` no es una decisión de rendimiento.

        El paquete MT5 habla con UN terminal por proceso y no tolera que dos
        hilos lo toquen. Con dos hilos, dos lecturas a la vez se corrompen de
        formas que el bróker no reporta: devuelve `None` sin error y el llamante
        no tiene forma de saber que es mentira.
        """
        assert session._executor._max_workers == 1

    def test_el_shutdown_nunca_ocurre_con_una_llamada_en_vuelo(self, session, mt5):
        """El `shutdown()` de Trinity estaba en un `finally`. Aquí no hay ninguno.

        El doble registra ese caso en `shutdown_while_busy`, y este test afirma
        que nunca ocurre. Para que el test signifique algo, el doble NO levanta
        al detectar concurrencia: registra y sondea el test.
        """
        assert mt5.shutdown_while_busy is False

        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(3))
        a = MT5ForexAdapter(session)
        for _ in range(20):
            a.ohlc("EURUSD", "M15", 3)
        session.close()

        assert mt5.shutdown_count >= 1
        assert mt5.shutdown_while_busy is False

    def test_una_sesion_reabierta_sigue_funcionando(self, mt5, session):
        """`close()` no es irreversible: la siguiente llamada reabre.

        `shutdown_executor()` sí lo es (mata el hilo). La diferencia importa en
        tests, que abren y cierran sesiones, y en un reinicio del terminal.
        """
        a = MT5ForexAdapter(session)
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(3))
        assert len(a.ohlc("EURUSD", "M15", 3)) == 3

        session.close()
        assert session.is_open is False

        assert len(a.ohlc("EURUSD", "M15", 3)) == 3
        assert session.is_open is True
        assert mt5.called("initialize") is True

    def test_close_espera_a_que_no_quede_nada_en_vuelo(self, mt5, session):
        """Cerrar no es solo `mt5.shutdown()`: primero drena."""
        dentro = threading.Event()
        seguir = threading.Event()

        def lento(_mt5_mod):
            dentro.set()
            seguir.wait(timeout=5)
            return 1

        t = threading.Thread(target=lambda: session.call(lento))
        t.start()
        assert dentro.wait(timeout=5)

        cerrado = threading.Event()

        def cerrar():
            session.close()
            cerrado.set()

        tc = threading.Thread(target=cerrar)
        tc.start()
        time.sleep(0.05)
        assert cerrado.is_set() is False, "close() cerró con trabajo en vuelo"

        seguir.set()
        t.join(timeout=5)
        tc.join(timeout=5)
        assert cerrado.is_set() is True
        assert mt5.shutdown_while_busy is False

    def test_close_es_idempotente(self, session, mt5):
        """Cerrar dos veces no debe hacer dos `shutdown()` ni reventar."""
        a = MT5ForexAdapter(session)
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(2))
        a.ohlc("EURUSD", "M15", 2)
        session.close()
        session.close()
        assert mt5.shutdown_count == 1


# ---------------------------------------------------------------------------
# La sesión se abre una vez, no en cada llamada
# ---------------------------------------------------------------------------


class TestAperturaDeSesion:
    def test_no_reconecta_en_cada_llamada(self, adp, mt5):
        """Trinity hacía `initialize()` en CADA llamada y `shutdown()` al salir.

        MT5 tarda ~100ms en conectar. Con 40 lecturas al minuto son 40
        reconexiones, y cada una es una ventana donde el bróker puede responder
        con datos a medio cargar.
        """
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(3))
        for _ in range(10):
            adp.ohlc("EURUSD", "M15", 3)
        assert len(mt5.calls_named("initialize")) == 1
        assert mt5.shutdown_count == 0

    def test_una_terminal_que_no_arraque_da_un_error_usable(self, session, monkeypatch):
        """El error tiene que decir QUÉ hacer, no solo qué falló.

        El mensaje incluye el `last_error` del bróker, que es la diferencia entre
        diagnosticar en diez segundos y en diez minutos.
        """
        FakeMT5.install(monkeypatch, initialize_ok=False, last_error_value=10049)
        a = MT5ForexAdapter(session)
        with pytest.raises(TerminalUnavailable) as exc:
            a.ohlc("EURUSD", "M15", 10)
        texto = str(exc.value)
        assert "terminal" in texto.lower()
        assert "10049" in texto

    def test_una_terminal_que_no_arraque_no_deja_la_sesion_abierta(self, session, monkeypatch):
        """Tras un fallo de `initialize()` no puede quedar estado a medias."""
        FakeMT5.install(monkeypatch, initialize_ok=False)
        a = MT5ForexAdapter(session)
        with pytest.raises(TerminalUnavailable):
            a.ohlc("EURUSD", "M15", 10)
        assert session.is_open is False

    def test_importar_el_adaptador_no_abre_la_terminal(self):
        """`import adapters.mt5.mt5_forex` no debe inicializar MT5.

        El adaptador se importa desde las rutas, los tests y cualquier chequeo de
        imports. Si importar abriera la terminal, un `pytest` entero dependería de
        que haya una sesión abierta.
        """
        assert mt5_forex._SESSION is None or mt5_forex._SESSION.is_open is False

    def test_importar_sin_el_paquete_no_falla(self, monkeypatch):
        """En CI o en un portátil sin terminal, `import` tiene que funcionar."""
        import sys

        monkeypatch.setitem(sys.modules, "MetaTrader5", None)
        assert mt5_forex._import_mt5() is not None

    def test_sin_el_paquete_el_operador_sabe_que_hace_falta(self, monkeypatch):
        """Sin paquete, la excepción debe decir el `pip install` de memoria."""
        import sys

        monkeypatch.setitem(sys.modules, "MetaTrader5", None)
        a = MT5ForexAdapter(Session())
        with pytest.raises(TerminalUnavailable) as exc:
            a.ohlc("EURUSD", "M15", 10)
        assert "pip install" in str(exc.value)


# ---------------------------------------------------------------------------
# Velas
# ---------------------------------------------------------------------------


class TestVelas:
    def test_lee_velas_cuando_el_broker_devuelve_un_array_numpy(self, adp, mt5):
        """El paquete real devuelve un numpy array, y el adaptador lo tiene que leer.

        Este es el test que faltaba y que hacía pasar por verde una lectura de
        velas IMPOSIBLE en producción: `rows or []` y `if not rows` no se pueden
        usar con un numpy array de más de un elemento, lanzan `ValueError: The
        truth value of an array with more than one element is ambiguous`.

        El doble devolvía listas, donde preguntar por la verdad es inocuo. Un
        doble que devuelve el tipo fácil no es un atajo: es un sitio donde el
        error real no se ve.
        """
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(30), as_numpy=True)
        velas = adp.ohlc("EURUSD", "M15", 30)
        assert len(velas) == 30
        assert set(velas[0]) == {"time", "open", "high", "low", "close", "volume"}

    def test_ohlc_con_daily_igualmente_cuando_viene_numpy(self, adp, mt5):
        """`ohlc_with_daily` hacía el mismo `or []` en las DOS lecturas."""
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(30), as_numpy=True)
        mt5.set_rates("EURUSD", TIMEFRAMES["D1"], rates_fixture(3), as_numpy=True)
        intraday, daily = adp.ohlc_with_daily("EURUSD", "M15", 30)
        assert len(intraday) == 30
        assert len(daily) == 3

    def test_un_broker_sin_velas_devuelve_lista_vacia_y_no_none(self, adp, mt5):
        """`None` del broker es "no hay velas", y sigue siendo una lista vacía."""
        assert adp.ohlc("EURUSD", "M15", 30) == []

    def test_pide_las_velas_al_broker_con_los_argumentos_correctos(self, adp, mt5):
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(30))
        adp.ohlc("EURUSD", "M15", 30)
        llamada = mt5.calls_named("copy_rates_from_pos")[0]
        assert llamada[1] == "EURUSD"
        assert llamada[2] == TIMEFRAMES["M15"]
        assert llamada[3] == 0
        assert llamada[4] == 30

    def test_devuelve_la_forma_que_necesita_core(self, adp, mt5):
        """`time/open/high/low/close/volume`, con `time` en segundos epoch."""
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(4))
        velas = adp.ohlc("EURUSD", "M15", 4)
        assert len(velas) == 4
        for vela in velas:
            assert set(vela) == {"time", "open", "high", "low", "close", "volume"}
        assert velas[0]["time"] == 1_700_000_000
        assert velas[0]["open"] == pytest.approx(1.1000)

    def test_las_velas_llegan_en_orden_de_mas_antigua_a_mas_reciente(self, adp, mt5):
        """El orden es lo que hace que cualquier acumulado tenga sentido.

        `copy_rates_from_pos` devuelve de más antigua a más reciente. Si alguien
        "optimiza" invirtiendo la lista para ir de la más nueva a la más antigua
        (parece más natural para un gráfico), los cálculos acumulados suman al
        revés. Este test es la red.
        """
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(6))
        velas = adp.ohlc("EURUSD", "M15", 6)
        tiempos = [v["time"] for v in velas]
        assert tiempos == sorted(tiempos)

    def test_acota_las_velas_al_tope_del_broker(self, adp, mt5):
        """Pedir 5000 no da error: da 1000 y trunca en silencio."""
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(50))
        adp.ohlc("EURUSD", "M15", 99999)
        llamada = mt5.calls_named("copy_rates_from_pos")[0]
        assert llamada[4] == 1000

    def test_no_pide_menos_de_10_velas(self, adp, mt5):
        """El bróker no sirve menos de 10 y devolvería una lista vacía."""
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(50))
        adp.ohlc("EURUSD", "M15", 1)
        assert mt5.calls_named("copy_rates_from_pos")[0][4] == 10

    def test_un_timeframe_invalido_falla_antes_de_tocar_el_broker(self, adp, mt5):
        """Un typo en el timeframe no debe abrir una conexión para nada."""
        with pytest.raises(ValueError, match="no válido"):
            adp.ohlc("EURUSD", "M7", 10)
        assert mt5.calls_named("copy_rates_from_pos") == []

    def test_un_simbolo_vacio_falla_antes_de_tocar_el_broker(self, adp, mt5):
        with pytest.raises(ValueError):
            adp.ohlc("", "M15", 10)
        assert mt5.calls_named("copy_rates_from_pos") == []

    def test_un_simbolo_que_el_broker_no_publica_lo_dice_bien(self, adp, mt5):
        """El mensaje tiene que distinguir "no existe" de "problema técnico"."""
        with pytest.raises(SymbolNotFound) as exc:
            adp.ohlc("NOEXISTE", "M15", 10)
        texto = str(exc.value)
        assert "NOEXISTE" in texto
        assert "Market Watch" in texto

    def test_un_broker_sin_datos_devuelve_una_lista_vacia(self, adp, mt5):
        """Sin velas no es un error: un mercado cerrado es una lista vacía."""
        mt5.rates_by_key[("EURUSD", TIMEFRAMES["M15"])] = None
        assert adp.ohlc("EURUSD", "M15", 10) == []

    def test_la_lectura_diaria_viene_en_el_mismo_viaje(self, adp, mt5):
        """`(intradía, D1)` en una llamada, porque el patrón SMC necesita el PDH/PDL."""
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(4))
        mt5.set_rates("EURUSD", TIMEFRAMES["D1"], rates_fixture(3, step=0.001))
        intraday, diario = adp.ohlc_with_daily("EURUSD", "M15", 4)
        assert len(intraday) == 4
        assert len(diario) == 3
        marcos = {(c[1], c[2]) for c in mt5.calls_named("copy_rates_from_pos")}
        assert marcos == {("EURUSD", TIMEFRAMES["M15"]), ("EURUSD", TIMEFRAMES["D1"])}

    def test_bars_es_alias_de_ohlc(self, adp, mt5):
        """El mismo dato con el nombre que usa el resto del sistema."""
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(3))
        assert adp.bars("EURUSD", "M15", 3) == adp.ohlc("EURUSD", "M15", 3)


# ---------------------------------------------------------------------------
# Precio y símbolos
# ---------------------------------------------------------------------------


class TestPrecioYSimbolos:
    def test_el_precio_trae_bid_ask_y_last(self, adp, mt5):
        mt5.tick_by_symbol["EURUSD"] = FakeTick(1_700_000_000, 1.1000, 1.1001, 1.10005)
        precio = adp.price("EURUSD")
        assert precio["bid"] == pytest.approx(1.1000)
        assert precio["ask"] == pytest.approx(1.1001)
        assert precio["last"] == pytest.approx(1.10005)
        assert precio["time"] == 1_700_000_000

    def test_un_precio_que_el_broker_no_publica_es_none(self, adp):
        """None, no un 0.0.

        Un precio 0 se lee como "el mercado está en cero", y un cálculo de
        distancia a partir de ahí da 0 SL, que es peor que no tener ninguno.
        """
        assert adp.price("NOEXISTE") is None

    def test_solo_ofrece_los_simbolos_visibles(self, adp, mt5):
        """Para un símbolo con `visible=False`, `symbol_info` devuelve None.

        Ofrecerlo sería ofrecer algo que no funciona: el usuario lo elige y
        recibe "símbolo no encontrado" sin entender por qué estaba en la lista.
        """
        simbolos = adp.symbols()
        assert "EURUSD" in simbolos
        assert "OCULTO" not in simbolos
        assert simbolos == sorted(simbolos)

    def test_sin_simbolos_devuelve_lista_vacia(self, adp, mt5):
        mt5.symbols_list = []
        assert adp.symbols() == []

    def test_pide_los_simbolos_con_el_grupo_que_entiende_la_api(self, adp, mt5):
        """El grupo tiene que ser `"*"`, no una máscara de MQL5.

        `symbols_get(group="\\*\\*\\*")` devuelve una lista vacía sin error en el
        bróker real: los escapes con barra invertida son sintaxis de MQL5 y la API
        de Python no los entiende.
        """
        adp.symbols()

        pedidos = [c for c in mt5.calls if c[0] == "symbols_get"]
        assert pedidos, "no se pidió la lista de símbolos"
        for llamada in pedidos:
            assert llamada[1] in (None, "*"), llamada

    def test_lista_vacia_del_broker_no_es_una_lista_vacia_por_error_de_grupo(self, adp, mt5):
        """Con un bróker que publica símbolos, la lista NO puede salir vacía.

        Es la aserción que hace visible el fallo: si el grupo enviado no coincide
        con nada, esto falla en vez de pasar en silencio.
        """
        mt5.symbols_list = [
            FakeSymbolInfo("EURUSD"),
            FakeSymbolInfo("GBPUSD"),
            FakeSymbolInfo("XAUUSD"),
        ]
        assert adp.symbols() == ["EURUSD", "GBPUSD", "XAUUSD"]


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


class TestSpecs:
    def test_arma_el_spec_desde_los_dos_lugares_del_paquete(self, adp, mt5):
        """`SYMBOL_TRADE_*` vive en `symbol_info_trade` en los paquetes nuevos.

        Y `digits`/`point` en `symbol_info` en todos. Un spec montado solo de
        `symbol_info` da `tick_value=0` y, con eso, un cálculo de riesgo que
        parece cero. Por eso se leen los dos y se rellena cada campo del sitio que
        lo tiene.
        """
        spec = adp.spec("EURUSD")
        assert spec.symbol == "EURUSD"
        assert spec.digits == 5
        assert spec.point == pytest.approx(0.00001)
        assert spec.tick_value == pytest.approx(1.0)
        assert spec.tick_size == pytest.approx(0.00001)

    def test_calcula_el_pip_correcto_por_simbolo(self, adp, mt5):
        """El caso del yen y el caso del euro salen distintos."""
        assert adp.spec("EURUSD").pip == pytest.approx(0.0001)
        assert adp.spec("USDJPY").pip == pytest.approx(0.01)

    def test_el_oro_no_usa_la_regla_de_pips(self, adp, mt5):
        """XAUUSD cotiza a 2 decimales: su unidad es el punto, no el pip.

        Aplicarle `point * 10` (la regla de forex) haría un pip de 0.1 y
        multiplicaría un SL de 12 unidades de oro por diez. El `unit_label`
        tiene que decir "puntos", no "pips".
        """
        mt5.info_by_symbol["XAUUSD"] = FakeSymbolInfo("XAUUSD", digits=2, point=0.01)
        spec = adp.spec("XAUUSD")
        assert spec.pip == pytest.approx(0.01)
        assert spec.pip_quote is False
        assert spec.unit_label == "puntos"

    def test_un_simbolo_inexistente_da_none_y_no_un_spec_inventado(self, adp):
        """Esta es la garantía que importa más que ninguna otra del fichero.

        Un spec inventado (los valores por defecto de otro símbolo) produce un
        cálculo de riesgo plausible y equivocado. El mejor resultado posible
        cuando no se sabe la spec es no saberla.
        """
        assert adp.spec("NOEXISTE") is None

    def test_un_paquete_viejo_sin_symbol_info_trade_aun_da_spec(self, adp, mt5):
        """En un paquete antiguo la función no existe, y eso no es un error."""
        mt5.symbol_info_trade = None
        mt5.trade_by_symbol.clear()
        spec = adp.spec("EURUSD")
        assert spec.digits == 5
        assert spec.tick_value == 0.0, "sin el paquete nuevo, el valor es 0 = no lo sé"
        assert spec.tick_value_for_volume(1.0) is None

    def test_un_spec_sin_contrato_no_permite_calcular_riesgo(self, adp, mt5):
        mt5.trade_by_symbol.clear()
        spec = adp.spec("EURUSD")
        assert spec.tick_value_for_volume(1.0) is None, (
            "sin tick_value no se puede calcular riesgo, y 0.0 parecería "
            "que no hay riesgo"
        )


# ---------------------------------------------------------------------------
# Cuenta y posiciones
# ---------------------------------------------------------------------------


class TestCuentaYPosiciones:
    def test_las_posiciones_llegan_normalizadas(self, adp, mt5):
        mt5.positions = [
            FakePosition(11, "EURUSD", type=0, volume=0.5, price_open=1.10,
                         sl=1.09, tp=1.12, profit=25.0, magic=8882026),
            FakePosition(12, "XAUUSD", type=1, volume=0.1, price_open=2000.0,
                         profit=-3.0),
        ]
        filas = adp.positions()
        assert len(filas) == 2
        assert filas[0]["ticket"] == 11
        assert filas[0]["symbol"] == "EURUSD"
        assert filas[0]["type"] == "BUY"
        assert filas[0]["volume"] == pytest.approx(0.5)
        assert filas[0]["magic"] == 8882026
        assert filas[1]["type"] == "SELL"

    def test_filtra_por_simbolo(self, adp, mt5):
        mt5.positions = [
            FakePosition(11, "EURUSD", type=0),
            FakePosition(12, "XAUUSD", type=1),
        ]
        filas = adp.positions("EURUSD")
        assert [f["symbol"] for f in filas] == ["EURUSD"]

    def test_sin_posiciones_devuelve_lista_vacia(self, adp, mt5):
        """`[]` es una respuesta legítima: no tener ninguna no es un error."""
        assert adp.positions() == []

    def test_account_info_normaliza_los_campos_de_la_cuenta(self, adp, mt5):
        mt5.account = FakeMT5().account
        mt5.account.balance = 25000.0
        mt5.account.equity = 24800.0
        mt5.account.profit = -200.0
        info = adp.account_info()
        assert info["balance"] == pytest.approx(25000.0)
        assert info["equity"] == pytest.approx(24800.0)
        assert info["profit"] == pytest.approx(-200.0)
        assert info["currency"] == "USD"

    def test_sin_terminal_account_info_lanza(self, adp, monkeypatch):
        """`account_info` None del bróker es "no hay terminal", no un AttributeError."""
        mt5 = FakeMT5.install(monkeypatch, initialize_ok=False)
        with pytest.raises(TerminalUnavailable):
            MT5ForexAdapter(Session()).account_info()


# ---------------------------------------------------------------------------
# Inyección de sesión
# ---------------------------------------------------------------------------


class TestInyeccionDeSesion:
    def test_la_fabrica_acepta_una_sesion(self, session):
        assert adapter(session).session is session

    def test_set_session_devuelve_la_anterior(self):
        primera = Session()
        segunda = Session()
        try:
            original = mt5_forex.get_session()
            try:
                anterior = mt5_forex.set_session(primera)
                assert anterior is original
                assert mt5_forex.get_session() is primera

                anterior = mt5_forex.set_session(segunda)
                assert anterior is primera
                assert mt5_forex.get_session() is segunda
            finally:
                mt5_forex.set_session(original)
                if original is not primera and original is not segunda:
                    original.shutdown_executor()
        finally:
            primera.shutdown_executor()
            segunda.shutdown_executor()

    def test_get_session_es_la_misma_para_todos(self):
        """Dos adaptadores sin sesión propia comparten la del proceso.

        Es el requisito del paquete MT5: UN terminal por proceso. Si cada
        adaptador fabricara su sesión, estaríamos de vuelta en las tres puertas
        de Trinity.
        """
        a = MT5ForexAdapter()
        b = MT5ForexAdapter()
        assert a.session is b.session

    def test_el_adaptador_tiene_nombre(self, adp):
        assert adp.name == "mt5"

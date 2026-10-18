"""Tests de `MarketService`: la fachada que las rutas de mercado consumen.

El servicio se prueba con un adaptador doble, sin terminal. Lo que se fija aquí
es el COMPORTAMIENTO que no es del bróker: la caché de velas (y su caducidad), el
acotado de `bars`, la normalización del símbolo, y que el spread se calcule en
puntos y no en pips. Si el adaptador real cambia, son los tests del adaptador los
que fallan; estos fijan la orquestación.
"""

from __future__ import annotations

import pytest

from adapters.base_adapter import SymbolSpec
from api.services.market import (
    CACHE_TTL_S,
    DEFAULT_BARS,
    MAX_BARS,
    MarketService,
    simbolo,
)


class FakeSession:
    """Sesión mínima: solo lo que `estado()` lee (el flag `is_open`)."""

    def __init__(self, is_open: bool = False) -> None:
        self.is_open = is_open


class FakeAdapter:
    """Adaptador doble: devuelve lo programado y CUENTA las llamadas.

    El contador de `ohlc_calls` es lo que hace verificable la caché: sin él, un
    test de caché solo puede medir el tiempo, que es frágil.
    """

    name = "fake"

    def __init__(self, session=None) -> None:
        self.session = session or FakeSession()
        self.ohlc_calls = 0
        self.pattern_calls = 0
        self._rates = {}
        self._prices = {}
        self._specs = {}

    def programar_velas(self, symbol, timeframe, rows):
        self._rates[(simbolo(symbol), timeframe.upper())] = rows

    def programar_precio(self, symbol, bid, ask, last=0.0, time=0):
        self._prices[simbolo(symbol)] = {
            "bid": bid, "ask": ask, "last": last, "time": time,
        }

    def programar_spec(self, symbol, spec):
        self._specs[simbolo(symbol)] = spec

    def symbols(self):
        return ["EURUSD", "XAUUSD"]

    def ohlc(self, symbol, timeframe="M15", bars=300):
        self.ohlc_calls += 1
        return list(self._rates.get((simbolo(symbol), timeframe.upper()), []))

    def ohlc_with_daily(self, symbol, timeframe="M15", bars=300):
        self.pattern_calls += 1
        s = simbolo(symbol)
        intraday = list(self._rates.get((s, timeframe.upper()), []))
        daily = list(self._rates.get((s, "D1"), []))
        return intraday, daily

    def price(self, symbol):
        return self._prices.get(simbolo(symbol))

    def spec(self, symbol):
        return self._specs.get(simbolo(symbol))

    def positions(self, symbol=None):
        return [{"ticket": 1, "symbol": symbol or "EURUSD"}]

    def account_info(self):
        return {"balance": 25000.0, "equity": 25000.0}


def _vela(t: int, c: float = 1.1):
    return {"time": t, "open": c, "high": c, "low": c, "close": c, "volume": 1}


class Reloj:
    """Reloj inyectable: la caché se prueba moviendo el tiempo, no durmiendo."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def avanza(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def reloj():
    return Reloj()


@pytest.fixture
def svc(reloj):
    return MarketService(adapter=FakeAdapter(), now=reloj)


# ---------------------------------------------------------------------------
# Normalización del símbolo
# ---------------------------------------------------------------------------


class TestSimbolo:
    def test_mayusculas_y_sin_espacios(self):
        assert simbolo(" eurusd ") == "EURUSD"

    def test_vacio_o_none_cae_al_default(self):
        assert simbolo(None) == "EURUSD"
        assert simbolo("") == "EURUSD"

    def test_el_servicio_normaliza_el_simbolo_que_envia(self, svc):
        """` eurusd ` tiene que llegar al adaptador como `EURUSD`."""
        svc.candles(" eurusd ", "m15", 20)
        assert svc.adapter.ohlc_calls == 1


# ---------------------------------------------------------------------------
# Caché de velas
# ---------------------------------------------------------------------------


class TestCacheDeVelas:
    def test_la_segunda_llamada_dentro_del_ttl_no_va_al_adaptador(self, svc, reloj):
        svc.adapter.programar_velas("EURUSD", "M15", [_vela(1), _vela(2)])
        svc.candles("EURUSD", "M15", 20)
        reloj.avanza(CACHE_TTL_S - 1)
        svc.candles("EURUSD", "M15", 20)
        assert svc.adapter.ohlc_calls == 1, "no cacheó: pidió las velas dos veces"

    def test_al_caducar_el_ttl_vuelve_a_pedirlas(self, svc, reloj):
        svc.adapter.programar_velas("EURUSD", "M15", [_vela(1)])
        svc.candles("EURUSD", "M15", 20)
        reloj.avanza(CACHE_TTL_S + 1)
        svc.candles("EURUSD", "M15", 20)
        assert svc.adapter.ohlc_calls == 2

    def test_claves_distintas_no_se_pisan(self, svc):
        svc.adapter.programar_velas("EURUSD", "M15", [_vela(1)])
        svc.adapter.programar_velas("EURUSD", "H1", [_vela(2)])
        svc.candles("EURUSD", "M15", 20)
        svc.candles("EURUSD", "H1", 20)
        assert svc.adapter.ohlc_calls == 2

    def test_clear_cache_vacia_todo(self, svc):
        svc.adapter.programar_velas("EURUSD", "M15", [_vela(1)])
        svc.candles("EURUSD", "M15", 20)
        svc.clear_cache()
        svc.candles("EURUSD", "M15", 20)
        assert svc.adapter.ohlc_calls == 2

    def test_clear_cache_por_simbolo_deja_el_resto(self, svc):
        svc.adapter.programar_velas("EURUSD", "M15", [_vela(1)])
        svc.adapter.programar_velas("XAUUSD", "M15", [_vela(2)])
        svc.candles("EURUSD", "M15", 20)
        svc.candles("XAUUSD", "M15", 20)
        svc.clear_cache("EURUSD")
        svc.candles("EURUSD", "M15", 20)  # recomputa
        svc.candles("XAUUSD", "M15", 20)  # sigue cacheada
        assert svc.adapter.ohlc_calls == 3


# ---------------------------------------------------------------------------
# pattern_data: ensamblado de velas + PDH/PDL + SMC + niveles
# ---------------------------------------------------------------------------


def _diaria(t: int, high: float, low: float):
    return {"time": t, "open": low, "high": high, "low": low, "close": high, "volume": 1}


class TestPatternData:
    def test_sin_velas_devuelve_none(self, svc):
        assert svc.pattern_data("EURUSD", "M15", 20) is None

    def test_pdh_pdl_excluyen_la_diaria_de_hoy(self, svc):
        """El PDH/PDL sale de las diarias PASADAS; la de hoy (última) se descarta."""
        svc.adapter.programar_velas("EURUSD", "M15", [_vela(1, 1.10), _vela(2, 1.11)])
        svc.adapter.programar_velas(
            "EURUSD", "D1",
            [_diaria(1, 1.20, 1.05), _diaria(2, 1.30, 1.10), _diaria(3, 9.99, 0.01)],
        )
        datos = svc.pattern_data("EURUSD", "M15", 20)
        assert datos["pdh"] == pytest.approx(1.30)
        assert datos["pdl"] == pytest.approx(1.05)

    def test_incluye_zonas_y_niveles(self, svc):
        svc.adapter.programar_velas("EURUSD", "M15", [_vela(t, 1.10 + t * 0.0001) for t in range(1, 12)])
        svc.adapter.programar_velas("EURUSD", "D1", [_diaria(1, 1.20, 1.05), _diaria(2, 1.25, 1.08)])
        datos = svc.pattern_data("EURUSD", "M15", 20)
        assert set(datos["analysis"]["patterns"]) == {"fvgs", "order_blocks", "sweeps"}
        kinds = {lv["kind"] for lv in datos["analysis"]["levels"]}
        assert "pd" in kinds

    def test_la_segunda_llamada_dentro_del_ttl_no_va_al_adaptador(self, svc, reloj):
        svc.adapter.programar_velas("EURUSD", "M15", [_vela(1)])
        svc.pattern_data("EURUSD", "M15", 20)
        reloj.avanza(CACHE_TTL_S - 1)
        svc.pattern_data("EURUSD", "M15", 20)
        assert svc.adapter.pattern_calls == 1, "no cacheó el análisis"

    def test_clear_cache_vacia_tambien_el_analisis(self, svc):
        svc.adapter.programar_velas("EURUSD", "M15", [_vela(1)])
        svc.pattern_data("EURUSD", "M15", 20)
        svc.clear_cache()
        svc.pattern_data("EURUSD", "M15", 20)
        assert svc.adapter.pattern_calls == 2


# ---------------------------------------------------------------------------
# Acotado de bars
# ---------------------------------------------------------------------------

class TestAcotadoDeBars:
    def test_acota_al_maximo_del_broker(self, svc):
        pedidas = {}

        class Captura(FakeAdapter):
            def ohlc(self, symbol, timeframe="M15", bars=300):
                pedidas["bars"] = bars
                return []

        s = MarketService(adapter=Captura())
        s.candles("EURUSD", "M15", 99999)
        assert pedidas["bars"] == MAX_BARS

    def test_minimo_de_diez_velas(self, svc):
        pedidas = {}

        class Captura(FakeAdapter):
            def ohlc(self, symbol, timeframe="M15", bars=300):
                pedidas["bars"] = bars
                return []

        s = MarketService(adapter=Captura())
        s.candles("EURUSD", "M15", 1)
        assert pedidas["bars"] == 10


# ---------------------------------------------------------------------------
# Precio, spec y spread
# ---------------------------------------------------------------------------


class TestPrecioSpecSpread:
    def test_el_precio_lleva_el_simbolo_normalizado(self, svc):
        svc.adapter.programar_precio("EURUSD", 1.1000, 1.1001, time=5)
        datos = svc.price("eurusd")
        assert datos["symbol"] == "EURUSD"
        assert datos["bid"] == pytest.approx(1.1000)

    def test_un_simbolo_sin_precio_es_none(self, svc):
        assert svc.price("NOEXISTE") is None

    def test_el_spread_se_mide_en_puntos(self, svc):
        """(ask - bid) / point, redondeado. En EURUSD, point=0.00001 -> 10 puntos."""
        svc.adapter.programar_precio("EURUSD", 1.10000, 1.10010)
        svc.adapter.programar_spec(
            "EURUSD", SymbolSpec("EURUSD", digits=5, point=0.00001)
        )
        assert svc.spread_points("EURUSD") == pytest.approx(10.0)

    def test_sin_precio_no_hay_spread(self, svc):
        svc.adapter.programar_spec(
            "EURUSD", SymbolSpec("EURUSD", digits=5, point=0.00001)
        )
        assert svc.spread_points("EURUSD") is None

    def test_sin_spec_no_hay_spread(self, svc):
        svc.adapter.programar_precio("EURUSD", 1.10000, 1.10010)
        assert svc.spread_points("EURUSD") is None

    def test_el_spec_pasa_tal_cual(self, svc):
        esp = SymbolSpec("XAUUSD", digits=2, point=0.01)
        svc.adapter.programar_spec("XAUUSD", esp)
        assert svc.spec("xauusd") is esp


# ---------------------------------------------------------------------------
# Estado, cuenta y posiciones
# ---------------------------------------------------------------------------


class TestEstadoCuentaPosiciones:
    def test_el_estado_refleja_el_flag_de_la_sesion_sin_tocar_el_terminal(self):
        svc = MarketService(adapter=FakeAdapter(session=FakeSession(is_open=True)))
        assert svc.estado() == {"adaptador": "fake", "terminal": "abierto"}

    def test_cerrado_cuando_la_sesion_no_esta_abierta(self, svc):
        assert svc.estado()["terminal"] == "cerrado"

    def test_la_cuenta_pasa_tal_cual(self, svc):
        assert svc.account_info()["balance"] == pytest.approx(25000.0)

    def test_las_posiciones_pasan_tal_cual(self, svc):
        assert svc.positions()[0]["ticket"] == 1

    def test_los_simbolos_pasan_tal_cual(self, svc):
        assert svc.symbols() == ["EURUSD", "XAUUSD"]


def test_el_default_de_bars_es_el_esperado():
    """Un cambio accidental del default mueve todas las lecturas de golpe."""
    assert DEFAULT_BARS == 300

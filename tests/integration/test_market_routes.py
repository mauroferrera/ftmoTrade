"""Tests de las rutas `/api/market` contra la app real, con dobles inyectados.

No se abre MT5 ni la base real: se construye un `Runtime` con un `market` doble y
se pasa a `create_app(runtime=...)`. Así el test recorre el camino completo
—router -> dependencia -> servicio -> adaptador doble— sin terminal, que es el
punto de tener el servicio inyectable.

Lo que se fija aquí es la CONTRATACIÓN HTTP: qué código sale cuando el bróker no
publica un símbolo (404, no 503), cómo se ve el cuerpo de cada endpoint, y que el
`spread_points` aparezca en el precio.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from adapters.base_adapter import (
    SymbolNotFound,
    SymbolSpec,
    TerminalUnavailable,
    resolve_timeframe,
)
from api.app import create_app
from api.runtime import Runtime
from api.services.market import MarketService


class FakeSession:
    def __init__(self, is_open=True):
        self.is_open = is_open


class FakeAdapter:
    """Adaptador doble que también puede simular 'el bróker no lo publica'."""

    name = "fake"

    def __init__(self, session=None, *, con_precio=True, con_spec=True):
        self.session = session or FakeSession()
        self._con_precio = con_precio
        self._con_spec = con_spec

    def symbols(self):
        return ["EURUSD", "XAUUSD"]

    def ohlc(self, symbol, timeframe="M15", bars=300):
        resolve_timeframe(timeframe)  # contrato del adaptador real: M7 -> ValueError
        if symbol.upper() == "NOEXISTE":
            raise SymbolNotFound(f"El bróker no publica {symbol} en Market Watch.")
        return [
            {"time": 1_700_000_000 + i * 900, "open": 1.1, "high": 1.11,
             "low": 1.09, "close": 1.105, "volume": 100 + i}
            for i in range(min(bars, 3))
        ]

    def ohlc_with_daily(self, symbol, timeframe="M15", bars=300):
        resolve_timeframe(timeframe)
        if symbol.upper() == "NOEXISTE":
            raise SymbolNotFound(f"El bróker no publica {symbol} en Market Watch.")
        intraday = [
            {"time": 1_700_000_000 + i * 900, "open": 1.1, "high": 1.11,
             "low": 1.09, "close": 1.105, "volume": 100 + i}
            for i in range(min(bars, 3))
        ]
        # Tres diarias: las dos primeras son "pasadas" (definen PDH/PDL) y la
        # última es "hoy", con un rango absurdo que debe EXCLUIRSE del PDH/PDL.
        daily = [
            {"time": 1, "open": 1.10, "high": 1.20, "low": 1.05, "close": 1.15, "volume": 1},
            {"time": 2, "open": 1.15, "high": 1.30, "low": 1.10, "close": 1.20, "volume": 1},
            {"time": 3, "open": 1.20, "high": 9.99, "low": 0.01, "close": 1.20, "volume": 1},
        ]
        return intraday, daily

    def price(self, symbol):
        if not self._con_precio or symbol.upper() == "NOEXISTE":
            return None
        return {"bid": 1.10000, "ask": 1.10010, "last": 1.10005, "time": 1_700_000_000}

    def spec(self, symbol):
        if not self._con_spec or symbol.upper() == "NOEXISTE":
            return None
        return SymbolSpec(symbol.upper(), digits=5, point=0.00001, tick_value=1.0)

    def positions(self, symbol=None):
        return [{"ticket": 7, "symbol": symbol or "EURUSD", "type": "BUY"}]

    def account_info(self):
        return {"balance": 25000.0, "equity": 25000.0, "currency": "USD"}


class FakeStore:
    """Store doble: solo lo que `/api/health` y `/api/config/summary` leen."""

    def list_db_tables(self):
        return [{"name": "t%d" % i} for i in range(12)]

    def get_trading_config(self):
        return {"symbols": ["EURUSD", "XAUUSD"], "execution": {"mode": "semi"}}


def _client(market=None):
    rt = Runtime(store=FakeStore(), market=market or MarketService(adapter=FakeAdapter()))
    return TestClient(create_app(runtime=rt))


@pytest.fixture
def client():
    with _client() as c:
        yield c


class TestHealthConMercado:
    def test_health_reporta_el_mercado(self, client):
        datos = client.get("/api/health").json()
        assert datos["market"]["estado"] == "ok"
        assert datos["market"]["terminal"] == "abierto"

    def test_health_sin_mercado_cableado(self):
        rt = Runtime(store=FakeStore(), market=None)
        with TestClient(create_app(runtime=rt)) as c:
            datos = c.get("/api/health").json()
        assert datos["market"]["estado"] == "sin_puerto"


class TestSymbols:
    def test_devuelve_la_lista(self, client):
        datos = client.get("/api/market/symbols").json()
        assert datos["symbols"] == ["EURUSD", "XAUUSD"]


class TestStatus:
    def test_el_estado_no_toca_el_terminal(self, client):
        datos = client.get("/api/market/status").json()
        assert datos == {"adaptador": "fake", "terminal": "abierto"}


class TestCandles:
    def test_devuelve_velas_normalizadas(self, client):
        datos = client.get("/api/market/candles", params={"symbol": "EURUSD", "bars": 10}).json()
        assert datos["symbol"] == "EURUSD"
        assert datos["timeframe"] == "M15"
        assert datos["bars"] == 3
        assert set(datos["candles"][0]) == {"time", "open", "high", "low", "close", "volume"}

    def test_un_simbolo_inexistente_da_404(self, client):
        r = client.get("/api/market/candles", params={"symbol": "NOEXISTE"})
        assert r.status_code == 404

    def test_bars_fuera_de_rango_da_422(self, client):
        """El acotado de la query lo hace FastAPI (`ge=10, le=1000`)."""
        assert client.get("/api/market/candles", params={"bars": 5}).status_code == 422
        assert client.get("/api/market/candles", params={"bars": 5000}).status_code == 422

    def test_un_timeframe_malo_da_400(self, client):
        """`resolve_timeframe` lanza ValueError, y su handler lo traduce a 400."""
        r = client.get("/api/market/candles", params={"symbol": "EURUSD", "timeframe": "M7"})
        assert r.status_code == 400


class TestAnalysis:
    def test_devuelve_zonas_y_niveles(self, client):
        datos = client.get(
            "/api/market/analysis", params={"symbol": "EURUSD", "bars": 10}
        ).json()
        assert datos["symbol"] == "EURUSD"
        assert datos["timeframe"] == "M15"
        # PDH/PDL de las diarias pasadas; la de hoy (rango absurdo) se descarta.
        assert datos["pdh"] == pytest.approx(1.30)
        assert datos["pdl"] == pytest.approx(1.05)
        assert set(datos["analysis"]["patterns"]) == {"fvgs", "order_blocks", "sweeps"}
        kinds = {lv["kind"] for lv in datos["analysis"]["levels"]}
        assert "pd" in kinds

    def test_un_simbolo_inexistente_da_404(self, client):
        r = client.get("/api/market/analysis", params={"symbol": "NOEXISTE"})
        assert r.status_code == 404

    def test_un_timeframe_malo_da_400(self, client):
        r = client.get(
            "/api/market/analysis", params={"symbol": "EURUSD", "timeframe": "M7"}
        )
        assert r.status_code == 400


class TestPrice:
    def test_devuelve_bid_ask_y_spread(self, client):
        datos = client.get("/api/market/price", params={"symbol": "EURUSD"}).json()
        assert datos["bid"] == pytest.approx(1.10000)
        assert datos["ask"] == pytest.approx(1.10010)
        assert datos["spread_points"] == pytest.approx(10.0)

    def test_sin_cotizacion_da_404(self, client):
        r = client.get("/api/market/price", params={"symbol": "NOEXISTE"})
        assert r.status_code == 404


class TestSpec:
    def test_devuelve_los_campos_del_spec(self, client):
        datos = client.get("/api/market/spec", params={"symbol": "EURUSD"}).json()
        assert datos["digits"] == 5
        assert datos["pip"] == pytest.approx(0.0001)
        assert datos["unit_label"] == "pips"

    def test_sin_spec_da_404(self):
        with _client(MarketService(adapter=FakeAdapter(con_spec=False))) as c:
            r = c.get("/api/market/spec", params={"symbol": "EURUSD"})
        assert r.status_code == 404


class TestCuentaYPosiciones:
    def test_la_cuenta_devuelve_el_balance(self, client):
        datos = client.get("/api/market/account").json()
        assert datos["balance"] == pytest.approx(25000.0)

    def test_las_posiciones_llegan_con_count(self, client):
        datos = client.get("/api/market/positions").json()
        assert datos["count"] == 1
        assert datos["positions"][0]["ticket"] == 7


class TestErroresDelBroker:
    def test_una_terminal_caida_da_503(self):
        class AdapterSinTerminal(FakeAdapter):
            def symbols(self):
                raise TerminalUnavailable("No hay conexión con MetaTrader 5.")

        svc = MarketService(adapter=AdapterSinTerminal())
        with _client(svc) as c:
            r = c.get("/api/market/symbols")
        assert r.status_code == 503
        assert "conexión" in r.json()["error"]

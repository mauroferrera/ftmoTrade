"""Tests de las rutas de ejecución contra la app real, con dobles inyectados.

Sin MT5 y sin base real: un `Runtime` con mercado, store, prop y puerto dobles,
pasado a `create_app(runtime=...)`. Se fija la CONTRATACIÓN de la capa HTTP:

  - **Las tres escrituras piden token** (`POST /api/trade/market`,
    `POST /api/positions/{ticket}/close`, `POST /api/positions/{ticket}/modify`).
    Abrir, cerrar o mover un stop mueven dinero; una ruta sin token convierte
    cualquier página web en cliente de la API con la sesión del usuario.
  - **La lectura NO pide token** (`GET /api/positions`): mirar posiciones no
    cambia nada, y exigir token ahí solo impediría pintar el panel.
  - **El status lo decide el servicio y la ruta solo lo traduce.** Por eso lo que
    se comprueba aquí no es la lógica de las puertas (ya probada en
    `test_execution_service.py`) sino que el código HTTP y el cuerpo JSON que
    salen por el cable son los del servicio, con `detail` íntegro.
  - Un `error` de `positions` (sin puerto cableado, o un puerto que se calló) es
    503; una lista vacía es un 200: "no hay posiciones" es una respuesta.

Los dobles son mínimos a propósito: cada uno implementa SOLO lo que la ruta toca
por debajo, para que un fallo del cableado no quede tapado por un doble que hace
demasiado.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from api.app import create_app
from api.runtime import Runtime

CAPITAL_TEST = 25000.0

#: Entrada válida: BUY a 1.10012 (el ask) con SL a 10 pips y TP al doble.
ENTRADA_BUY = 1.10012
SL_BUY = 1.09912
TP_BUY = 1.10212
INVALIDACION_BUY = 1.09850

#: Configuración mínima que deja pasar un setup decente.
CFG_MINIMA: Dict[str, Any] = {
    "symbols_allow": ["EURUSD"],
    "min_rr": 2.0,
    "sl_distance": 0.00100,
    "magic": 8882026,
    "comment": "FTMO Copilot",
}

#: El cuerpo que el panel manda para una orden a mercado que debe salir.
CUERPO_OK: Dict[str, Any] = {
    "symbol": "EURUSD",
    "action": "BUY",
    "volume": 0.10,
    "sl_distance": 0.00100,
    "tp_distance": 0.00200,
    "score": 72.0,
    "verdict": "APROBADO",
    "invalidate_level": INVALIDACION_BUY,
    "planned_entry": ENTRADA_BUY,
}


class SpecFalso:
    """El spec del bróker: campos que leen el dimensionado y la calidad."""

    symbol = "EURUSD"
    point = 0.00001
    digits = 5
    contract_size = 100000.0
    tick_size = 0.00001
    tick_value = 1.0
    volume_min = 0.01
    volume_max = 100.0
    volume_step = 0.01

    def __getattr__(self, nombre: str) -> Any:
        return 0.0


class MarketFalso:
    """`spec` y `price`. El `price` es el del doble del unit test, para que las
    puertas de calidad de ejecución vean el mismo spread de 12 puntos."""

    def __init__(self, spec: Any = None, precio: Any = "ok") -> None:
        self._spec = spec if spec is not None else SpecFalso()
        self._precio = precio

    def spec(self, symbol: str) -> Any:
        return self._spec

    def price(self, symbol: str) -> Any:
        if self._precio == "ok":
            return {"symbol": symbol, "bid": 1.10000, "ask": 1.10012,
                    "time": 1_700_000_000}
        return self._precio


class PropFalso:
    """El gate FTMO: las tres claves que `_estado_ftmo` mira."""

    def __init__(self, tracked: bool = True, blocked: bool = False,
                 reasons: Optional[List[str]] = None, capital: Any = CAPITAL_TEST,
                 reason: Optional[str] = None) -> None:
        self._tracked = tracked
        self._blocked = blocked
        self._reasons = list(reasons or [])
        self._capital = capital
        self._reason = reason

    def state(self) -> Dict[str, Any]:
        if not self._tracked:
            return {"tracked": False,
                    "reason": self._reason or "la cuenta no tiene perfil FTMO",
                    "profile": None, "evaluation": None}
        return {"tracked": True, "reason": None,
                "profile": {"initial_capital": self._capital},
                "evaluation": {"blocked": self._blocked, "reasons": self._reasons}}


class StoreFalso:
    """Auditoría: `get_trading_config`, `log_setup`, `update_trade_result`."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        self._cfg = cfg if cfg is not None else CFG_MINIMA
        self.filas: List[Dict[str, Any]] = []

    def get_trading_config(self) -> Dict[str, Any]:
        return dict(self._cfg)

    def log_setup(self, entry: Dict[str, Any]) -> int:
        self.filas.append(dict(entry))
        return 1000 + len(self.filas)

    def update_trade_result(self, setup_id: int, resultado: Dict[str, Any]) -> None:
        return None


class PuertoFalso:
    """El puerto de ejecución: recuerda lo que le pidieron, sin bróker detrás."""

    def __init__(self, resultado: Optional[Dict[str, Any]] = None,
                 cierre: Optional[Any] = None,
                 modificacion: Optional[Any] = None,
                 posiciones: Optional[Any] = None) -> None:
        self.resultado = resultado if resultado is not None else {
            "ok": True, "retcode": 10009, "status": "llenada", "fill_price": ENTRADA_BUY,
            "deal": 555, "order": 666, "filling": 0, "volume": 0.10, "attempts": [],
        }
        self.cierre = cierre
        self.modificacion = modificacion
        self.posiciones = posiciones if posiciones is not None else []
        self.envios: List[Dict[str, Any]] = []
        self.cierres: List[Dict[str, Any]] = []
        self.modificaciones: List[Dict[str, Any]] = []

    def profit_per_lot(self, symbol, lote, entrada, sl):
        return 100.0

    def send_market_order(self, symbol, action, lote, **kw) -> Dict[str, Any]:
        self.envios.append({"symbol": symbol, "action": action, "volume": lote, **kw})
        return dict(self.resultado)

    def close_position(self, **kw) -> Dict[str, Any]:
        self.cierres.append(dict(kw))
        if self.cierre is None:
            return {"ok": True, "retcode": 10009, "status": "cerrada"}
        return dict(self.cierre)

    def modify_position(self, ticket, sl=None, tp=None) -> Dict[str, Any]:
        self.modificaciones.append({"ticket": ticket, "sl": sl, "tp": tp})
        if self.modificacion is None:
            return {"ok": True, "retcode": 10009, "status": "modificada"}
        return dict(self.modificacion)

    def positions(self) -> List[Dict[str, Any]]:
        if isinstance(self.posiciones, Exception):
            raise self.posiciones
        return [dict(p) for p in self.posiciones]


def _client(*, market=None, store=None, prop: Any = "auto", puerto: Any = "auto",
            token: Optional[str] = None, monkeypatch: Any = None) -> TestClient:
    """Un `TestClient` con los cuatro colaboradores del runtime doblados.

    `prop`/`puerto` usan el centinela `"auto"` para distinguir "no me lo pases"
    (se monta el doble que funciona) de `None` explícito (el modo sin esa pieza,
    que es el que se quiere probar).
    """
    market = market if market is not None else MarketFalso()
    store = store if store is not None else StoreFalso()
    prop = PropFalso() if prop == "auto" else prop
    puerto = PuertoFalso() if puerto == "auto" else puerto
    rt = Runtime(store=store, market=market, prop=prop, execution=puerto)
    if monkeypatch is not None:
        if token is not None:
            monkeypatch.setenv("API_TOKEN", token)
        else:
            monkeypatch.delenv("API_TOKEN", raising=False)
    return TestClient(create_app(runtime=rt))


# ---------------------------------------------------------------------------
# Contratación: token en escrituras, nunca en lectura
# ---------------------------------------------------------------------------

ESCRITURAS = [
    ("/api/trade/market", dict(CUERPO_OK)),
    ("/api/positions/123/close", None),
    ("/api/positions/123/modify", {"sl": 1.10100}),
]


def test_la_lectura_no_pide_token(monkeypatch):
    with _client(token="secreto", monkeypatch=monkeypatch) as c:
        r = c.get("/api/positions")
    assert r.status_code == 200


def test_las_escrituras_piden_token(monkeypatch):
    with _client(token="secreto", monkeypatch=monkeypatch) as c:
        for ruta, cuerpo in ESCRITURAS:
            r = c.post(ruta, json=cuerpo) if cuerpo is not None else c.post(ruta)
            assert r.status_code == 401, ruta


def test_las_escrituras_pasan_con_token(monkeypatch):
    with _client(token="secreto", monkeypatch=monkeypatch) as c:
        for ruta, cuerpo in ESCRITURAS:
            cabeceras = {"x-api-token": "secreto"}
            r = (c.post(ruta, json=cuerpo, headers=cabeceras) if cuerpo is not None
                 else c.post(ruta, headers=cabeceras))
            assert r.status_code == 200, (ruta, r.text)


# ---------------------------------------------------------------------------
# POST /api/trade/market: la ruta traduce el (cuerpo, status) del servicio
# ---------------------------------------------------------------------------


class TestTradeMarket:
    def test_una_orden_valida_sale_y_audita(self):
        store = StoreFalso()
        puerto = PuertoFalso()
        with _client(store=store, puerto=puerto) as c:
            r = c.post("/api/trade/market", json=CUERPO_OK)
        assert r.status_code == 200, r.text
        datos = r.json()
        assert datos["ok"] is True
        assert datos["symbol"] == "EURUSD"
        assert datos["setup_id"] is not None
        assert datos["audit_logged"] is True
        assert puerto.envios[0]["sl"] == SL_BUY
        assert puerto.envios[0]["tp"] == TP_BUY
        # La fila de auditoría dejó `source="manual"`: el camino que pidió la orden.
        assert store.filas[0]["source"] == "manual"

    def test_simbolo_fuera_de_la_lista_blanca_es_403(self):
        with _client() as c:
            r = c.post("/api/trade/market", json=dict(CUERPO_OK, symbol="DE40"))
        assert r.status_code == 403
        assert r.json()["detail"]["status"] == "SYMBOL_NOT_ALLOWED"

    def test_sin_puerto_de_ejecucion_es_503(self):
        with _client(puerto=None) as c:
            r = c.post("/api/trade/market", json=CUERPO_OK)
        assert r.status_code == 503
        assert r.json()["detail"]["status"] == "NO_EXECUTION_PORT"

    def test_el_gate_ftmo_bloqueado_es_403(self):
        with _client(prop=PropFalso(blocked=True, reasons=["tope diario"])) as c:
            r = c.post("/api/trade/market", json=CUERPO_OK)
        assert r.status_code == 403
        assert r.json()["detail"]["status"] == "BLOCKED_BY_RISK"

    def test_un_rechazo_del_gate_es_400_con_motivos(self):
        with _client() as c:
            r = c.post("/api/trade/market", json=dict(CUERPO_OK, tp_distance=0.00100))
        assert r.status_code == 400
        detalle = r.json()["detail"]
        assert detalle["status"] == "GATE_REJECTED"
        assert any("R:R" in m for m in detalle["reasons"])

    def test_falta_el_simbolo_es_422(self):
        """El esquema de pydantic rechaza el cuerpo antes de tocar el servicio."""
        with _client() as c:
            r = c.post("/api/trade/market", json={"action": "BUY"})
        assert r.status_code == 422

    def test_el_cuerpo_del_rechazo_viaja_entero_en_detail(self):
        """El `status` del JSON y el código HTTP son dos cosas distintas, y las dos salen."""
        with _client() as c:
            r = c.post("/api/trade/market", json=dict(CUERPO_OK, action="CERRAR"))
        assert r.status_code == 400
        detalle = r.json()["detail"]
        assert "BUY o SELL" in detalle["error"]


# ---------------------------------------------------------------------------
# GET /api/positions: lectura, sin token, con 503 solo si no se puede contestar
# ---------------------------------------------------------------------------


class TestPositions:
    def test_devuelve_las_posiciones(self):
        puerto = PuertoFalso(posiciones=[{"ticket": 1, "magic": 8882026,
                                          "symbol": "EURUSD"}])
        with _client(puerto=puerto) as c:
            r = c.get("/api/positions")
        assert r.status_code == 200
        assert r.json()["positions"][0]["ticket"] == 1

    def test_sin_posiciones_es_200_con_lista_vacia(self):
        with _client(puerto=PuertoFalso(posiciones=[])) as c:
            r = c.get("/api/positions")
        assert r.status_code == 200
        assert r.json()["positions"] == []

    def test_sin_puerto_es_503_con_el_motivo(self):
        with _client(puerto=None) as c:
            r = c.get("/api/positions")
        assert r.status_code == 503
        assert "cableado" in r.json()["detail"]["error"]

    def test_un_puerto_que_revienta_es_503_no_un_500(self):
        with _client(puerto=PuertoFalso(posiciones=RuntimeError("terminal muerta"))) as c:
            r = c.get("/api/positions")
        assert r.status_code == 503
        assert "terminal muerta" in r.json()["detail"]["error"]

    def test_el_magic_se_filtra_por_query_param(self):
        puerto = PuertoFalso(posiciones=[
            {"ticket": 1, "magic": 8882026},
            {"ticket": 2, "magic": 8882027},
        ])
        with _client(puerto=puerto) as c:
            r = c.get("/api/positions", params={"magic": 8882026})
        assert r.status_code == 200
        assert [p["ticket"] for p in r.json()["positions"]] == [1]


# ---------------------------------------------------------------------------
# POST /api/positions/{ticket}/close
# ---------------------------------------------------------------------------


class TestClose:
    def test_cierra_y_devuelve_ok(self):
        puerto = PuertoFalso()
        with _client(puerto=puerto) as c:
            r = c.post("/api/positions/12345/close")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert puerto.cierres[0]["ticket"] == 12345

    def test_cerrar_lo_que_no_existe_es_404(self):
        puerto = PuertoFalso(cierre={"ok": False, "status": "no_position",
                                     "error": "no hay posición 42"})
        with _client(puerto=puerto) as c:
            r = c.post("/api/positions/42/close")
        assert r.status_code == 404
        assert r.json()["detail"]["status"] == "no_position"

    def test_sin_puerto_es_503(self):
        with _client(puerto=None) as c:
            r = c.post("/api/positions/1/close")
        assert r.status_code == 503
        assert r.json()["detail"]["status"] == "NO_EXECUTION_PORT"

    def test_un_ticket_no_numerico_es_422(self):
        with _client() as c:
            r = c.post("/api/positions/abc/close")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/positions/{ticket}/modify
# ---------------------------------------------------------------------------


class TestModify:
    def test_mueve_el_stop_y_devuelve_ok(self):
        puerto = PuertoFalso()
        with _client(puerto=puerto) as c:
            r = c.post("/api/positions/7/modify", json={"sl": 1.10100})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert puerto.modificaciones[0] == {"ticket": 7, "sl": 1.10100, "tp": None}

    def test_sin_niveles_es_400(self):
        puerto = PuertoFalso()
        with _client(puerto=puerto) as c:
            r = c.post("/api/positions/7/modify", json={})
        assert r.status_code == 400
        assert puerto.modificaciones == []

    def test_sin_puerto_es_503(self):
        with _client(puerto=None) as c:
            r = c.post("/api/positions/7/modify", json={"sl": 1.10100})
        assert r.status_code == 503
        assert r.json()["detail"]["status"] == "NO_EXECUTION_PORT"

    def test_un_error_de_puente_es_503_modify_failed(self):
        puerto = PuertoFalso(modificacion=RuntimeError("puente caído"))
        with _client(puerto=puerto) as c:
            r = c.post("/api/positions/7/modify", json={"sl": 1.10100})
        assert r.status_code == 503
        assert r.json()["detail"]["status"] == "MODIFY_FAILED"

    def test_el_broker_sin_posicion_es_404(self):
        puerto = PuertoFalso(modificacion={"ok": False, "status": "no_position",
                                           "error": "no existe"})
        with _client(puerto=puerto) as c:
            r = c.post("/api/positions/7/modify", json={"sl": 1.10100})
        assert r.status_code == 404

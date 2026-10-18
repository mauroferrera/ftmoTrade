"""La puerta ÚNICA de salida: cada puerta tiene su test de rechazo.

Este módulo es donde se decide si una orden sale. Un "no se manda" sin test es una
intención, y las intenciones se degradan solas la primera vez que hay prisa: alguien
añade un `return` nuevo, nadie lo cubre porque los tests verdes ya estaban, y el
filtro que nadie iba a tocar se queda sin comprobar.

Por eso la estructura de cada test es la misma: se monta un servicio con UNA cosa
rota, se llama a `execute_market_trade`, y se mira el `status` de la respuesta. El
`status` es el contrato con el cliente HTTP —503 es reintentable, 403 es "no lo
intentes otra vez"— y por eso se comprueba, no solo el `ok`.

**Qué cambia respecto al test de Trinity.** El gate de noticias NO existe aquí
(D-012), así que no hay `NewsFalso` ni veredictos de calendario. En su lugar está el
gate FTMO: en vez de `market.daily_risk_state`, la puerta lee el veredicto del
`PropService` (`_estado_ftmo`), y el capital fijo con el que se dimensiona sale del
perfil del `PropService` y no del balance vivo del mercado. `PropFalso` es el doble
de esa puerta, del tamaño exacto de las tres cosas que el servicio le pregunta:
`tracked`, `evaluation.blocked` y `profile.initial_capital`.

**Por qué no se reusa `FakeMT5`.** Aquí no se prueba el envío: eso es
`test_mt5_execution.py`. Lo que se prueba es qué llega al puerto y, sobre todo, qué
NO llega.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from adapters.mt5 import mt5_execution
from api.services.execution import (
    ExecutionService,
    _status_de_ejecucion,
    execution_from,
)
from core import execution_quality

#: Capital FIJO del perfil del doble. Es la base del dimensionado (B-2): se ancla
#: aquí y no en el balance vivo, y el test lo ve en el lote.
CAPITAL_TEST = 25000.0

# ---------------------------------------------------------------------------
# Dobles mínimos
# ---------------------------------------------------------------------------


class SpecFalso:
    """Lo que `_lote_por_riesgo` y `execution_quality` leen del spec.

    Los números son de EURUSD en 5 decimales con un contrato de 100.000: con un
    tick size de 0.00001 y un tick value de 1.0, un lote pierde 100 por un SL de
    0.00100, que es la cuenta que hace un humano al mirar la pantalla de riesgo.
    """

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
        """Todo lo que no se declara es 0.0, como un spec sin ese campo.

        `getattr(spec, "x", 0.0)` es el patrón que usa el servicio; si el doble no
        tiene el atributo, ese `getattr` devuelve 0.0 y el test pasa por el mismo
        camino que un spec real sin el campo.
        """
        return 0.0


class MarketFalso:
    """La fachada de lectura del mercado. Solo `spec` y `price`.

    `account_info` existe para poder AFIRMAR que NO se llama: el dimensionado de
    este proyecto se ancla al capital fijo del perfil FTMO, no al balance vivo, así
    que el servicio no tiene por qué preguntar por la cuenta.
    """

    def __init__(self, spec: Any = "ok", precio: Any = "ok") -> None:
        self._spec = spec
        self._precio = precio
        self.llamadas: List[str] = []

    def spec(self, simbolo: str) -> Any:
        self.llamadas.append("spec")
        if isinstance(self._spec, Exception):
            raise self._spec
        return SpecFalso() if self._spec == "ok" else self._spec

    def price(self, simbolo: str) -> Any:
        self.llamadas.append("price")
        if isinstance(self._precio, Exception):
            raise self._precio
        if self._precio == "ok":
            return {"symbol": simbolo, "bid": 1.10000, "ask": 1.10012,
                    "time": 1_700_000_000}
        return self._precio

    def account_info(self) -> Dict[str, Any]:
        self.llamadas.append("account_info")
        raise AssertionError("el dimensionado no debe leer el balance vivo")


class PropFalso:
    """El gate FTMO. Devuelve el veredicto que se le configure, o revienta.

    Tres cosas y ninguna más, que son las tres que `_estado_ftmo` mira: si la
    cuenta está `tracked`, si la evaluación del día la `blocked`, y el
    `initial_capital` del perfil (la base del sizing). Un error de lectura se
    simula con `raises`.
    """

    def __init__(self, tracked: bool = True, blocked: bool = False,
                 reasons: Optional[List[str]] = None, capital: Any = CAPITAL_TEST,
                 reason: Optional[str] = None, raises: Any = None) -> None:
        self._tracked = tracked
        self._blocked = blocked
        self._reasons = list(reasons or [])
        self._capital = capital
        self._reason = reason
        self._raises = raises
        self.llamadas = 0

    def state(self) -> Dict[str, Any]:
        self.llamadas += 1
        if isinstance(self._raises, Exception):
            raise self._raises
        if not self._tracked:
            return {"account": {}, "tracked": False,
                    "reason": self._reason or "la cuenta no tiene perfil FTMO",
                    "profile": None, "evaluation": None}
        return {
            "account": {},
            "tracked": True,
            "reason": None,
            "profile": {"initial_capital": self._capital},
            "evaluation": {"blocked": self._blocked, "reasons": self._reasons},
        }


class StoreFalso:
    """Auditoría: `get_trading_config`, `log_setup`, `update_trade_result`."""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None,
                 log_setup_raises: bool = False) -> None:
        self._cfg = cfg if cfg is not None else CFG_MINIMA
        self._log_setup_raises = log_setup_raises
        self.filas: List[Dict[str, Any]] = []
        self.resultados: List[Dict[str, Any]] = []
        self._siguiente_id = 1000

    def get_trading_config(self) -> Dict[str, Any]:
        return dict(self._cfg)

    def log_setup(self, entry: Dict[str, Any]) -> int:
        if self._log_setup_raises:
            raise RuntimeError("disco lleno")
        self.filas.append(dict(entry))
        self._siguiente_id += 1
        return self._siguiente_id

    def update_trade_result(self, setup_id: int, resultado: Dict[str, Any]) -> None:
        self.resultados.append({"setup_id": setup_id, **dict(resultado)})


class PuertoFalso:
    """El puerto de ejecución. Recuerda lo que le pidieron, sin bróker detrás."""

    def __init__(self, resultado: Optional[Dict[str, Any]] = None,
                 cierre: Optional[Any] = None,
                 riesgo_por_lote: Any = 100.0,
                 modificacion: Optional[Any] = None,
                 posiciones: Optional[Any] = None) -> None:
        self.resultado = resultado if resultado is not None else {
            "ok": True, "retcode": 10009, "status": "llenada", "fill_price": 1.10012,
            "deal": 555, "order": 666, "filling": mt5_execution.ORDER_FILLING_FOK,
            "volume": 0.10, "attempts": [],
        }
        self.cierre = cierre
        self.riesgo_por_lote = riesgo_por_lote
        #: `modify_position`: dict = se devuelve, Exception = se lanza, None = éxito.
        self.modificacion = modificacion
        #: `positions`: lista de filas, Exception = se lanza, None = vacío.
        self.posiciones = posiciones if posiciones is not None else []
        self.envios: List[Dict[str, Any]] = []
        self.cierres: List[Dict[str, Any]] = []
        self.preguntas_riesgo: List[Any] = []
        self.modificaciones: List[Dict[str, Any]] = []
        self.consultas_posiciones = 0

    def profit_per_lot(self, simbolo: str, lote: float, entrada: float, sl: float) -> Any:
        self.preguntas_riesgo.append((simbolo, lote, entrada, sl))
        if isinstance(self.riesgo_por_lote, Exception):
            raise self.riesgo_por_lote
        return self.riesgo_por_lote

    def send_market_order(self, simbolo: str, accion: str, lote: float,
                          **kw: Any) -> Dict[str, Any]:
        self.envios.append({"symbol": simbolo, "action": accion, "volume": lote, **kw})
        return dict(self.resultado)

    def close_position(self, **kw: Any) -> Dict[str, Any]:
        self.cierres.append(dict(kw))
        if self.cierre is None:
            return {"ok": True, "retcode": 10009, "status": "cerrada",
                    "ticket": kw.get("ticket"), "filling": 0}
        if isinstance(self.cierre, Exception):
            raise self.cierre
        return dict(self.cierre)

    def modify_position(self, ticket: int, sl: Optional[float] = None,
                        tp: Optional[float] = None) -> Dict[str, Any]:
        self.modificaciones.append({"ticket": ticket, "sl": sl, "tp": tp})
        if self.modificacion is None:
            return {"ok": True, "status": "modified", "ticket": ticket,
                    "sl": sl, "tp": tp, "retcode": 10009, "attempts": []}
        if isinstance(self.modificacion, Exception):
            raise self.modificacion
        return dict(self.modificacion)

    def positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        self.consultas_posiciones += 1
        if isinstance(self.posiciones, Exception):
            raise self.posiciones
        return [dict(p) for p in self.posiciones]


#: Configuración mínima que deja pasar un setup decente: EURUSD en la lista blanca,
#: R:R 2, la distancia de SL declarada y el magic/comentario del sistema.
CFG_MINIMA: Dict[str, Any] = {
    "symbols_allow": ["EURUSD"],
    "min_rr": 2.0,
    "sl_distance": 0.00100,
    "magic": 8882026,
    "comment": "FTMO Copilot",
}

#: Entrada válida: BUY a 1.10012 con SL a 10 pips y TP al doble.
ENTRADA_BUY = 1.10012
SL_BUY = 1.09912
TP_BUY = 1.10212
INVALIDACION_BUY = 1.09850


_AUTO = "auto"


def _servicio(market: Any = None, store: Any = None, prop: Any = _AUTO,
              puerto: Any = _AUTO) -> ExecutionService:
    """Servicio con un puerto y un prop que FUNCIONAN salvo que se pida otra cosa.

    El defecto sería que `_servicio()` montara una instalación sin puerto o sin
    prop: entonces casi todos los tests de puerta terminarían en `NO_EXECUTION_PORT`
    o `FTMO_STATE_UNKNOWN` y no probarían la puerta que creían estar probando.
    `puerto=None`/`prop=None` explícitos siguen siendo `None` — el centinela `_AUTO`
    distingue "no me lo pases" de "pásame None", que es la diferencia entre montar
    un doble que funciona y montar el modo de solo lectura.
    """
    return ExecutionService(
        market=market if market is not None else MarketFalso(),
        store=store if store is not None else StoreFalso(),
        execution=PuertoFalso() if puerto is _AUTO else puerto,
        prop=PropFalso() if prop is _AUTO else prop,
    )


def _ejecuta(svc: ExecutionService, **kw: Any):
    """Atajo para una operación que debería salir, con lo que la ruta mandaría."""
    params: Dict[str, Any] = {
        "symbol": "EURUSD",
        "action": "BUY",
        "volume": 0.10,
        "sl_distance": 0.00100,
        "tp_distance": 0.00200,
        "score": 72.0,
        "verdict": "APROBADO",
        "invalidate_level": INVALIDACION_BUY,
        "planned_entry": ENTRADA_BUY,
        "source": "unit",
    }
    params.update(kw)
    return svc.execute_market_trade(**params)


# ---------------------------------------------------------------------------
# 0. Petición bien formada: ni esto toca el bróker
# ---------------------------------------------------------------------------


def test_sin_simbolo_no_toca_nada():
    svc = _servicio()
    cuerpo, status = svc.execute_market_trade(symbol="  ", action="BUY")
    assert status == 400
    assert "símbolo" in cuerpo["error"].lower()


def test_accion_que_no_es_buy_ni_sell_se_rechaza():
    svc = _servicio()
    cuerpo, status = svc.execute_market_trade(symbol="EURUSD", action="CERRAR")
    assert status == 400
    assert "BUY o SELL" in cuerpo["error"]
    assert svc._market.llamadas == []


def test_la_accion_se_normaliza_antes_de_mirar_las_puertas():
    """Un " buy " con espacios es una orden válida; "B U Y" no.

    La normalización tiene que pasar ANTES de comparar contra BUY/SELL y contra
    `symbols_allow`, porque las dos listas están en mayúsculas.
    """
    svc = _servicio()
    cuerpo, status = _ejecuta(svc, action=" buy ")
    assert status == 200, cuerpo
    assert cuerpo["action"] == "BUY"

    svc2 = _servicio()
    _, status2 = _ejecuta(svc2, action="B U Y")
    assert status2 == 400


# ---------------------------------------------------------------------------
# 1. Lista blanca
# ---------------------------------------------------------------------------


def test_simbolo_fuera_de_symbols_allow_es_403():
    """403, no 400: reintentarlo sin cambiar la config es inútil."""
    svc = _servicio()
    cuerpo, status = svc.execute_market_trade(symbol="DE40", action="BUY", volume=0.1,
                                              sl_distance=0.001, tp_distance=0.002)
    assert status == 403
    assert cuerpo["status"] == "SYMBOL_NOT_ALLOWED"
    assert "DE40" in cuerpo["error"]
    # Ni la cuenta FTMO ni el bróker: la lista blanca va antes.
    assert svc._prop.llamadas == 0
    assert "spec" not in svc._market.llamadas


def test_la_lista_blanca_acepta_una_cadena_con_separadores():
    """`symbols_allow` también llega como texto, no solo como lista de YAML."""
    store = StoreFalso({"symbols_allow": "eurusd, gbpusd"})
    svc = _servicio(store=store)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo


def test_una_lista_blanca_vacia_permite_todo_y_lo_dice():
    """El caso peligroso: sin lista blanca, no hay lista blanca.

    Se permite, pero se DECLARA: una lista vacía que no avisa es indistinguible de
    una lista que sí protege.
    """
    store = StoreFalso({"symbols_allow": []})
    svc = _servicio(store=store)
    cuerpo, status = _ejecuta(svc, symbol="DE40")
    assert status == 200, cuerpo
    assert any("symbols_allow" in w for w in cuerpo["warnings"])
    # Y quien sí bloquea lo dice en el mensaje de error:
    svc2 = _servicio(store=StoreFalso({"symbols_allow": ["EURUSD"]}))
    cuerpo2, status2 = _ejecuta(svc2, symbol="DE40")
    assert status2 == 403
    assert "DE40" in cuerpo2["error"]


# ---------------------------------------------------------------------------
# 2. Gate FTMO: topes del día, fail-closed
# ---------------------------------------------------------------------------


def test_el_gate_ftmo_bloquea_si_la_cuenta_no_está_seguida():
    """`tracked=False` → 403: la cuenta no tiene perfil, no se opera.

    Sin perfil no hay capital fijo ni buffer, y dimensionar sobre el balance vivo en
    una cuenta prop es justo el error que el shell existe para evitar.
    """
    svc = _servicio(prop=PropFalso(tracked=False, reason="la cuenta 999 no está mapeada"))
    cuerpo, status = _ejecuta(svc)
    assert status == 403
    assert cuerpo["status"] == "BLOCKED_BY_FTMO"
    assert "999" in cuerpo["error"]
    assert "spec" not in svc._market.llamadas


def test_un_tope_del_día_bloquea():
    svc = _servicio(prop=PropFalso(blocked=True,
                                   reasons=["4 trades hoy", "pérdida diaria al 4%"]))
    cuerpo, status = _ejecuta(svc)
    assert status == 403
    assert cuerpo["status"] == "BLOCKED_BY_RISK"
    # Los dos topes en el mensaje: hay que saber cuál de los dos paró la orden.
    assert "4 trades hoy" in cuerpo["error"]
    assert "pérdida diaria" in cuerpo["error"]
    assert "spec" not in svc._market.llamadas


def test_no_poder_leer_el_estado_ftmo_es_503_no_permiso():
    """Un fallo de lectura no es un "no" ni un "sí": es un "no se sabe".

    503 y no 403 porque el reintento tiene sentido: la terminal puede volver.
    """
    svc = _servicio(prop=PropFalso(raises=RuntimeError("terminal cerrada")))
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "FTMO_STATE_UNKNOWN"
    assert "terminal cerrada" in cuerpo["error"]


def test_sin_servicio_prop_es_503_fail_closed():
    """Sin `PropService` cableado no hay con qué comprobar el estado: se bloquea."""
    svc = _servicio(prop=None)
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "FTMO_STATE_UNKNOWN"
    assert svc._market.llamadas == []


# ---------------------------------------------------------------------------
# 3. Spec y precio
# ---------------------------------------------------------------------------


def test_el_bróker_no_publica_el_simbolo_es_404():
    svc = _servicio(market=MarketFalso(spec=None))
    cuerpo, status = _ejecuta(svc)
    assert status == 404
    assert cuerpo["status"] == "SYMBOL_NOT_FOUND"


def test_spec_ilegible_es_503():
    svc = _servicio(market=MarketFalso(spec=RuntimeError("campo corrupto")))
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "SPEC_UNAVAILABLE"
    assert "campo corrupto" in cuerpo["error"]


def test_sin_cotizacion_no_se_manda_una_orden_a_mercado():
    """El peor modo de fallo posible: enviar a mercado sin saber a qué precio."""
    svc = _servicio(market=MarketFalso(precio=None))
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "PRICE_UNAVAILABLE"
    assert svc._store.filas == []  # ni rastro: no se intentó


def test_precio_no_positivo_tambien_es_precio_ausente():
    """Un 0 o un None disfrazado de precio es lo mismo que no tener precio."""
    svc = _servicio(market=MarketFalso(precio={"bid": 0.0, "ask": 0.0}))
    _, status = _ejecuta(svc)
    assert status == 503


def test_la_entrada_se_toma_del_lado_que_se_paga():
    """Una BUY entra al ASK; una SELL al BID.

    Entrar al bid en una compra es un precio que el bróker nunca ha cotizado, y la
    diferencia es exactamente el spread que el gate no vería.
    """
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, action="BUY")
    assert status == 200, cuerpo
    assert cuerpo["entry"] == pytest.approx(1.10012)  # el ask
    assert puerto.envios[0]["sl"] == pytest.approx(1.10012 - 0.00100)

    puerto2 = PuertoFalso()
    svc2 = _servicio(puerto=puerto2)
    cuerpo2, status2 = _ejecuta(svc2, action="SELL",
                                invalidate_level=1.10050)  # arriba, para una SELL
    assert status2 == 200, cuerpo2
    assert cuerpo2["entry"] == pytest.approx(1.10000)  # el bid
    assert puerto2.envios[0]["sl"] == pytest.approx(1.10000 + 0.00100)


# ---------------------------------------------------------------------------
# 4. Distancias y lote
# ---------------------------------------------------------------------------


def test_sin_distancia_de_sl_configurada_no_se_inventa_un_stop():
    """Un YAML que apaga el SL global no cae en silencio al default de fábrica.

    `build_flat` siempre trae `sl_distance` en sus DEFAULTS, así que el único camino
    a "sin SL" es que la config lo desactive EXPLÍCITAMENTE (`sl_distance: null` y
    `sl_distance_by_symbol: {}`). Ese `None` sobrevive al aplanado y `sl_distance_for`
    devuelve `(None, "none")`. Si alguien "arreglara" esto tratando `None` como
    "usa el 0.0012", el sistema mandaría stops de un tamaño que nadie pidió.
    """
    store = StoreFalso({"symbols_allow": ["EURUSD"], "min_rr": 2.0,
                        "sl_distance": None, "sl_distance_by_symbol": {}})
    svc = _servicio(store=store)
    cuerpo, status = _ejecuta(svc, sl_distance=None)
    assert status == 400
    assert cuerpo["status"] == "NO_SL_DISTANCE"
    assert svc._store.filas == []


def test_la_distancia_de_sl_se_toma_de_la_config_por_símbolo():
    store = StoreFalso({"symbols_allow": ["EURUSD"], "min_rr": 2.0,
                        "sl_distance_by_symbol": {"EURUSD": 0.00150}})
    puerto = PuertoFalso()
    svc = _servicio(store=store, puerto=puerto)
    cuerpo, status = _ejecuta(svc, sl_distance=None, tp_distance=0.00300)
    assert status == 200, cuerpo
    assert puerto.envios[0]["sl"] == pytest.approx(1.10012 - 0.00150)
    assert cuerpo["risk"]["sl_distance_source"] != "request"


def test_el_tp_por_defecto_es_el_r_multiple_configurado():
    puerto = PuertoFalso()
    svc = _servicio(store=StoreFalso(dict(CFG_MINIMA, tp_ratio_r=3.0)), puerto=puerto)
    cuerpo, status = _ejecuta(svc, tp_distance=None)
    assert status == 200, cuerpo
    # 1.0 de SL por 3.0 de TP.
    assert puerto.envios[0]["tp"] == pytest.approx(1.10012 + 0.00300)


def test_el_lote_se_dimensiona_por_riesgo_cuando_no_viene():
    """Sin volumen, la cuenta es la de la pantalla de lote: la MISMA.

    Con un SL de 0.00100 (10 ticks de 0.00001) y tick value de 1.0 por tick y lote,
    un lote arriesga 100. Al 0.5% de 25.000 (el capital FIJO del perfil) son 125 de
    riesgo: 125/100 = 1.25 lotes.
    """
    store = StoreFalso(dict(CFG_MINIMA, risk_pct=0.5))
    puerto = PuertoFalso()
    svc = _servicio(store=store, puerto=puerto, prop=PropFalso(capital=25000.0))
    cuerpo, status = _ejecuta(svc, volume=None, score=90.0,
                              verdict="ALTA_PROBABILIDAD")
    assert status == 200, cuerpo
    assert cuerpo["risk"]["risk_pct"] == pytest.approx(0.5)
    assert puerto.envios[0]["volume"] == pytest.approx(1.25)
    assert any("Lote dimensionado por riesgo" in w for w in cuerpo["warnings"])
    nota = [w for w in cuerpo["warnings"] if "Lote dimensionado" in w][0]
    assert "125.00" in nota  # 0.5% de 25000


def test_un_verdict_que_no_es_alta_reduce_el_lote_a_la_mitad():
    """El riesgo del setup es parte de la decisión, no un adorno.

    Un verdict distinto de ALTA_PROBABILIDAD usa `reduced_risk_pct`, y si no está
    configurado, la mitad del nominal. Con 25.000 fijos: 0.5% -> 1.25 lotes; 0.25% ->
    0.62 (redondeado hacia abajo al `volume_step`).
    """
    puerto_nominal = PuertoFalso()
    svc_nominal = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)),
                            puerto=puerto_nominal)
    cuerpo_nominal, _ = _ejecuta(svc_nominal, volume=None,
                                 verdict="ALTA_PROBABILIDAD")

    puerto_reducido = PuertoFalso()
    svc_reducido = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)),
                             puerto=puerto_reducido)
    cuerpo, status = _ejecuta(svc_reducido, volume=None,
                              verdict="MEDIA_PROBABILIDAD")

    assert status == 200, cuerpo
    assert cuerpo_nominal["risk"]["risk_pct"] == pytest.approx(0.5)
    assert cuerpo["risk"]["risk_pct"] == pytest.approx(0.25)  # la mitad, sin configurar
    assert puerto_nominal.envios[0]["volume"] == pytest.approx(1.25)
    assert puerto_reducido.envios[0]["volume"] == pytest.approx(0.62)


def test_sin_capital_fijo_no_hay_dimensionado():
    """Sin perfil no hay capital fijo y el lote no se puede calcular: 400."""
    store = StoreFalso(dict(CFG_MINIMA, risk_pct=0.5))
    svc = _servicio(store=store, prop=PropFalso(capital=None))
    cuerpo, status = _ejecuta(svc, volume=None)
    assert status == 400
    assert cuerpo["status"] == "LOT_UNAVAILABLE"
    assert "capital fijo" in cuerpo["error"]


def test_el_riesgo_por_lote_se_pregunta_al_bróker_y_si_falla_se_declara():
    """`order_calc_profit` es la fuente; el tick value es la reserva.

    Y la fuente se DECLARA. La estimación y la medición del bróker producen las dos
    un número, así que deducir el origen de si el número existe etiquetaba de
    "broker" a una estimación hecha con el tick value.
    """
    puerto = PuertoFalso(riesgo_por_lote=RuntimeError("no disponible"))
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["risk"]["loss_per_lot_source"] == "estimacion"
    assert any("ESTIMADA" in w for w in cuerpo["warnings"])
    # 0.00100 de SL = 100 ticks de 0.00001, y 100 ticks x 1.0 por tick y LOTE = 100.
    assert cuerpo["risk"]["loss_per_lot"] == pytest.approx(100.0)
    assert puerto.preguntas_riesgo, "se preguntó al bróker antes de estimar"


def test_el_tick_value_de_mt5_ya_incluye_el_tamano_del_contrato():
    """El invariante, dicho como test: riesgo por lote = ticks × tick_value.

    Si el cálculo de reserva multiplicara también por `contract_size`, en EURUSD el
    riesgo del lote saldría 100.000 veces alto y el gate de presupuesto rechazaría la
    operación entera citando millones, un motivo que no lleva a ninguna parte.
    """
    puerto = PuertoFalso(riesgo_por_lote=None)  # el bróker no contesta
    svc = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)), puerto=puerto)
    cuerpo, status = _ejecuta(svc, volume=0.10)

    assert status == 200, cuerpo
    assert cuerpo["risk"]["loss_per_lot"] == pytest.approx(100.0)
    assert cuerpo["risk"]["loss_per_lot"] * 0.10 == pytest.approx(10.0)


def test_una_perdida_que_supera_el_presupuesto_se_rechaza_aunque_el_bróker_la_diga():
    """El gate de riesgo manda sobre el bróker que dice que sí se puede enviar."""
    # 0.10 lotes x 5000 = 500 de pérdida; el presupuesto al 0.25% de 25.000 = 62.50.
    puerto = PuertoFalso(riesgo_por_lote=5000.0)
    svc = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)), puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 400
    assert cuerpo["status"] == "GATE_REJECTED"
    assert any("excede el presupuesto" in r for r in cuerpo["reasons"])
    assert puerto.envios == []


# ---------------------------------------------------------------------------
# 5. Los dos gates, y la razón de mirar los dos
# ---------------------------------------------------------------------------


def test_rr_insuficiente_no_sale():
    svc = _servicio(puerto=PuertoFalso())
    cuerpo, status = _ejecuta(svc, tp_distance=0.00100)  # R:R 1.0
    assert status == 400
    assert cuerpo["status"] == "GATE_REJECTED"
    assert any("R:R" in r for r in cuerpo["reasons"])
    assert svc._store.filas[0]["validated"] == 0


def test_estructura_rota_no_sale_aunque_el_rr_sea_perfecto():
    """Precio contra el nivel de invalidez: el setup ya no es ese."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, invalidate_level=1.10050)  # por encima del precio
    assert status == 400
    assert any("Estructura rota" in r for r in cuerpo["reasons"])
    assert puerto.envios == []


def test_deriva_de_entrada_no_sale():
    """La entrada se va 0.7 pips de más y el SL son 10: el 70% del stop.

    La deriva se mide contra el nivel PLANIFICADO, y para un BUY lo adverso es
    entrar por encima. Con la entrada real en el ask (1.10012), un planificado en
    1.09942 son 0.00070 de deriva: 70% del SL, sobre el 50% que admite la regla.
    """
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, planned_entry=1.09942)
    assert status == 400
    assert execution_quality.reason_code(cuerpo["reasons"][0]) == \
        execution_quality.REASON_DERIVA
    assert puerto.envios == []


def test_spread_que_se_come_el_stop_no_sale():
    """Spread de 50 pips contra un SL de 50: hay que recorrer 2x para breakeven."""
    puerto = PuertoFalso()
    svc = _servicio(market=MarketFalso(precio={"bid": 1.10000, "ask": 1.10050,
                                                "time": 1_700_000_000}),
                    puerto=puerto)
    cuerpo, status = _ejecuta(svc, planned_entry=1.10012)
    assert status == 400
    assert execution_quality.reason_code(cuerpo["reasons"][0]) == \
        execution_quality.REASON_SPREAD
    assert puerto.envios == []


def test_un_setup_rechazado_se_guarda_igual_para_poder_estudiarlo():
    """Un rechazo sin fila no se puede depurar: nadie sabe qué se rechazó ni por qué."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, tp_distance=0.00100)
    assert status == 400
    assert len(svc._store.filas) == 1
    fila = svc._store.filas[0]
    assert fila["validated"] == 0
    assert fila["reject_reasons"] == cuerpo["reasons"]
    assert fila["direction"] == "BUY"
    assert fila["entry"] == pytest.approx(1.10012)


def test_el_approved_de_un_gate_no_es_permiso_de_ejecutar():
    """Los dos tienen que decir que sí. Uno solo no basta."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["execution_quality"]["approved"] is True
    assert cuerpo["risk_validation"]["approved"] is True


# ---------------------------------------------------------------------------
# 6. Auditoría antes del envío
# ---------------------------------------------------------------------------


def test_el_setup_se_escribe_ANTES_de_mandar_la_orden():
    """El orden de las dos llamadas es el que deja la historia creíble.

    Si el registro fuera después, un proceso que muere entre medias deja órdenes
    vivas sin fila, y una operación sin fila no sabe ni si ocurrió.
    """
    orden: List[str] = []

    class StoreOrdenado(StoreFalso):
        def log_setup(self, entry: Dict[str, Any]) -> int:
            orden.append("log_setup")
            return super().log_setup(entry)

    class PuertoOrdenado(PuertoFalso):
        def send_market_order(self, *a: Any, **kw: Any) -> Dict[str, Any]:
            orden.append("send_market_order")
            return super().send_market_order(*a, **kw)

    svc = _servicio(store=StoreOrdenado(), puerto=PuertoOrdenado())
    _ejecuta(svc)
    assert orden == ["log_setup", "send_market_order"]


def test_una_auditoria_que_falla_no_tumba_la_orden_pero_se_declara():
    """La auditoría no es requisito de ejecución, pero sí información obligatoria."""
    store = StoreFalso(log_setup_raises=True)
    puerto = PuertoFalso()
    svc = _servicio(store=store, puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert puerto.envios  # la orden SÍ salió
    assert cuerpo["setup_id"] is None
    assert cuerpo["audit_logged"] is False
    assert store.resultados == []


def test_los_intentos_de_llenado_se_graban_contra_la_misma_fila():
    """FOK falló y acabó en IOC: la fila tiene que contar los dos."""
    store = StoreFalso()
    resultado = {"ok": True, "retcode": 10009, "status": "llenada",
                 "fill_price": 1.10012, "deal": 1, "order": 2,
                 "filling": mt5_execution.ORDER_FILLING_IOC, "volume": 0.10,
                 "attempts": [
                     {"filling": mt5_execution.ORDER_FILLING_FOK, "retcode": 10030},
                     {"filling": mt5_execution.ORDER_FILLING_IOC, "retcode": 10009},
                 ]}
    puerto = PuertoFalso(resultado=resultado)
    svc = _servicio(store=store, puerto=puerto)
    _, status = _ejecuta(svc)
    assert status == 200
    assert len(store.resultados) == 1
    guardado = store.resultados[0]
    assert guardado["retcode"] == 10009
    assert len(guardado["attempts"]) == 2


# ---------------------------------------------------------------------------
# 7. Envío: lo que llega al puerto
# ---------------------------------------------------------------------------


def test_la_orden_llega_al_puerto_con_los_niveles_que_se_validaron():
    """El precio que sale de `order_send` es el que se ha medido y validado."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, magic=1234, comment="manual")
    assert status == 200, cuerpo
    envio = puerto.envios[0]
    assert envio["symbol"] == "EURUSD"
    assert envio["action"] == "BUY"
    assert envio["volume"] == pytest.approx(0.10)
    assert envio["sl"] == pytest.approx(SL_BUY)
    assert envio["tp"] == pytest.approx(TP_BUY)
    assert envio["magic"] == 1234
    assert envio["comment"] == "manual"
    assert cuerpo["entry"] == pytest.approx(envio["sl"] + 0.001)
    assert cuerpo["sl"] == pytest.approx(envio["sl"])


def test_el_magic_y_el_comentario_salen_de_la_config_si_no_vienen():
    puerto = PuertoFalso()
    svc = _servicio(store=StoreFalso(), puerto=puerto)
    _ejecuta(svc, magic=None, comment=None)
    assert puerto.envios[0]["magic"] == 8882026
    assert puerto.envios[0]["comment"] == "FTMO Copilot"


def test_sin_puerto_de_ejecucion_es_503_y_no_un_500_de_ultima_hora():
    """Se puede analizar sin poder operar, y eso tiene que decirse."""
    svc = _servicio(puerto=None)
    cuerpo, status = _ejecuta(svc)
    assert status == 503
    assert cuerpo["status"] == "NO_EXECUTION_PORT"
    # Aunque sin puerto, la fila queda: se intentó, y se sabe.
    assert len(svc._store.filas) == 1


def test_un_rechazo_del_bróker_no_es_un_200_con_ok_false():
    """El status HTTP distingue lo que el bróker rechazó de lo que falló el envío."""
    puerto = PuertoFalso(resultado={"ok": False, "status": "rejected", "retcode": 10019,
                                    "error": "invalid volume", "attempts": []})
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 400
    assert cuerpo["ok"] is False
    assert cuerpo["retcode"] == 10019
    assert cuerpo["error"] == "invalid volume"


def test_sin_terminal_es_503_para_que_el_cliente_reintente():
    """503 y no 400: un cliente que solo reintenta ante 5xx nunca reintentaría."""
    puerto = PuertoFalso(resultado={"ok": False, "status": "sin_terminal",
                                    "retcode": 10019, "error": "no hay terminal",
                                    "attempts": []})
    svc = _servicio(puerto=puerto)
    _, status = _ejecuta(svc)
    assert status == 503


def test_el_relleno_devuelto_es_el_del_bróker_no_el_pedido():
    """Si el bróker rellenó menos de lo pedido, se dice cuál de los dos."""
    puerto = PuertoFalso(resultado={"ok": True, "retcode": 10009, "status": "parcial",
                                    "fill_price": 1.10012, "volume": 0.04,
                                    "requested_volume": 0.10, "deal": 1, "order": 2,
                                    "filling": 0, "attempts": []})
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["volume"] == pytest.approx(0.04)
    assert cuerpo["requested_volume"] == pytest.approx(0.10)


def test_volumen_negativo_o_cero_se_recalcula_en_vez_de_mandarse():
    """Un 0 o un negativo no es un volumen: es la ausencia de volumen.

    El 0.62 sale de la mitad del nominal al 0.25% de 25.000 = 62.50 / 100 por lote.
    """
    puerto = PuertoFalso()
    svc = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)), puerto=puerto)
    cuerpo, status = _ejecuta(svc, volume=0.0)
    assert status == 200, cuerpo
    assert puerto.envios[0]["volume"] == pytest.approx(0.62)

    puerto_neg = PuertoFalso()
    svc_neg = _servicio(store=StoreFalso(dict(CFG_MINIMA, risk_pct=0.5)),
                        puerto=puerto_neg)
    _ejecuta(svc_neg, volume=-1.0)
    assert puerto_neg.envios[0]["volume"] == pytest.approx(0.62)


# ---------------------------------------------------------------------------
# 8. El sizing se ancla al capital FIJO (B-2), no al balance vivo
# ---------------------------------------------------------------------------


def test_el_sizing_no_pregunta_por_el_balance_vivo():
    """La base del lote es el capital fijo del perfil; la cuenta no se consulta.

    Es la propiedad B-2: anclar al balance vivo hace que la cuenta que gana
    arriesgue más y la que pierde menos, justo al revés de la regla. Aquí el
    dimensionado ni siquiera tiene vía para leer el balance.
    """
    market = MarketFalso()
    store = StoreFalso(dict(CFG_MINIMA, risk_pct=0.5))
    puerto = PuertoFalso()
    svc = _servicio(market=market, store=store, puerto=puerto,
                    prop=PropFalso(capital=25000.0))
    cuerpo, status = _ejecuta(svc, volume=None, score=90.0,
                              verdict="ALTA_PROBABILIDAD")
    assert status == 200, cuerpo
    assert "account_info" not in market.llamadas
    assert puerto.envios[0]["volume"] == pytest.approx(1.25)  # 0.5% de 25000 / 100


# ---------------------------------------------------------------------------
# 9. Cierre
# ---------------------------------------------------------------------------


def test_cerrar_llega_al_puerto_con_el_magic():
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = svc.close_position(ticket=12345)
    assert status == 200, cuerpo
    assert cuerpo["ok"] is True
    assert puerto.cierres[0]["ticket"] == 12345
    assert puerto.cierres[0]["magic"] == 8882026


def test_cerrar_no_pasa_por_symbols_allow():
    """Una lista blanca de símbolos para ABRIR no puede impedir cerrar."""
    store = StoreFalso({"symbols_allow": ["EURUSD"]})
    puerto = PuertoFalso()
    svc = _servicio(store=store, puerto=puerto)
    cuerpo, status = svc.close_position(ticket=1, symbol="DE40")
    assert status == 200, cuerpo
    assert puerto.cierres[0]["ticket"] == 1


def test_cerrar_no_pasa_por_el_gate_ftmo():
    """Cerrar reduce riesgo: si el gate FTMO lo bloqueara, dejaría posiciones vivas."""
    svc = _servicio(prop=PropFalso(blocked=True, reasons=["tope diario"]))
    cuerpo, status = svc.close_position(ticket=1)
    assert status == 200, cuerpo
    assert svc._prop.llamadas == 0


def test_cerrar_una_posicion_que_no_existe_es_404():
    puerto = PuertoFalso(cierre={"ok": False, "status": "no_position",
                                 "error": "no hay posición 42"})
    svc = _servicio(puerto=puerto)
    cuerpo, status = svc.close_position(ticket=42)
    assert status == 404
    assert cuerpo["status"] == "no_position"


def test_cerrar_sin_ticket_ni_símbolo_no_toca_el_puerto():
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    _, status = svc.close_position()
    assert status == 400
    assert puerto.cierres == []


def test_cerrar_sin_puerto_es_503():
    svc = _servicio(puerto=None)
    cuerpo, status = svc.close_position(ticket=1)
    assert status == 503
    assert cuerpo["status"] == "NO_EXECUTION_PORT"


# ---------------------------------------------------------------------------
# 9. Sin objetivo: la entrada cuya salida la decide una política
# ---------------------------------------------------------------------------


def test_no_tp_manda_la_orden_sin_objetivo():
    """`no_tp=True` → la orden sale sin TP y sin su distancia.

    La salida la decide una política posterior con el ATR del momento: un target
    fijo congelado sería una salida que la validación nunca evaluó.
    """
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, no_tp=True, tp_distance=None)
    assert status == 200, cuerpo
    assert cuerpo["tp"] is None
    assert cuerpo["risk"]["tp_distance"] is None
    assert puerto.envios[0]["tp"] is None
    assert svc._store.filas[0]["target"] is None
    assert svc._store.filas[0]["trade_result_json"]["planned_tp"] is None
    # El SL, en cambio, sigue ahí: sin stop no hay trailing que mover.
    assert cuerpo["sl"] == pytest.approx(SL_BUY)
    assert puerto.envios[0]["sl"] == pytest.approx(SL_BUY)


def test_no_tp_tambien_salta_el_tp_por_defecto_del_ratio():
    """Sin `tp_distance` el defecto es `sl * tp_ratio_r`; `no_tp` lo pisa."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = svc.execute_market_trade(
        symbol="EURUSD", action="BUY", volume=0.10, sl_distance=0.00100,
        no_tp=True, score=72.0, verdict="APROBADO",
        invalidate_level=INVALIDACION_BUY, planned_entry=ENTRADA_BUY, source="unit")
    assert status == 200, cuerpo
    assert cuerpo["tp"] is None
    assert cuerpo["risk"]["tp_distance"] is None
    assert puerto.envios[0]["tp"] is None


def test_no_tp_con_un_tp_distance_lo_ignora_y_lo_dice():
    """`no_tp=True` y `tp_distance` a la vez es una contradicción: gana `no_tp`."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc, no_tp=True)  # el atajo ya trae tp_distance=0.00200
    assert status == 200, cuerpo
    assert cuerpo["tp"] is None
    assert any("no_tp" in w for w in cuerpo["warnings"])
    assert cuerpo["risk"]["tp_distance"] is None


def test_sin_no_tp_el_tp_sigue_siendo_el_ratio_configurado():
    """La puerta por defecto no cambia: `no_tp` es opt-in, no una reescritura."""
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = _ejecuta(svc)
    assert status == 200, cuerpo
    assert cuerpo["tp"] == pytest.approx(TP_BUY)
    assert puerto.envios[0]["tp"] == pytest.approx(TP_BUY)


def test_no_tp_no_es_un_camino_sin_puertas():
    """`no_tp` omite el cálculo del TP; el resto de puertas siguen iguales."""
    svc = _servicio(prop=PropFalso(blocked=True, reasons=["tope diario"]),
                    puerto=PuertoFalso())
    cuerpo, status = _ejecuta(svc, no_tp=True)
    assert status == 403
    assert cuerpo["status"] == "BLOCKED_BY_RISK"


def test_no_tp_sin_target_no_pide_rr():
    """Sin objetivo no hay R:R que validar, no una excepción nueva."""
    svc = _servicio(puerto=PuertoFalso())
    cuerpo, status = _ejecuta(svc, no_tp=True, tp_distance=0.00100)
    assert status == 200, cuerpo
    assert cuerpo["risk_validation"].get("approved") is True


# ---------------------------------------------------------------------------
# 10. Modificación de stops y posiciones (trailing)
# ---------------------------------------------------------------------------


def test_modify_stop_llega_al_puerto():
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = svc.modify_stop(12345, sl=1.10100, tp=1.10400)
    assert status == 200, cuerpo
    assert cuerpo["ok"] is True
    assert puerto.modificaciones == [{"ticket": 12345, "sl": 1.10100, "tp": 1.10400}]


def test_modificar_no_pasa_por_las_puertas_de_apertura():
    """Mover el stop NO abre riesgo: lista blanca y gate FTMO no se consultan."""
    store = StoreFalso({"symbols_allow": ["EURUSD"]})
    market = MarketFalso()
    svc = _servicio(market=market, store=store,
                    prop=PropFalso(blocked=True, reasons=["tope diario"]),
                    puerto=PuertoFalso())
    cuerpo, status = svc.modify_stop(9, sl=1.10100)
    assert status == 200, cuerpo
    assert market.llamadas == []
    assert svc._prop.llamadas == 0


def test_modificar_no_escribe_fila_de_setup():
    """El trailing no es un setup nuevo: la fila ya existe desde la apertura."""
    store = StoreFalso()
    svc = _servicio(store=store, puerto=PuertoFalso())
    svc.modify_stop(7, sl=1.10100)
    assert store.filas == []
    assert store.resultados == []


def test_modificar_sin_ticket_es_400_sin_tocar_el_puerto():
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    cuerpo, status = svc.modify_stop(None, sl=1.10100)
    assert status == 400
    assert puerto.modificaciones == []


@pytest.mark.parametrize("ticket,sl,tp", [(None, 1.10100, None), (5, None, None)])
def test_modificar_sin_nada_que_mandar_es_400(ticket, sl, tp):
    puerto = PuertoFalso()
    svc = _servicio(puerto=puerto)
    _, status = svc.modify_stop(ticket, sl=sl, tp=tp)
    assert status == 400
    assert puerto.modificaciones == []


def test_modificar_sin_puerto_es_503():
    svc = _servicio(puerto=None)
    cuerpo, status = svc.modify_stop(1, sl=1.10100)
    assert status == 503
    assert cuerpo["status"] == "NO_EXECUTION_PORT"


def test_modificar_con_un_puerto_que_no_sabe_es_503():
    """Un puerto sin `modify_position` es una instalación que no opera trailing."""
    svc = _servicio(puerto=object())
    cuerpo, status = svc.modify_stop(1, sl=1.10100)
    assert status == 503
    assert cuerpo["status"] == "NO_EXECUTION_PORT"


def test_modificar_que_revienta_en_el_puerto_es_503_no_500():
    """Un error de puente (no del bróker) se reporta y no tumba la ruta."""
    puerto = PuertoFalso(modificacion=RuntimeError("puente caído"))
    svc = _servicio(puerto=puerto)
    cuerpo, status = svc.modify_stop(1, sl=1.10100)
    assert status == 503
    assert cuerpo["status"] == "MODIFY_FAILED"
    assert "puente caído" in cuerpo["error"]


@pytest.mark.parametrize("estado,esperado", [
    ("no_position", 404),
    ("symbol_not_found", 404),
    ("sin_terminal", 503),
    ("sin_respuesta", 503),
    ("rejected", 400),
    ("", 400),
    (None, 400),
])
def test_modificar_traduce_los_estados_del_broker(estado, esperado):
    puerto = PuertoFalso(modificacion={"ok": False, "status": estado,
                                       "error": "motivo", "retcode": 10016})
    svc = _servicio(puerto=puerto)
    cuerpo, status = svc.modify_stop(9, sl=1.10100)
    assert status == esperado
    assert cuerpo["status"] == (estado or "rejected")


def test_positions_sin_puerto_es_un_error_distinto():
    """Sin puerto el error NOMBRA el puerto: es la instalación, no el bróker."""
    svc = _servicio(puerto=None)
    filas, error = svc.positions(magic=8882026)
    assert filas is None
    assert error and "cableado" in error


def test_positions_vacio_no_es_error():
    """([], None) = "no hay nada que mover", que es lo normal un trail."""
    svc = _servicio(puerto=PuertoFalso(posiciones=[]))
    filas, error = svc.positions(magic=8882026)
    assert filas == []
    assert error is None


def test_positions_filtra_por_magic():
    """El magic es el que distingue "posiciones del sistema" de las del resto."""
    puerto = PuertoFalso(posiciones=[
        {"ticket": 1, "magic": 8882026, "symbol": "EURUSD"},
        {"ticket": 2, "magic": 8882027, "symbol": "EURUSD"},
        {"ticket": 3, "magic": 0, "symbol": "XAUUSD"},
    ])
    svc = _servicio(puerto=puerto)
    filas, error = svc.positions(magic=8882026)
    assert error is None
    assert [f["ticket"] for f in filas] == [1]


def test_positions_sin_filtro_devuelve_todo():
    puerto = PuertoFalso(posiciones=[{"ticket": 1, "magic": 8882026},
                                     {"ticket": 2, "magic": 8882027}])
    svc = _servicio(puerto=puerto)
    filas, error = svc.positions(magic=None)
    assert error is None
    assert len(filas) == 2
    assert puerto.consultas_posiciones == 1


def test_positions_sin_coincidencias_es_vacio_no_error():
    puerto = PuertoFalso(posiciones=[{"ticket": 2, "magic": 8882027}])
    svc = _servicio(puerto=puerto)
    filas, error = svc.positions(magic=8882026)
    assert filas == []
    assert error is None


def test_positions_con_un_puerto_que_no_sabe_es_error():
    svc = _servicio(puerto=object())
    filas, error = svc.positions(magic=8882026)
    assert filas is None
    assert error and "positions" in error


def test_positions_que_revienta_es_error_no_excepcion():
    puerto = PuertoFalso(posiciones=RuntimeError("terminal muerta"))
    svc = _servicio(puerto=puerto)
    filas, error = svc.positions(magic=8882026)
    assert filas is None
    assert error and "terminal muerta" in error


# ---------------------------------------------------------------------------
# 11. Utilidades del módulo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado,esperado", [
    ("sin_terminal", 503),
    ("symbol_not_found", 404),
    ("sin_precio", 503),
    ("volume_invalido", 400),
    ("rejected", 400),
    ("", 400),
    (None, 400),
])
def test_la_traduccion_de_estados_no_colapsa_todo_a_400(estado, esperado):
    assert _status_de_ejecucion(estado) == esperado


def test_execution_from_toma_las_piezas_del_runtime():
    """El servicio se monta desde el runtime, no desde el handler.

    Que sea una función y no un `if hasattr` en cada ruta es lo que garantiza que
    el watcher y la API tengan las MISMAS puertas.
    """

    class RuntimeFalso:
        market = "market"
        store = "store"
        execution = "execution"
        prop = "prop"

    svc = execution_from(RuntimeFalso())
    assert isinstance(svc, ExecutionService)
    assert svc._market == "market"
    assert svc._store == "store"
    assert svc._execution == "execution"
    assert svc._prop == "prop"


def test_execution_from_tolera_un_runtime_sin_puerto_de_ejecucion():
    """Un runtime de solo lectura da un servicio que NO opera, no uno que revienta."""

    class RuntimeFalso:
        market = "market"
        store = "store"

    svc = execution_from(RuntimeFalso())
    assert svc._execution is None
    assert svc._prop is None

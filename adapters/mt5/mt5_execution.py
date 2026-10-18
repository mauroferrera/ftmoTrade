"""Puerto de EJECUCIÓN contra MT5: la única puerta que llama a `order_send`.

Vive en `adapters/` y no en `core/` por una razón que no es de estilo: mandar una
orden necesita el bróker (bid/ask, `symbol_info`, fills) y decide cuánto dinero se
compromete. `core/` es puro por contrato y lo vigila `tests/unit/test_core_purity.py`;
una función que envía órdenes no tiene ningún sitio legítimo ahí dentro.

**Por qué un puerto y no un método más de `MT5ForexAdapter`.** El adaptador de
lectura responde "qué se ve" y este responde "qué se hace". Se separan porque las
preguntas que se pueden responder sin permiso de escribir y las que no son
diferentes: leer ticks es inocuo, `order_send` mueve dinero. Un único objeto que
puede ambas cosas es un objeto al que se le puede pedir lo segundo creyendo que se
pide lo primero.

**La forma real del bróker manda aquí.** Tres cosas que el doble de test no
avisa y que en el bróker sí:

- `order_send` devuelve un **objeto** (`MqlTradeResult`), no un dict, y sus
  atributos (`retcode`, `deal`, `order`, `price`) existen siempre aunque el envío
  haya fallado. Por eso se lee con `getattr` y no con `result["retcode"]`.
- `order_send` devuelve **None** cuando la llamada ni siquiera se procesó
  (terminal desconectada, parámetros que el serializador ni acepta). None NO es
  "orden rechazada con un motivo": es ausencia de respuesta, y se reporta como
  eso, porque un None tratado como retcode es un fallo con un número inventado.
- `positions_get` y `order_send` leen del MISMO estado del terminal. Por eso
  toda la operación (info, tick, cálculo y envío) ocurre dentro de una única
  llamada de sesión: entre dos idas y venidas al bróker el precio puede haber
  cambiado y el `price` que se valida sería el que se envió hace un segundo.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from adapters.base_adapter import SymbolSpec, TerminalUnavailable
from adapters.mt5.mt5_forex import Session, get_session

# ---------------------------------------------------------------------------
# Constantes de la API de MT5.
#
# Enteros literales, no `MetaTrader5.ORDER_TYPE_BUY`: este módulo se importa y se
# prueba SIN terminal conectado, y pedirle al paquete las constantes lo ataría a
# que el paquete esté instalado. Los valores son los de la documentación de MT5 y
# están verificados contra el bróker real en `tests/unit/test_mt5_execution.py`.
# ---------------------------------------------------------------------------

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
TRADE_ACTION_DEAL = 1
#: Mover SL/TP de una posición ABierta. Literal, igual que el resto de constantes
#: de este módulo (ver el bloque de arriba): se prueba sin paquete MT5 instalado.
TRADE_ACTION_SLTP = 3
ORDER_TIME_GTC = 0

# OJO, y está verificado contra el bróker real: `ORDER_FILLING_FOK` es **0** y
# `ORDER_FILLING_IOC` es **1**. No son 1 y 2. La lectura "natural" (FOK=1 porque es
# la primera opción del enum) es la que se cuela aquí, y con estos valores
# equivocados la mascara de `SYMBOL_FILLING_MODE` se lee al revés: en EURUSD, que
# publica `filling_mode=1`, se creería que admite IOC y no FOK, se manda IOC, y el
# bróker responde 10030 `ERR_INVALID_FILL`. Es decir, el bug se manifestaba como
# "el bróker no acepta mis órdenes", que es el peor sitio para descubrir que el
# error es de aquí.
#
# Que sean literales y no `mt5.ORDER_FILLING_FOK` es para poder importar y probar
# este módulo sin el paquete instalado; el test `test_constantes_coinciden_con_el
#_paquete` compara los dos y falla si divergen.
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1

# Bits de la MASCARA `SYMBOL_FILLING_MODE`, que NO son los valores del enum que se
# mandan en `type_filling`. Son dos numeraciones distintas que se parecen y por eso
# hay que nombrarlas por separado:
#   - `SYMBOL_FILLING_MODE` es una máscara de bits: bit 1 = FOK permitido,
#     bit 2 = IOC permitido, bit 4 = Return permitido.
#   - `type_filling` lleva el VALOR del enum (0 = FOK, 1 = IOC).
# Usar el enum para leer la máscara hace `mask & 0 == 0` siempre, y el síntoma es
# que `fillings_for` degenera siempre a "probar los dos" para cualquier símbolo
# cuya máscara no sea 0. Verificado en el bróker real: EURUSD publica
# `filling_mode=1` (solo FOK) y este es el camino que lo lee bien.
FILLING_BIT_FOK = 1
FILLING_BIT_IOC = 2

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_PLACED = 10008

# Reintentos: el precio se movió entre que se calculó y que se envió.
_RETRY_RETCODES = (10004, 10020)  # REQUOTE, PRICE_CHANGED
# Fill rechazado: el símbolo no admite ese modo con esta orden. Se prueba el otro.
INVALID_FILL = 10030

RETCODE_OK = (TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED)

RETCODE_MENSAJE = {
    10003: "Orden inválida.",
    10004: "Requote: el precio se movió.",
    10005: "Rechazada en el servidor del bróker.",
    10006: "Rechazada: el bróker no acepta la orden.",
    10008: "Orden aceptada pero todavía no ejecutada.",
    10009: "Ejecutada.",
    10012: "Tiempo de espera agotado en el bróker.",
    10013: "Solicitud incorrecta.",
    10014: "Volumen no válido para este símbolo.",
    10015: "Precio no válido para este símbolo.",
    10016: "Stops demasiado cerca o en el lado equivocado del precio.",
    10017: "Trading deshabilitado para el símbolo.",
    10018: "Mercado cerrado.",
    10019: "Sin dinero en la cuenta.",
    10020: "El precio cambió al enviar.",
    10021: "Sin cotizaciones.",
    10022: "Orden bloqueada por el bróker.",
    10024: "No hay órdenes para procesar.",
    10025: "No se puede modificar el estado de una orden existente.",
    10027: "No hay liquidity para cubrir la orden.",
    10030: "Modo de llenado no permitido para esta orden.",
    10031: "Sin conexión con el bróker.",
    10032: "Solo se permiten órdenes de cierre.",
    10039: "Operación rechazada mientras está en curso.",
}

DEFAULT_DEVIATION_POINTS = 20
CLOSE_COMMENT = "FTMO Copilot"

#: Cuántas veces se reintenta el mismo modo de llenado cuando el bróker no
#: responde o dice que el precio se movió. Es un presupuesto DELIBERADAMENTE
#: pequeño: son órdenes de mercado y cada reintento es una orden nueva que puede
#: llenarse dos veces si la primera llegó al bróker sin que la respuesta se
#: perdiera. Tres es suficiente para un bróker lento y corto para un bróker roto.
MAX_PRECIO_REINTENTOS = 3


def retcode_msg(retcode: Any, comment: Any = None) -> str:
    """Texto legible de un retcode, con el comentario del bróker si lo hay.

    El comentario del bróker va primero cuando existe porque es lo más
    específico que hay: el texto de MT5 es genérico ("orden inválida") y el del
    servidor explica por qué ("volumen no permitido").
    """
    base = RETCODE_MENSAJE.get(int(retcode), "Retcode {0} sin descripción.".format(retcode))
    extra = str(comment).strip() if comment else ""
    return "{0} {1}".format(base, extra).strip()


def _campo(info: Any, trade: Any, nombre: str, default: Any = None) -> Any:
    """Lee un `SYMBOL_*` de `symbol_info_trade` o de `symbol_info`.

    Igual que `_build_spec` en el adaptador de lectura: los campos de trading
    viven en `symbol_info_trade` en las versiones modernas del paquete. Aquí se
    repite a propósito en vez de importarse: el puerto tiene que poder trabajar
    con un `symbol_info` plano sin que el lector esté disponible.
    """
    for fuente in (trade, info):
        if fuente is None:
            continue
        v = getattr(fuente, nombre, None)
        if v is not None:
            return v
    return default


def _redondea_a_paso(valor: float, paso: Optional[float], hacia_arriba: bool = False) -> float:
    """Alinea `valor` a la rejilla `paso` del bróker.

    El volumen baja con `floor` a propósito: `volume_step=0.01` y un lote pedido
    de 0.019 no se puede enviar, pero el bróker acepta 0.01. Redondear a 0.02 sería
    inventar exposición que nadie pidió.
    """
    if not paso or paso <= 0:
        return valor
    n = valor / paso
    n = math.floor(n) if hacia_arriba else round(n)
    return round(n * paso, 10)


def _a_rejilla_precio(valor: Optional[float], tick_size: Optional[float],
                      point: Optional[float], digits: int) -> Optional[float]:
    """Alinea un precio (SL/TP) a `tick_size`, con `point` como red de seguridad.

    `tick_size` es la rejilla real del símbolo (0.00001 en EURUSD). Si el bróker no
    la publica, `point` es el piso: sin esto, un SL calculado como
    `1.12341 - 0.0012 = 1.12221` llega al bróker con decimales de más y se
    rechaza con `ERR_INVALID_STOPS` (10016), que es un rechazo opaco si no se sabe
    que la culpa era del redondeo.
    """
    if valor is None:
        return None
    paso = tick_size if (tick_size and tick_size > 0) else point
    if not paso or paso <= 0:
        return round(float(valor), int(digits))
    return round(_redondea_a_paso(float(valor), paso), int(digits))


def fillings_for(filling_mode: Any) -> List[int]:
    """Modos de llenado a probar, en orden, a partir de `SYMBOL_FILLING_MODE`.

    `filling_mode` es la MASCARA de `SYMBOL_FILLING_MODE` (bits, no el enum): bit
    1 = FOK, bit 2 = IOC, bit 4 = Return. Solo se prueban FOK e IOC porque son los
    que una orden a mercado acepta de forma compatible; Return es para salirse de
    una posición y no aplica aquí.

    Devuelve los VALORES del enum para `type_filling`, en el orden en que se
    prueban: FOK primero. No es arbitrario. FOK es el único modo que garantiza el
    llenado inmediato o nada, que es lo que quiere una orden cuyo stop se ha
    validado contra el precio de ahora; con IOC, un llenado parcial deja una
    posición de tamaño distinto al validado. Si FOK falla, el ciclo prueba IOC.

    Si el bróker no publica la máscara se prueban los dos: es la única forma de
    saber cuál admite, y equivocarse de relleno devuelve 10030, que se maneja
    reintentando el otro. La alternativa —no enviar nunca porque el bróker no
    publica el modo— deja el sistema sin poder operar.
    """
    try:
        mask = int(filling_mode or 0)
    except (TypeError, ValueError):
        mask = 0
    fills = [(FILLING_BIT_FOK, ORDER_FILLING_FOK), (FILLING_BIT_IOC, ORDER_FILLING_IOC)]
    chosen = [enum for bit, enum in fills if mask and mask & bit]
    return chosen or [ORDER_FILLING_FOK, ORDER_FILLING_IOC]


def _fila(pos: Any) -> Dict[str, Any]:
    """Posición de MT5 -> dict normalizado, sin inventar ceros donde no hay dato."""
    return {
        "ticket": int(getattr(pos, "ticket", 0) or 0),
        "symbol": str(getattr(pos, "symbol", "") or ""),
        "type": "BUY" if int(getattr(pos, "type", 0) or 0) == POSITION_TYPE_BUY else "SELL",
        "volume": float(getattr(pos, "volume", 0.0) or 0.0),
        "price_open": float(getattr(pos, "price_open", 0.0) or 0.0),
        "price_current": float(getattr(pos, "price_current", 0.0) or 0.0),
        "sl": float(getattr(pos, "sl", 0.0) or 0.0),
        "tp": float(getattr(pos, "tp", 0.0) or 0.0),
        "profit": float(getattr(pos, "profit", 0.0) or 0.0),
        "magic": int(getattr(pos, "magic", 0) or 0),
        "comment": str(getattr(pos, "comment", "") or ""),
    }


class MT5ExecutionAdapter:
    """Envío y cierre de órdenes de mercado vía `order_send`.

    Una instancia comparte la `Session` del resto del sistema (`get_session()` por
    defecto): dos sesiones contra el mismo terminal son dos doors al mismo sitio y
    la segunda escribe mientras la primera lee.
    """

    def __init__(self, session: Optional[Session] = None) -> None:
        self._session = session if session is not None else get_session()

    @property
    def session(self) -> Session:
        return self._session

    # -- helpers ----------------------------------------------------------------

    def _posiciones(self, mt5: Any, symbol: str, ticket: Optional[int]) -> List[Any]:
        """`positions_get` por ticket y, si no hay, por símbolo.

        En una cuenta hedging hay varias posiciones del mismo símbolo y cerrar por
        símbolo cerraría la equivocada: el ticket es el identificador real.
        """
        if ticket:
            found = mt5.positions_get(ticket=int(ticket))
            if found is None:
                return []
            return list(found) if isinstance(found, (list, tuple)) else [found]
        return list(mt5.positions_get(symbol=symbol) or ())

    # -- cálculo ----------------------------------------------------------------

    def profit_per_lot(self, symbol: str, volume: float, entry: float,
                       exit_price: float) -> Optional[float]:
        """Pérdida en dinero de `volume` lotes si el precio va a `exit_price`.

        Va al bróker (`order_calc_profit`) en vez de estimarlo con
        `contract_size`: la estimación con el tick value falla en cuentas con
        conversión de divisa y en símbolos con `tick_value` no lineal, y su error
        es justo del lado peligroso (subestimar la pérdida).
        """
        simbolo = str(symbol or "").strip().upper()

        def _get(mt5) -> Optional[float]:
            if not all([entry, exit_price]) or not volume:
                return None
            if hasattr(mt5, "order_calc_profit"):
                valor = mt5.order_calc_profit(simbolo, float(volume), float(entry),
                                              float(exit_price))
                if valor is not None:
                    return float(valor)
            return None

        try:
            return self._session.call(_get)
        except TerminalUnavailable:
            return None

    # -- envío ------------------------------------------------------------------

    def send_market_order(self, symbol: str, side: str, volume: float,
                          sl: Optional[float] = None, tp: Optional[float] = None,
                          magic: Optional[int] = None, comment: str = "",
                          deviation: int = DEFAULT_DEVIATION_POINTS,
                          position: Optional[int] = None) -> Dict[str, Any]:
        """Manda una orden de mercado y devuelve el resultado normalizado.

        Nunca lanza por un rechazo del bróker: un `retcode` es un RESPUESTA, no
        una excepción, y el que decide qué hacer con un rechazo es el servicio de
        ejecución (que tiene que poder registrar el intento). Solo propaga
        `TerminalUnavailable`, que sí significa que no se pudo intentar nada.

        El recorrido es siempre el mismo y en este orden, porque en cada paso una
        respuesta mala invalida el siguiente:
          1. `symbol_info` + tick: sin spec no se sabe a qué rejilla alinear.
          2. volumen normalizado a `volume_step` y dentro de `[min, max]`.
          3. SL/TP alineados a `tick_size`/`point`.
          4. `price` = ask para BUY, bid para SELL (entrar a mercado es pagar el
             spread: manda el lado que se paga, no el que se muestra).
          5. `order_send`, y si dice que el llenado no vale, el otro modo.
        """
        simbolo = str(symbol or "").strip().upper()
        side_norm = str(side or "").strip().upper()
        if side_norm == "BUY":
            order_type = ORDER_TYPE_BUY
        elif side_norm == "SELL":
            order_type = ORDER_TYPE_SELL
        else:
            return {"ok": False, "status": "rejected", "symbol": simbolo,
                    "error": "Acción inválida: solo BUY o SELL.", "retcode": None,
                    "filling": None, "attempts": []}

        def _get(mt5) -> Dict[str, Any]:
            return self._envia(mt5, simbolo, order_type, volume, sl, tp, magic,
                               comment, deviation, position)

        try:
            return self._session.call(_get)
        except TerminalUnavailable as exc:
            return {"ok": False, "status": "sin_terminal", "symbol": simbolo,
                    "error": str(exc), "retcode": None, "filling": None,
                    "attempts": []}

    def _envia(self, mt5: Any, simbolo: str, order_type: int, volume: float,
               sl: Optional[float], tp: Optional[float], magic: Optional[int],
               comment: str, deviation: int, position: Optional[int]) -> Dict[str, Any]:
        """El envío, dentro de la sesión. Ver `send_market_order` para el orden."""
        info = mt5.symbol_info(simbolo)
        if info is None:
            return {"ok": False, "status": "symbol_not_found", "symbol": simbolo,
                    "error": "El bróker no publica el símbolo {0}.".format(simbolo),
                    "retcode": None, "filling": None, "attempts": []}

        trade = self._info_trade(mt5, simbolo)
        spec = self._spec_de(info, trade, simbolo)

        tick = mt5.symbol_info_tick(simbolo)
        if tick is None:
            return {"ok": False, "status": "sin_precio", "symbol": simbolo,
                    "error": "Sin cotización para {0}: no se puede mandar la orden "
                             "a mercado sin precio.".format(simbolo),
                    "retcode": None, "filling": None, "attempts": []}

        # 2. Volumen a la rejilla del bróker y dentro de su rango declarado.
        vol, aviso_vol = self._prepara_volumen(volume, spec)
        base: Dict[str, Any] = {
            "ok": False, "symbol": simbolo,
            "action": "BUY" if order_type == ORDER_TYPE_BUY else "SELL",
            "volume": vol, "requested_volume": volume,
            "volume_adjusted": bool(aviso_vol), "volume_note": aviso_vol,
            "magic": magic, "comment": comment,
            "sl": None, "tp": None, "price": None,
            "retcode": None, "filling": None,
            "deal": None, "order": None, "attempts": [],
        }
        if vol is None:
            base["status"] = "volume_invalido"
            base["error"] = ("El lote {0} no es válido para {1}: el bróker admite "
                             "entre {2} y {3} en pasos de {4}.".format(
                                 volume, simbolo, spec.volume_min,
                                 spec.volume_max, spec.volume_step))
            return base

        # 3. Niveles a la rejilla de precios del símbolo.
        base["sl"] = _a_rejilla_precio(sl, spec.tick_size, spec.point, spec.digits)
        base["tp"] = _a_rejilla_precio(tp, spec.tick_size, spec.point, spec.digits)

        # 4. El precio al que se entra: el que se paga.
        precio = float(tick.ask or 0.0) if order_type == ORDER_TYPE_BUY else float(tick.bid or 0.0)
        if precio <= 0:
            base["status"] = "sin_precio"
            base["error"] = "Cotización incompleta para {0}: no se manda nada " \
                            "contra un precio 0.".format(simbolo)
            return base
        base["price"] = precio
        base["bid"] = float(tick.bid or 0.0)
        base["ask"] = float(tick.ask or 0.0)

        # El ciclo tiene DOS ejes y por eso la cola lleva parejas `(modo, es_reintento)`
        # en vez de solo el modo: (a) qué modos de llenado prueba, y (b) cuántas
        # veces repite el MISMO porque el precio se movió o el bróker no respondió.
        # Con un solo contador, o se pierde el segundo eje —y un bróker lento no
        # llegaría a llenar nunca— o se repite el modo indefinidamente. Y son
        # órdenes de mercado: cada reintento es una orden nueva que puede llenarse
        # dos veces si la primera llegó al bróker y se perdió la respuesta, así que
        # el presupuesto de repeticiones es la exposición máxima que se acepta.
        pendientes: List[Tuple[int, bool]] = [
            (f, False) for f in fillings_for(_campo(info, trade, "filling_mode", 0))
        ]
        modos_vistos: set = set()
        ultimo_error: Optional[str] = None
        intentos_reintento = 0

        while pendientes:
            fill, es_reintento = pendientes.pop(0)
            if es_reintento:
                if intentos_reintento >= MAX_PRECIO_REINTENTOS:
                    break
                intentos_reintento += 1
            elif fill in modos_vistos:
                continue
            modos_vistos.add(fill)

            request: Dict[str, Any] = {
                "action": TRADE_ACTION_DEAL,
                "symbol": simbolo,
                "volume": vol,
                "type": order_type,
                "price": precio,
                "sl": base["sl"] or 0.0,
                "tp": base["tp"] or 0.0,
                "deviation": int(deviation or DEFAULT_DEVIATION_POINTS),
                "magic": int(magic or 0),
                "comment": str(comment or "")[:31],
                "type_time": getattr(mt5, "ORDER_TIME_GTC", ORDER_TIME_GTC),
                "type_filling": fill,
            }
            if position:
                # Cerrar por ticket: sin esto, en una cuenta hedging la orden
                # cierra la posición que el bróker considere, no la que se pidió.
                request["position"] = int(position)

            result = mt5.order_send(request)
            nombre_fill = "FOK" if fill == ORDER_FILLING_FOK else "IOC"
            if result is None:
                # Ausencia de respuesta. Se reintenta el mismo modo de llenado y
                # solo se cambia de modo si de verdad cambia el motivo: un None no
                # dice nada sobre el relleno, así que rotar a FOK para descubrir
                # que tampoco contesta habría gastado los dos intentos para no
                # aprender nada.
                ultimo_error = ("order_send devolvió None (revisa la conexión "
                                "con la terminal).")
                base["attempts"].append({"filling": nombre_fill, "retcode": None,
                                         "ok": False, "error": ultimo_error})
                # Ausencia de respuesta: se reintenta el MISMO modo. Un None no
                # dice nada sobre el relleno, así que rotar a FOK para descubrir
                # que tampoco contesta gastaría los dos modos sin aprender nada.
                pendientes.insert(0, (fill, True))
                continue

            retcode = int(getattr(result, "retcode", 0) or 0)
            comentario = getattr(result, "comment", None)
            ok = retcode in RETCODE_OK
            base["attempts"].append({
                "filling": nombre_fill, "retcode": retcode, "ok": ok,
                "error": None if ok else retcode_msg(retcode, comentario),
            })

            if ok:
                base.update({
                    "ok": True, "status": "sent", "retcode": retcode,
                    "filling": nombre_fill,
                    "deal": getattr(result, "deal", None),
                    "order": getattr(result, "order", None),
                    "fill_price": float(getattr(result, "price", 0.0) or 0.0) or None,
                    "broker_comment": comentario,
                })
                return base

            ultimo_error = retcode_msg(retcode, comentario)
            if retcode == INVALID_FILL:
                # El modo no vale: se prueba el otro, una sola vez.
                otro = (ORDER_FILLING_IOC if fill == ORDER_FILLING_FOK
                        else ORDER_FILLING_FOK)
                if otro not in modos_vistos:
                    pendientes.insert(0, (otro, False))
                continue
            if retcode in _RETRY_RETCODES:
                # El precio se movió: se reintenta con el precio nuevo, no con el
                # que ya no existe. Sin esto, un movimiento de 1 punto en un
                # segundo deja al bróker rechacando un precio caducado.
                nuevo = (float(tick.ask or 0.0) if order_type == ORDER_TYPE_BUY
                         else float(tick.bid or 0.0))
                if nuevo > 0:
                    precio = nuevo
                    base["price"] = precio
                # El MISMO modo: un requote no dice nada sobre el relleno, así que
                # cambiarlo gastaría el otro intento por un motivo que no existe.
                pendientes.insert(0, (fill, True))
                continue
            break

        base["status"] = "rejected"
        base["error"] = ultimo_error or "El bróker no devolvió una respuesta utilizable."
        base["retcode"] = (base["attempts"][-1].get("retcode")
                           if base["attempts"] else None)
        return base

    def _prepara_volumen(self, volume: Any, spec: SymbolSpec) -> Tuple[Optional[float], Optional[str]]:
        """`(lote_enviable, aviso)` o `(None, por qué no)`.

        El aviso existe porque bajar el lote es una decisión que cambia la
        exposición: quien llama tiene que poder enseñarla, no encontrarla
        después en un log.
        """
        try:
            vol = float(volume)
        except (TypeError, ValueError):
            return None, "El lote no es un número."
        if vol <= 0:
            return None, "El lote tiene que ser mayor que cero."

        vmin = float(spec.volume_min or 0.01)
        vmax = float(spec.volume_max or 0.0) or 100.0
        step = float(spec.volume_step or 0.01)
        if vol < vmin:
            return None, ("Lote {0} por debajo del mínimo del bróker ({1})."
                          .format(vol, vmin))
        if vol > vmax:
            return None, ("Lote {0} por encima del máximo del bróker ({1})."
                          .format(vol, vmax))
        alineado = _redondea_a_paso(vol, step, hacia_arriba=False)
        if alineado < vmin:
            alineado = vmin
        if alineado > vmax:
            alineado = _redondea_a_paso(vmax, step, hacia_arriba=True)
        aviso = None
        if abs(alineado - vol) > 1e-9:
            aviso = ("Lote ajustado de {0} a {1} para caer en la rejilla del "
                     "bróker (paso {2}).".format(vol, alineado, step))
        return alineado, aviso

    @staticmethod
    def _info_trade(mt5: Any, simbolo: str) -> Optional[Any]:
        """`symbol_info_trade` si existe; None si el paquete es viejo."""
        fn = getattr(mt5, "symbol_info_trade", None)
        if fn is None:
            return None
        try:
            return fn(simbolo)
        except Exception:  # noqa: BLE001 - versión antigua: se lee de symbol_info
            return None

    @staticmethod
    def _spec_de(info: Any, trade: Any, simbolo: str) -> SymbolSpec:
        def c(nombre: str, default: Any) -> Any:
            v = _campo(info, trade, nombre, default)
            return default if v is None else v

        return SymbolSpec(
            symbol=simbolo,
            digits=int(c("digits", 5) or 5),
            point=float(c("point", 0.0) or 0.0),
            contract_size=float(c("trade_contract_size", 0.0) or 0.0),
            tick_size=float(c("trade_tick_size", 0.0) or 0.0),
            tick_value=float(c("trade_tick_value", 0.0) or 0.0),
            volume_min=float(c("volume_min", 0.01) or 0.01),
            volume_max=float(c("volume_max", 100.0) or 0.0) or 100.0,
            volume_step=float(c("volume_step", 0.01) or 0.01),
        )

    # -- cierre -----------------------------------------------------------------

    def close_position(self, ticket: Optional[int] = None, symbol: Optional[str] = None,
                       magic: Optional[int] = None, comment: str = CLOSE_COMMENT,
                       deviation: int = DEFAULT_DEVIATION_POINTS) -> Dict[str, Any]:
        """Cierra la posición indicada.

        **El `magic` de la posición se preserva.** No es un detalle: el EA cuenta
        sus operaciones por magic, y un cierre con el magic de la web le borra del
        conteo una operación que sí ocurrió. El precio al que se cierra es el que
        se RECIBE: `bid` al cerrar una BUY (es a lo que se vende), `ask` al cerrar
        una SELL. Cerrar una BUY al ask es pagar el spread dos veces.
        """
        simbolo = str(symbol or "").strip().upper()

        def _get(mt5) -> Dict[str, Any]:
            posiciones = self._posiciones(mt5, simbolo, ticket)
            if not posiciones:
                return {"ok": False, "status": "no_position", "symbol": simbolo,
                        "ticket": ticket, "error": "No hay posición abierta "
                        "para {0}.".format(ticket or simbolo), "attempts": []}
            pos = posiciones[0]
            fila = _fila(pos)
            pos_type = int(getattr(pos, "type", 0) or 0)
            order_type = ORDER_TYPE_SELL if pos_type == POSITION_TYPE_BUY else ORDER_TYPE_BUY

            info = mt5.symbol_info(fila["symbol"])
            if info is None:
                return {"ok": False, "status": "symbol_not_found", "symbol": fila["symbol"],
                        "ticket": fila["ticket"], "error": "El bróker no publica "
                        "{0}.".format(fila["symbol"]), "attempts": []}
            trade = self._info_trade(mt5, fila["symbol"])
            tick = mt5.symbol_info_tick(fila["symbol"])
            if tick is None:
                return {"ok": False, "status": "sin_precio", "symbol": fila["symbol"],
                        "ticket": fila["ticket"], "error": "Sin cotización para "
                        "cerrar {0}.".format(fila["symbol"]), "attempts": []}

            precio = float(tick.bid or 0.0) if order_type == ORDER_TYPE_SELL else float(tick.ask or 0.0)
            if precio <= 0:
                return {"ok": False, "status": "sin_precio", "symbol": fila["symbol"],
                        "ticket": fila["ticket"], "error": "Cotización incompleta "
                        "para cerrar.", "attempts": []}

            # El magic de la posición manda; el de config es solo el respaldo para
            # una posición sin magic (no debería existir, pero si existe, cerrar
            # con 0 la haría invisible al EA igual que con cualquier otro número).
            magic_cierre = fila["magic"] or int(magic or 0)

            base: Dict[str, Any] = {
                "ok": False, "symbol": fila["symbol"], "ticket": fila["ticket"],
                "position_type": fila["type"], "volume": fila["volume"],
                "magic": magic_cierre, "comment": comment, "price": precio,
                "retcode": None, "filling": None, "deal": None, "order": None,
                "attempts": [],
            }

            pendientes: List[Tuple[int, bool]] = [
                (f, False) for f in fillings_for(_campo(info, trade, "filling_mode", 0))
            ]
            modos_vistos: set = set()
            ultimo_error: Optional[str] = None
            intentos_reintento = 0
            while pendientes:
                fill, es_reintento = pendientes.pop(0)
                if es_reintento:
                    if intentos_reintento >= MAX_PRECIO_REINTENTOS:
                        break
                    intentos_reintento += 1
                elif fill in modos_vistos:
                    continue
                modos_vistos.add(fill)
                request = {
                    "action": TRADE_ACTION_DEAL,
                    "symbol": fila["symbol"],
                    "volume": fila["volume"],
                    "type": order_type,
                    "position": fila["ticket"],
                    "price": precio,
                    "deviation": int(deviation or DEFAULT_DEVIATION_POINTS),
                    "magic": magic_cierre,
                    "comment": str(comment or CLOSE_COMMENT)[:31],
                    "type_time": getattr(mt5, "ORDER_TIME_GTC", ORDER_TIME_GTC),
                    "type_filling": fill,
                }
                result = mt5.order_send(request)
                nombre_fill = "FOK" if fill == ORDER_FILLING_FOK else "IOC"
                if result is None:
                    ultimo_error = ("order_send devolvió None (revisa la conexión "
                                    "con la terminal).")
                    base["attempts"].append({"filling": nombre_fill, "retcode": None,
                                             "ok": False, "error": ultimo_error})
                    pendientes.insert(0, (fill, True))
                    continue
                retcode = int(getattr(result, "retcode", 0) or 0)
                comentario = getattr(result, "comment", None)
                ok = retcode in RETCODE_OK
                base["attempts"].append({
                    "filling": nombre_fill, "retcode": retcode, "ok": ok,
                    "error": None if ok else retcode_msg(retcode, comentario)})
                if ok:
                    base.update({"ok": True, "status": "closed", "retcode": retcode,
                                 "filling": nombre_fill,
                                 "deal": getattr(result, "deal", None),
                                 "order": getattr(result, "order", None),
                                 "fill_price": float(getattr(result, "price", 0.0) or 0.0) or None})
                    return base
                ultimo_error = retcode_msg(retcode, comentario)
                if retcode == INVALID_FILL:
                    otro = (ORDER_FILLING_IOC if fill == ORDER_FILLING_FOK
                            else ORDER_FILLING_FOK)
                    if otro not in modos_vistos:
                        pendientes.insert(0, (otro, False))
                    continue
                if retcode in _RETRY_RETCODES:
                    nuevo = (float(tick.bid or 0.0) if order_type == ORDER_TYPE_SELL
                             else float(tick.ask or 0.0))
                    if nuevo > 0:
                        precio = nuevo
                        base["price"] = precio
                    pendientes.insert(0, (fill, True))
                    continue
                break

            base["status"] = "rejected"
            base["error"] = ultimo_error or "El bróker no devolvió una respuesta utilizable."
            base["retcode"] = (base["attempts"][-1].get("retcode")
                               if base["attempts"] else None)
            return base

        try:
            return self._session.call(_get)
        except TerminalUnavailable as exc:
            return {"ok": False, "status": "sin_terminal", "symbol": simbolo,
                    "ticket": ticket, "error": str(exc), "attempts": []}

    # -- modificación -----------------------------------------------------------

    def modify_position(self, ticket: int, sl: Optional[float] = None,
                        tp: Optional[float] = None) -> Dict[str, Any]:
        """Mueve el SL y/o el TP de una posición ABierta (`TRADE_ACTION_SLTP`).

        `sl=None` o `tp=None` conserva el nivel que YA tiene la posición: el
        bróker pisa los dos campos con lo que recibe, así que la petición lleva el
        valor actual en vez de no llevar nada. `0.0` explícito es la forma en que
        MT5 entiende "sin nivel". El precio se alinea a la rejilla igual que en la
        apertura, porque un stop off-grid se rechaza con el mismo `10016` opaco.

        No rota rellenos ni pide precio: esta orden no compra ni vende, solo
        mueve dos campos de una posición que ya existe — por eso `attempts` lleva
        un solo entry. Y no preserva el magic porque no lo toca: la posición
        sigue siendo la misma.
        """
        try:
            int_ticket = int(ticket)
        except (TypeError, ValueError):
            return {"ok": False, "status": "no_position", "ticket": ticket,
                    "error": "Ticket inválido: {0!r}".format(ticket), "attempts": []}

        def _get(mt5) -> Dict[str, Any]:
            posiciones = self._posiciones(mt5, "", int_ticket)
            if not posiciones:
                return {"ok": False, "status": "no_position", "ticket": int_ticket,
                        "error": "No hay posición abierta con ticket {0}.".format(int_ticket),
                        "attempts": []}
            fila = _fila(posiciones[0])
            info = mt5.symbol_info(fila["symbol"])
            if info is None:
                return {"ok": False, "status": "symbol_not_found", "symbol": fila["symbol"],
                        "ticket": fila["ticket"], "error": "El bróker no publica "
                        "{0}.".format(fila["symbol"]), "attempts": []}
            trade = self._info_trade(mt5, fila["symbol"])
            spec = self._spec_de(info, trade, fila["symbol"])

            sl_final = _a_rejilla_precio(sl if sl is not None else fila["sl"],
                                         spec.tick_size, spec.point, spec.digits)
            tp_final = _a_rejilla_precio(tp if tp is not None else fila["tp"],
                                         spec.tick_size, spec.point, spec.digits)
            request = {
                "action": TRADE_ACTION_SLTP,
                "symbol": fila["symbol"],
                "position": fila["ticket"],
                "sl": float(sl_final or 0.0),
                "tp": float(tp_final or 0.0),
            }
            result = mt5.order_send(request)

            def _salida(status: str, retcode: Optional[int], comentario: Any,
                        defecto: Any = None) -> Dict[str, Any]:
                if status == "modified":
                    motivo = None
                elif retcode is None:
                    motivo = str(comentario or defecto
                                 or "El bróker no devolvió una respuesta utilizable.")
                else:
                    motivo = retcode_msg(retcode, comentario)
                return {
                    "ok": status == "modified", "status": status,
                    "symbol": fila["symbol"], "ticket": fila["ticket"],
                    "magic": fila["magic"], "sl": request["sl"], "tp": request["tp"],
                    "retcode": retcode,
                    "comment": comentario if retcode is not None else None,
                    "error": motivo,
                    "attempts": [{"retcode": retcode,
                                  "ok": status == "modified", "error": motivo}],
                }

            if result is None:
                # Ausencia de respuesta, dicha como tal y no como un rechazo: no
                # hay retcode que traducir y quien llama debe poder reintentar.
                return _salida("sin_respuesta", None, None,
                               "order_send devolvió None (revisa la conexión "
                               "con la terminal).")
            retcode = int(getattr(result, "retcode", 0) or 0)
            ok = retcode in RETCODE_OK
            return _salida("modified" if ok else "rejected", retcode,
                           getattr(result, "comment", None))

        try:
            return self._session.call(_get)
        except TerminalUnavailable as exc:
            return {"ok": False, "status": "sin_terminal", "ticket": int_ticket,
                    "error": str(exc), "attempts": []}

    def positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Posiciones abiertas, normalizadas. Vacío si el bróker no publica."""
        simbolo = str(symbol or "").strip().upper() if symbol else None

        def _get(mt5) -> List[Dict[str, Any]]:
            crudas = (mt5.positions_get(symbol=simbolo) if simbolo
                      else mt5.positions_get())
            return [_fila(p) for p in (crudas or ())]

        try:
            return self._session.call(_get)
        except TerminalUnavailable:
            return []


def adapter(session: Optional[Session] = None) -> MT5ExecutionAdapter:
    """Fábrica. Igual que en el adaptador de lectura: permite inyectar la sesión."""
    return MT5ExecutionAdapter(session)


__all__ = [
    "CLOSE_COMMENT",
    "DEFAULT_DEVIATION_POINTS",
    "FILLING_BIT_FOK",
    "FILLING_BIT_IOC",
    "MAX_PRECIO_REINTENTOS",
    "MT5ExecutionAdapter",
    "ORDER_FILLING_FOK",
    "ORDER_FILLING_IOC",
    "ORDER_TIME_GTC",
    "ORDER_TYPE_BUY",
    "ORDER_TYPE_SELL",
    "POSITION_TYPE_BUY",
    "POSITION_TYPE_SELL",
    "RETCODE_OK",
    "TRADE_ACTION_DEAL",
    "TRADE_ACTION_SLTP",
    "adapter",
    "fillings_for",
    "retcode_msg",
]

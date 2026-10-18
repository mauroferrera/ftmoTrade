"""El watcher: qué se está vigilando y qué setups hay ahora mismo.

Dos operaciones, y solo dos
---------------------------
`estado()` y `escanear()`. El escaneo evalúa, **audita** y avisa; no manda órdenes. Es
una decisión explícita (D-005): el watcher mira y deja constancia de lo que miró. La ruta
que ejecuta llega en M5 (`ExecutionService`), con su token y sus manos; hasta entonces
`AUTO_EJECUCION_DISPONIBLE` es `False` y no hay forma de que un escaneo lo convierta en
`True` con un parámetro.

Por qué el watcher NO tiene su propio camino a la orden
-------------------------------------------------------
En REF el escaneo tenía un `executor` inyectado y, con `auto_execute` a true, mandaba la
orden dentro del mismo ciclo que detectaba el setup. Eso convierte un `auto_execute: true`
en una decisión de arranque: el sistema empieza a operar solo desde que el proceso existe,
sin nadie que pulse nada. Un proceso que arranca y opera no tiene a nadie mirando cuando
opera. Lo que se trae de REF aquí es la dedup del ciclo, el log del setup y el log de los
descartes — no el camino a la orden.

Lo que este servicio NO hace
----------------------------
- No ejecuta. `AUTO_EJECUCION_DISPONIBLE` es `False`.
- No lanza el bucle. Escanear es una operación que el operador pide y una función que se
  puede probar. Un bucle en segundo plano dentro de un servicio HTTP sigue vivo cuando el
  request ya terminó.
- No conoce noticias ni ejecución (fuera de alcance, ver `docs/ALCANCE.md`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core import setup_gate, setup_lifecycle, strategy

log = logging.getLogger(__name__)

#: ¿Hay auto-ejecución? No. Publicarlo en el estado, en vez de callarlo, es lo que
#: permite que el frontend muestre el interruptor apagado y explique por qué.
AUTO_EJECUCION_DISPONIBLE = False

#: Motivo único de por qué no. Va en el estado y en la respuesta de la ruta de
#: auto-ejecución, con la misma frase en los dos sitios: si divergen, el operador lee uno
#: y se decide por el otro.
MOTIVO_SIN_AUTO_EJECUCION = (
    "El watcher no manda órdenes: evalúa setups y los audita. La ejecución llega en M5 "
    "con sus puertas (lista blanca, riesgo del día, validate_entry y calidad de "
    "ejecución). Auto-ejecutar requiere escribir antes ese camino, no solo encender una "
    "bandera."
)

#: Timeframe por defecto cuando la configuración no lo dice. M15 es el timeframe del
#: gate y el que trae `strategy.yaml` para los símbolos del alcance.
TIMEFRAME_DEFECTO = "M15"


class WatcherService:
    """Estado y escaneo del watcher. No ejecuta órdenes.

    Los colaboradores son opcionales e inyectables: un test hace
    `WatcherService(market=Falso(), store=Falso())` y ya tiene el servicio entero. Sin
    `store` no hay ni configuración ni auditoría, y el escaneo lo dice en vez de fingir
    que no pasa nada.
    """

    def __init__(self, market: Any = None, store: Any = None) -> None:
        self._market = market
        self._store = store
        #: Firmas de rechazo ya registradas, por símbolo/timeframe. Vive en la instancia
        #: y NO en la base de datos: es memoria de este proceso para no escribir la misma
        #: fila en cada ciclo. Si el proceso se reinicia se vuelve a escribir una fila,
        #: que es un ruido aceptable; lo que no es aceptable es una deduplicación
        #: persistente que oculte un rechazo nuevo porque se parece a uno viejo.
        self._rejects: Dict[Tuple[str, str], Tuple[Any, ...]] = {}

    # -- configuración ----------------------------------------------------------

    def _cfg_watcher(self) -> Dict[str, Any]:
        if self._store is None:
            return {}
        try:
            return self._store.get_watcher_config() or {}
        except Exception as exc:  # noqa: BLE001 - sin config no hay ciclo que escanear
            log.warning("no se pudo leer la configuración del watcher: %s", exc)
            return {}

    def _cfg_trading(self) -> Dict[str, Any]:
        if self._store is None:
            return {}
        try:
            return self._store.get_trading_config() or {}
        except Exception:  # noqa: BLE001 - el umbral cae al default de strategy.yaml
            return {}

    def _watch_list(self) -> List[Dict[str, str]]:
        """La lista de `{symbol, timeframe}` a vigilar.

        `watcher.symbols` manda si está; si no, se usa la lista `symbols` del propio
        `strategy.yaml`. El YAML de este proyecto declara los símbolos a nivel raíz y no
        dentro de `watcher`, así que leer solo `watcher.symbols` dejaría el watcher sin
        nada que mirar y un escaneo en calma que en realidad no miró nada.
        """
        wcfg = self._cfg_watcher()
        entradas = wcfg.get("symbols") or []
        if not entradas:
            entradas = (self._cfg_trading() or {}).get("symbols") or []
        out: List[Dict[str, str]] = []
        for e in entradas or []:
            if isinstance(e, dict):
                sym = str(e.get("symbol") or "").strip().upper()
                tf = str(e.get("timeframe") or TIMEFRAME_DEFECTO).strip().upper()
            else:
                sym = str(e or "").strip().upper()
                tf = TIMEFRAME_DEFECTO
            if sym:
                out.append({"symbol": sym, "timeframe": tf})
        return out

    # -- puertos de lectura ------------------------------------------------------

    def _estado_previo(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        if self._store is None:
            return None
        try:
            return self._store.get_setup_state(symbol, timeframe)
        except Exception as exc:  # noqa: BLE001 - sin estado previo se trata como nuevo
            log.warning("no se pudo leer el estado previo de %s %s: %s", symbol, timeframe, exc)
            return None

    def _snapshot(self, symbol: str, timeframe: str, tcfg: Dict[str, Any],
                  force_killzone: bool, ahora: Optional[datetime]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """`(snapshot, error)`.

        El error es un `str` porque se propaga como evento y no como excepción: un símbolo
        sin datos en este escaneo no es un fallo del escaneo entero. Si un símbolo del
        watcher no tiene velas, los demás siguen evaluándose y el operador ve cuál fue.
        """
        if self._market is None:
            return None, "mercado no cableado"
        try:
            snap = self._market.snapshot(symbol, timeframe, cfg=tcfg,
                                         force_killzone=force_killzone, now=ahora)
        except Exception as exc:  # noqa: BLE001 - un símbolo no tumba el ciclo
            return None, str(exc)
        if not isinstance(snap, dict) or not (snap.get("risk_engine") or {}):
            return None, "sin risk engine en el snapshot (no hay velas o no se pudo calcular)"
        return snap, None

    def _spec(self, symbol: str) -> Any:
        """El `SymbolSpec` del símbolo, o `None`. Nunca inventado.

        `None` degrada los motivos a distancia de precio, igual que en las rutas de
        setup. Un spec inventado daría unidades equivocadas en el mercado donde más caro
        sale equivocarse.
        """
        if self._market is None:
            return None
        try:
            return self._market.spec(symbol)
        except Exception:  # noqa: BLE001 - el spec es presentación, no veredicto
            return None

    # -- auditoría --------------------------------------------------------------

    def _registra(self, entry: Dict[str, Any]) -> Optional[int]:
        """`store.log_setup` sin dejar que un fallo de auditoría tumbe el escaneo.

        `None` es un resultado honesto: el escaneo puede continuar (auditar no es
        vigente) pero el ciclo lo dice en `audit_logged=False`. Perder la fila de un
        setup detectado es un agujero en la auditoría, y esconderlo dejaría un "no se
        registró ningún setup" indistinguible de "se registró y se perdió".
        """
        if self._store is None:
            return None
        try:
            return self._store.log_setup(entry)
        except Exception as exc:  # noqa: BLE001 - perder el log no cambia lo que pasó
            log.warning("no se pudo registrar el setup en setup_log: %s", exc)
            return None

    def _nuevo_estado(self, symbol: str, timeframe: str, gate: Dict[str, Any],
                      status: str, notificado: bool) -> None:
        if self._store is None:
            return
        row = {
            "direction": gate.get("direction"),
            "score": gate.get("score"),
            "verdict": gate.get("verdict"),
            "entry": gate.get("entry"),
            "sl": gate.get("sl"),
            "target": gate.get("tp"),
            "invalidate_level": gate.get("invalidate_level"),
            "status": status,
        }
        try:
            self._store.new_setup_state(symbol, timeframe, row)
        except Exception as exc:  # noqa: BLE001 - el estado es dedup, no la auditoría
            log.warning("no se pudo guardar el estado de %s %s: %s", symbol, timeframe, exc)

    def _cierra_estado(self, symbol: str, timeframe: str, status: str) -> None:
        if self._store is None:
            return
        try:
            self._store.update_setup_state(symbol, timeframe, {"status": status})
        except Exception as exc:  # noqa: BLE001 - idem
            log.warning("no se pudo actualizar el estado de %s %s: %s", symbol, timeframe, exc)

    # -- estado -----------------------------------------------------------------

    def estado(self) -> Dict[str, Any]:
        """Configuración del watcher y estado vigente de cada símbolo que vigila.

        `auto_execute` sale SIEMPRE a `False` aunque `strategy.yaml` diga otra cosa: es
        la lectura del interruptor, y el interruptor controla algo que no existe.
        Publicar aquí el valor del YAML haría que el panel dijera "AUTO" sobre algo que
        no ocurre. `auto_execute_conf` publica lo que el YAML pide, que es un hecho
        distinto.
        """
        wcfg = self._cfg_watcher()
        watch = self._watch_list()
        states: List[Optional[Dict[str, Any]]] = []
        for e in watch:
            states.append(self._estado_previo(e["symbol"], e["timeframe"]))
        return {
            "enabled": bool(wcfg.get("enabled")),
            "scan_interval_sec": wcfg.get("scan_interval_sec"),
            "dedup_ttl_sec": wcfg.get("dedup_ttl_sec") or setup_lifecycle.DEDUP_TTL_DEFECTO,
            "symbols": self._watch_list(),
            "states": states,
            "min_score": strategy.min_score_for(self._cfg_trading()),
            "auto_execute_conf": bool(wcfg.get("auto_execute")),
            "auto_execute": False,
            "auto_execute_disponible": AUTO_EJECUCION_DISPONIBLE,
            "auto_execute_motivo": None if AUTO_EJECUCION_DISPONIBLE else MOTIVO_SIN_AUTO_EJECUCION,
            "dry_run": True,
        }

    # -- escaneo ----------------------------------------------------------------

    def escanear(self, ahora: Optional[datetime] = None,
                 force_killzone: bool = False) -> Dict[str, Any]:
        """Un ciclo completo: evalúa, audita y devuelve los eventos.

        Por símbolo, en orden de la configuración:

        1. Snapshot. Si no hay datos, evento `error` con el motivo y se sigue con el
           siguiente símbolo: un símbolo roto no es un escaneo roto.
        2. Gate (`core.setup_gate`). Sin veredicto (`None`) no hay nada que decidir y no
           se escribe nada.
        3. Transición (`core.setup_lifecycle`), y según ella: `new`, `closed` o nada.

        Devuelve `events` para lo que el frontend cuenta y `rejects` para los descartes
        que dejaron fila de auditoría. Van separados porque no son lo mismo: mezclarlos
        es lo que hace que un panel diga "3 setups" con un setup detectado y dos
        descartes.

        `audit_logged` cuenta los setups APROBADOS que dejaron fila; `audit_fallidos`
        cuenta las filas de descartes que se perdieron (la firma ya se guardó y no se
        reintentará). Van separados porque el primero responde a "¿se registró lo que
        encontré?" y el segundo a "¿se perdió algo de lo que vi?".
        """
        momento = ahora or datetime.now(timezone.utc)
        wcfg = self._cfg_watcher()
        tcfg = self._cfg_trading()
        ttl = float(wcfg.get("dedup_ttl_sec") or setup_lifecycle.DEDUP_TTL_DEFECTO)
        vigilados = self._watch_list()

        eventos: List[Dict[str, Any]] = []
        rechazos: List[Dict[str, Any]] = []
        fallos: List[Dict[str, Any]] = []
        auditados = 0
        huecos = 0

        for entrada in vigilados:
            symbol = entrada["symbol"]
            timeframe = entrada["timeframe"]
            base = {"symbol": symbol, "timeframe": timeframe}

            snap, error = self._snapshot(symbol, timeframe, tcfg, force_killzone, momento)
            if snap is None:
                eventos.append({**base, "event": "error", "error": error})
                fallos.append({**base, "error": error})
                continue

            gate = setup_gate.evaluate_gate(snap, tcfg, spec=self._spec(symbol))
            if gate is None:
                eventos.append({
                    **base, "event": "error",
                    "error": "el snapshot no trae scores: no hay veredicto que evaluar",
                })
                fallos.append({**base, "error": "sin scores"})
                continue

            componentes = setup_lifecycle.componentes_del_score(snap, gate["direction"])
            fila_base = {
                **base,
                "direction": gate["direction"],
                "score": gate["score"],
                "verdict": gate["verdict"],
                "killzone": (gate.get("killzone") or {}).get("name"),
                "entry": gate["entry"],
                "sl": gate["sl"],
                "tp": gate["tp"],
                "invalidate_level": gate["invalidate_level"],
            }
            previo = self._estado_previo(symbol, timeframe)
            transicion = setup_lifecycle.decide_transition(
                previo, bool(gate["approved"]), ahora=momento, ttl_sec=ttl)

            if transicion == "new":
                self._nuevo_estado(symbol, timeframe, gate,
                                   status=setup_lifecycle.ESTADO_ACTIVO, notificado=True)
                if self._audita_aprobado(symbol, timeframe, gate, componentes) is not None:
                    auditados += 1
                eventos.append({
                    **fila_base, "event": "new",
                    "reason": "; ".join(gate["reasons"]),
                    "notificado": True,
                    "auto_ejecutado": False,
                })
            elif transicion == "closed":
                self._cierra_estado(symbol, timeframe, "closed")
                eventos.append({
                    **fila_base, "event": "closed",
                    "reason": "el setup ya no cumple el gate o expiró su TTL ({0:.0f} s)".format(ttl),
                })
            elif not gate["approved"] and transicion is None:
                descartado = self._audita_rechazo(symbol, timeframe, gate, componentes)
                if descartado is not None:
                    rechazos.append(descartado)
                    if not descartado["auditado"]:
                        huecos += 1
                    eventos.append({
                        **fila_base, "event": "rejected",
                        "reasons": list(gate["reasons"]),
                    })

        return {
            "events": eventos,
            "rejects": rechazos,
            "fallos": fallos,
            "audit_logged": auditados,
            "audit_fallidos": huecos,
            "auto_ejecutado": False,
            "auto_ejecucion_disponible": AUTO_EJECUCION_DISPONIBLE,
            "dry_run": True,
            "symbols": len(vigilados),
            "motivo": (None if vigilados else
                       "no hay símbolos que vigilar: 'watcher.symbols' y los 'symbols' "
                       "de strategy.yaml están vacíos."),
            "ttl_sec": ttl,
            "killzone_forced": bool(force_killzone),
            "scan_at": momento.isoformat(),
        }

    # -- escritura de filas -----------------------------------------------------

    @staticmethod
    def _breakdown(gate: Dict[str, Any], componentes: Dict[str, Any]) -> Dict[str, Any]:
        """El `breakdown_json` de la fila: el gate y los componentes del score."""
        return {"gate": gate, **componentes}

    def _audita_aprobado(self, symbol: str, timeframe: str, gate: Dict[str, Any],
                         componentes: Dict[str, Any]) -> Optional[int]:
        """Fila de un setup APROBADO, sin ejecutar.

        `trade_result` lleva `{"executed": False, "dry_run": True}`. Una fila de setup
        detectado que apareciera más tarde con `trade_result_json` vacío se leería como
        una operación abierta de la que todavía no se sabe el resultado; marcarla aquí es
        lo que hace que la fila sea una **intención auditada**, que es lo que fue.
        """
        return self._registra({
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": gate["direction"],
            "verdict": gate["verdict"],
            "score": gate["score"],
            "breakdown_json": self._breakdown(gate, componentes),
            "entry": gate.get("entry"),
            "sl": gate.get("sl"),
            "target": gate.get("tp"),
            "invalidate_level": gate.get("invalidate_level"),
            "validated": True,
            "reject_reasons": [],
            "trade_result_json": {"executed": False, "dry_run": True},
            "source": "watcher",
        })

    def _audita_rechazo(self, symbol: str, timeframe: str, gate: Dict[str, Any],
                        componentes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Fila de un setup RECHAZADO, registrada UNA vez por firma.

        La dedup es por `{símbolo, timeframe}` y firma (`core.setup_lifecycle`), en
        memoria de este proceso. Sin esto, un setup que lleva media hora por debajo del
        umbral escribe una fila cada `scan_interval_sec`, y el histórico deja de servir
        para nada.

        Devuelve el evento si se escribió, `None` si ya estaba registrado. Un `None` no
        es un fallo: es un rechazo que ya se sabe.
        """
        clave = (symbol.upper(), timeframe.upper())
        firma = setup_lifecycle.firma_de_rechazo(gate)
        if self._rejects.get(clave) == firma:
            return None
        self._rejects[clave] = firma

        veredicto = gate.get("verdict") or "RECHAZADO"
        id_fila = self._registra({
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": gate["direction"],
            "verdict": veredicto,
            "score": gate["score"],
            "breakdown_json": self._breakdown(gate, componentes),
            "entry": gate.get("entry"),
            "sl": gate.get("sl"),
            "target": gate.get("tp"),
            "invalidate_level": gate.get("invalidate_level"),
            "validated": False,
            "reject_reasons": list(gate.get("reasons") or []),
            "trade_result_json": {},
            "source": "watcher",
        })
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": gate["direction"],
            "score": gate["score"],
            "verdict": veredicto,
            "reasons": list(gate.get("reasons") or []),
            "auditado": id_fila is not None,
        }


__all__ = [
    "AUTO_EJECUCION_DISPONIBLE",
    "MOTIVO_SIN_AUTO_EJECUCION",
    "TIMEFRAME_DEFECTO",
    "WatcherService",
]

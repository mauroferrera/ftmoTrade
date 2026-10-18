"""El ciclo de vida de un setup detectado: nuevo, activo, cerrado y su deduplicación.

Modulo PURO, como el resto de `core/`: no lee la base de datos, no habla con el bróker
y no mira el reloj del sistema salvo que se le pase. Recibe la fila de estado anterior y
el veredicto del gate, y devuelve la transición. Quien recibe el `None` no tiene nada que
hacer; quien recibe un nombre tiene que persistirlo.

Por qué vive aquí y no en el servicio del watcher
-------------------------------------------------
En REF esta lógica estaba dentro de `watcher.py`, junto con el escaneo, la notificación
y el envío de órdenes. Mezclada con la I/O era imposible de probar sin base de datos y
sin reloj. Aquí hay un problema distinto y más pequeño: **cuándo tiene que volver a
hablar el watcher**. Un setup que sigue siendo válido no debe reavisar en cada ciclo
(ruido), uno que ha muerto sí debe cerrarse (para que el siguiente sea una alerta nueva y
no un recargo del anterior), y uno que caducó por TTL tampoco puede quedar abierto para
siempre.

Lo que este módulo NO hace
--------------------------
- No decide si un setup es operable: eso es `core.setup_gate`. El gate llega ya resuelto
  como `gate_ok` (un `bool`), no como un dict de score: recomputar el veredicto aquí
  sería una segunda fuente de la misma verdad.
- No conoce noticias ni ejecución (M5). Los únicos estados que este sistema escribe hoy
  son `active` y `closed`; cualquier otro estado no terminal (uno escrito por una fase
  futura) se trata como ciclo abierto, que es la lectura segura: mientras no esté
  terminado, no se reabre.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from core import clock

#: TTL por defecto de un ciclo de alerta, en segundos. 900 son 15 minutos: el timeframe
#: por defecto del watcher es M15, así que una vela. El default solo se usa cuando la
#: configuración no lo declara, y por eso se publica en la respuesta del estado en vez
#: de asumirse en silencio.
DEDUP_TTL_DEFECTO = 900.0

#: Estados que autorizan a abrir un ciclo nuevo. Un setup que estaba en cualquiera de
#: ellos ya terminó, y por lo tanto la fila anterior no debe impedir que el siguiente
#: setup del mismo símbolo se avise como nuevo.
ESTADOS_TERMINADOS = ("", "none", "closed", "expired", "invalidated")

#: Estado de un setup detectado y aún vigente.
ESTADO_ACTIVO = "active"

#: La dirección del gate y la clave del `risk_engine` del snapshot no se llaman igual.
#: `evaluate_gate` elige entre `bull` y `bear`; sin este mapa, `direccion="BUY"` buscaría
#: `"buy"` en el snapshot, no lo encontraría y devolvería un breakdown vacío: una fila
#: de auditoría con `{}` en lugar del desglose, sin ningún error que lo diga.
LADO_EN_EL_SNAPSHOT = {"BUY": "bull", "SELL": "bear"}


def _campo(fila: Any, nombre: str, defecto: Any = None) -> Any:
    """Lee un campo de la fila de estado tanto de dict como de objeto/tupla.

    `store.get_setup_state` devuelve un dict, pero admitir las dos formas evita el
    `AttributeError` en el momento en que un estado guardado con otra forma tiene que
    cerrarse.
    """
    if fila is None:
        return defecto
    if isinstance(fila, dict):
        return fila.get(nombre, defecto)
    return getattr(fila, nombre, defecto)


def decide_transition(fila_previa: Any,
                      gate_ok: bool,
                      ahora: Optional[datetime] = None,
                      ttl_sec: float = DEDUP_TTL_DEFECTO) -> Optional[str]:
    """La transición de estado para un `{símbolo, timeframe}` dado.

    Devuelve `"new"`, `"closed"` o `None`. `None` significa **no hay nada que hacer**
    (el setup sigue igual, no se reavisa ni se cierra).

    Reglas:
      - Sin fila previa, o con un estado TERMINADO: `"new"` si el gate aprueba, si no
        `None`. La fila vieja ya no bloquea un ciclo nuevo.
      - Con un estado ABIERTO (cualquiera que no sea terminal): `"closed"` si el gate
        dejó de aprobar o si se superó el TTL; si no, `None` (sigue vivo, no se reavisa).

    El TTL se mide desde `created_at`, no desde la última actualización: un setup que se
    reavisa cada ciclo no debe reiniciar su reloj de vida.
    """
    fila = fila_previa
    if fila is None:
        return "new" if gate_ok else None

    estado = str(_campo(fila, "status", "") or "").strip().lower()
    if estado in ESTADOS_TERMINADOS:
        return "new" if gate_ok else None

    creado = clock.parse_iso(_campo(fila, "created_at"))
    ahora_dt = ahora or clock.now_utc()
    expirado = bool(
        creado is not None
        and ttl_sec
        and (ahora_dt - creado).total_seconds() > float(ttl_sec)
    )
    if not gate_ok or expirado:
        return "closed"
    return None


def clave_de_motivo(motivo: Any) -> str:
    """La parte de un motivo de rechazo que NO es un número.

    "Score 41.0 < umbral 59.5." y "Score 43.0 < umbral 59.5." son el mismo rechazo;
    una firma de dedup que incluyera los números los tomaría por dos y escribiría dos
    filas de auditoría por el mismo hecho.
    """
    return "".join(c for c in str(motivo or "") if not c.isdigit() and c not in ".:%,")


def firma_de_rechazo(gate: Optional[Dict[str, Any]]) -> Tuple[Any, ...]:
    """Una firma estable de un rechazo: dirección, banda de score y motivos.

    Se usa para deduplicar en memoria los descartes de un ciclo: dos ciclos seguidos
    con la misma dirección y los mismos motivos son el MISMO rechazo y no deben dejar
    dos filas por minuto.
    """
    g = gate or {}
    banda = int(float(g.get("score") or 0) // 5)
    motivos = tuple(sorted(clave_de_motivo(m) for m in (g.get("reasons") or [])))
    return (str(g.get("direction") or "").upper(), banda, motivos)


def componentes_del_score(snapshot: Optional[Dict[str, Any]],
                          direccion: Optional[str]) -> Dict[str, Any]:
    """El desglose del score del lado elegido, para la fila de auditoría.

    Se devuelve TAL CUAL viene del `risk_engine`, sin recomponer: una fila de auditoría
    cuyo desglose no suma lo que el motor dijo es peor que una sin desglose.

    Devuelve una copia PROFUNDA: el `dict()` a secas dejaría los subdicts compartidos con
    el snapshot, y una mutación en el camino que serializa cambiaría lo que se registró.
    """
    lado = LADO_EN_EL_SNAPSHOT.get(str(direccion or "").upper())
    if lado is None:
        return {}
    re_ = (snapshot or {}).get("risk_engine") or {}
    entrada = re_.get(lado) or {}
    comps = entrada.get("components") if isinstance(entrada, dict) else None
    return deepcopy(comps) if isinstance(comps, dict) else {}


__all__ = [
    "DEDUP_TTL_DEFECTO",
    "ESTADO_ACTIVO",
    "ESTADOS_TERMINADOS",
    "LADO_EN_EL_SNAPSHOT",
    "clave_de_motivo",
    "componentes_del_score",
    "decide_transition",
    "firma_de_rechazo",
]

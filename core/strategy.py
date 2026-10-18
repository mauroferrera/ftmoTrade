"""La estrategia de trading, materializada y validada. Parte PURA.

`config/strategy.yaml` es la fuente única de verdad de las reglas. Este módulo es la
mitad pura de leerlo: recibe el documento ya leído (un `dict`), lo valida clave por
clave y devuelve la config PLANA que consumen el gate (`core/setup_gate.py`), el
watcher y `core/risk_engine`.

La otra mitad (leer el fichero) vive en `settings/strategy_source.py`. La separación
no es purismo: `tests/unit/test_core_purity.py` prohíbe `open()` dentro de `core/`, y
con razón. Un módulo que abre un fichero convierte cada test unitario en un test de
integración que depende del disco, y el `strategy.yaml` de producción es justo el
fichero que un test no debe poder pisar.

Normas de validación (las del proyecto de referencia, por el mismo motivo):
  - Una clave desconocida se ignora, para no petar el arranque por reglas nuevas.
  - Un valor con el tipo incorrecto o fuera de rango LANZA `StrategyConfigError`,
    para que el plan nunca se ejecute con reglas rotas en silencio.

Recorte respecto a Trinity (ver `docs/ALCANCE.md`): aquí NO entran los bloques de
COT/CVD/news, las fuentes de datos ni los textos del agente. Solo lo que el copiloto
usa: riesgo, ejecución, score, killzones y reloj.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from core import risk_engine

#: Suelo del umbral de score, en puntos. NO es la calibración de producción (esa
#: está en `strategy.yaml`: 59.5); es lo que se usa si la clave no está. Existe como
#: constante y no como literal en varios sitios porque `cfg.get("min_score") or 80.0`
#: confunde "no está" con "es 0.0", y 0.0 es un valor legal (el rango validado es
#: 0..100): un umbral pedido explícitamente en 0 se sustituía en silencio por 80.
DEFAULT_MIN_SCORE: float = 80.0

#: Clave legacy: distancia de SL expresada en pips. Se LEE (para un YAML viejo, con
#: la ayuda del spec del símbolo) pero no se escribe ni se valida: el contrato del
#: sistema es la distancia absoluta, en unidades de precio.
LEGACY_SL_PIPS_KEY = "sl_default_pips"


class StrategyConfigError(ValueError):
    """`strategy.yaml` no es utilizable: no es un mapeo o tiene una regla rota.

    Hereda de `ValueError`, igual que `PropProfilesError`: un YAML de reglas mal
    escrito se VE (sale 400 por `api/errors.py`), no se esconde tras un default
    silencioso. Ejecutar con reglas rotas sin decirlo es el fallo que este error
    existe para evitar.
    """


# ============================================================
# Defaults (lo que se usa si la clave no está en el YAML)
# ============================================================

DEFAULTS: Dict[str, Any] = {
    # Riesgo.
    "risk_pct": 0.5,
    "reduced_risk_pct": 0.25,
    "max_trades_day": 3,
    # Ejecución.
    "magic": 8882026,
    "comment": "FTMO Copilot",
    # Distancia de SL en UNIDADES DE PRECIO (no pips: un pip no significa lo mismo
    # en EURUSD que en oro). `sl_distance_by_symbol` manda sobre el valor global.
    "sl_distance": 0.0012,
    "sl_distance_by_symbol": {},
    "tp_ratio_r": 2.0,
    "deviation_points": 15,
    "symbols_allow": [],
    # Score. `min_score` y `risk_weights` se leen de `risk_engine` y no se duplican:
    # antes este archivo, `risk_engine` y el YAML tenían tres juegos distintos de
    # pesos y cuatro umbrales. La calibración de producción vive solo en el YAML.
    "min_score": DEFAULT_MIN_SCORE,
    "min_rr": 2.0,
    "setup_ttl_minutes": 45.0,
    "risk_weights": dict(risk_engine.DEFAULT_WEIGHTS),
    "grade_thresholds": dict(risk_engine.DEFAULT_GRADE_THRESHOLDS),
    # Killzones (UTC), tomadas de `risk_engine` y no repetidas: dos copias podían
    # divergir y entonces el path de test y el real medirían ventanas distintas.
    "killzones": [dict(w) for w in risk_engine.DEFAULT_KILLZONES],
}


# ============================================================
# Especificación de tipos y rangos por clave, para la validación
# ============================================================
# (tipo, mínimo, máximo). El mínimo o el máximo a None es "sin límite".

_BOOLEAN = "bool"
_NUMBER = "number"
_STRING = "string"
_LIST_STR = "list_str"
_MAP_NUMBER = "map_number"


def _spec_for(key: str) -> Optional[Tuple[str, Optional[float], Optional[float]]]:
    """`(tipo, mínimo, máximo)` de una clave plana, o `None` si no se valida."""
    table: Dict[str, Tuple[str, Any, Any]] = {
        "risk_pct": (_NUMBER, 0.0, 100.0),
        "reduced_risk_pct": (_NUMBER, 0.0, 100.0),
        "max_trades_day": (_NUMBER, 0, None),
        "magic": (_NUMBER, 0, None),
        "comment": (_STRING, None, None),
        "sl_distance": (_NUMBER, 0.0, None),
        "sl_distance_by_symbol": (_MAP_NUMBER, None, None),
        "tp_ratio_r": (_NUMBER, 0.0, None),
        "deviation_points": (_NUMBER, 0, None),
        "symbols_allow": (_LIST_STR, None, None),
        "min_score": (_NUMBER, 0.0, 100.0),
        "min_rr": (_NUMBER, 0.0, None),
        "setup_ttl_minutes": (_NUMBER, 0, None),
    }
    return table.get(key)


def _check(value: Any, spec: Tuple[str, Any, Any], key: str) -> None:
    """Valida un valor contra su spec. Lanza `StrategyConfigError` si no cuadra."""
    tipo = spec[0]
    if tipo == _NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StrategyConfigError(f"strategy.yaml: '{key}' debe ser un número.")
        lo, hi = spec[1], spec[2]
        if lo is not None and value < lo:
            raise StrategyConfigError(
                f"strategy.yaml: '{key}' no puede ser menor que {lo} (es {value})."
            )
        if hi is not None and value > hi:
            raise StrategyConfigError(
                f"strategy.yaml: '{key}' no puede ser mayor que {hi} (es {value})."
            )
    elif tipo == _STRING:
        if not isinstance(value, str):
            raise StrategyConfigError(f"strategy.yaml: '{key}' debe ser texto.")
    elif tipo == _LIST_STR:
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise StrategyConfigError(
                f"strategy.yaml: '{key}' debe ser una lista de textos."
            )
    elif tipo == _MAP_NUMBER:
        if not isinstance(value, dict):
            raise StrategyConfigError(
                f"strategy.yaml: '{key}' debe ser un mapa {{SÍMBOLO: número}}."
            )
        for sym, dist in value.items():
            if isinstance(dist, bool) or not isinstance(dist, (int, float)):
                raise StrategyConfigError(
                    f"strategy.yaml: '{key}.{sym}' debe ser un número (es {dist!r})."
                )


# ============================================================
# Parseo de las secciones no planas
# ============================================================


def _parse_killzones(raw: Any) -> List[Dict[str, str]]:
    """Ventanas UTC aplanadas. Acepta el formato plano (lista) y el jerárquico
    `{timezone, windows: [...]}`.

    La zona horaria informativa no se aplana: los consumidores trabajan siempre en
    UTC (`risk_engine.killzone_score`), y aplicarle un offset aquí cambiaría las
    ventanas sin que nadie lo pidiera.
    """
    if raw is None:
        return [dict(w) for w in DEFAULTS["killzones"]]
    if isinstance(raw, dict):
        raw = raw.get("windows")
    if not isinstance(raw, list):
        raise StrategyConfigError(
            "strategy.yaml: 'killzones' debe ser una lista de ventanas."
        )
    out: List[Dict[str, str]] = []
    for w in raw:
        if not isinstance(w, dict) or "start" not in w or "end" not in w:
            raise StrategyConfigError(
                "strategy.yaml: cada killzone debe tener 'name', 'start' y 'end' HH:MM."
            )
        for k in ("start", "end"):
            if not isinstance(w.get(k), str) or ":" not in str(w[k]):
                raise StrategyConfigError(
                    f"strategy.yaml: killzone '{w.get('name', '?')}' requiere "
                    f"'{k}' en formato HH:MM."
                )
        out.append({
            "name": str(w.get("name") or "?"),
            "start": str(w["start"]),
            "end": str(w["end"]),
        })
    return out or [dict(w) for w in DEFAULTS["killzones"]]


def _parse_weights(raw: Any, key: str = "score.weights") -> Dict[str, float]:
    """Pesos del score, con los de `risk_engine` como base y solo las claves conocidas."""
    if raw is None:
        return dict(DEFAULTS["risk_weights"])
    if not isinstance(raw, dict):
        raise StrategyConfigError(f"strategy.yaml: '{key}' debe ser un objeto de pesos.")
    base = dict(DEFAULTS["risk_weights"])
    for k in ("smc", "killzone"):
        if k in raw:
            v = raw[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise StrategyConfigError(
                    f"strategy.yaml: 'weights.{k}' debe ser un número >= 0."
                )
            base[k] = float(v)
    return base


def _json_o_estructura(value: Any) -> Any:
    """Deserializa una estructura que pueda llegar como JSON string.

    `risk_weights` y `killzones` pueden venir ya parseados (del YAML crudo o de un
    test) o como string JSON (histórico de `build_flat` de Trinity). Aceptar solo una
    forma hace que la otra ruta falle con un error que no parece de configuración:
    `killzone_score` iterando un string levanta `AttributeError` y el score entero
    revienta sin decir por qué.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value


def _primer_valor(cfg: Optional[Dict[str, Any]], *caminos: Any) -> Any:
    """El primer valor de `caminos` que exista y no esté vacío.

    Acepta la forma plana y la anidada del `strategy.yaml` porque las dos existen: el
    gate consume la plana y un test puede pasar el YAML crudo. Aceptar solo una deja
    el otro camino con los defaults en silencio, y un default silencioso es
    indistinguible de una decisión.
    """
    c = cfg or {}
    for camino in caminos:
        valor: Any = c
        for clave in camino:
            if not isinstance(valor, dict):
                valor = None
                break
            valor = valor.get(clave)
        if valor is None or valor == "" or valor == {} or valor == []:
            continue
        return valor
    return None


# ============================================================
# Aplanado: documento anidado -> config plana
# ============================================================


def build_flat(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """El `strategy.yaml` anidado -> config PLANA que consume el gate.

    Mapea cada escalar de su sección YAML a su clave plana, aplana `score.weights` y
    las `killzones`, y VALIDA el resultado. Lanza `StrategyConfigError` en cuanto una
    regla tiene el tipo equivocado o está fuera de rango.

    Las estructuras (`risk_weights`, `killzones`) se devuelven como `dict`/`list`
    nativos, no como JSON: aquí no hay round-trip por la base que obligue a
    serializarlas, y un consumidor que las lea como estructura es el caso normal.
    """
    doc = doc or {}
    flat: Dict[str, Any] = dict(DEFAULTS)

    d = doc.get("risk") or {}
    e = doc.get("execution") or {}
    s = doc.get("score") or {}

    scalar: Dict[str, Tuple[Dict[str, Any], str]] = {
        "risk_pct": (d, "risk_pct"),
        "reduced_risk_pct": (d, "reduced_risk_pct"),
        "max_trades_day": (d, "max_trades_day"),
        "magic": (e, "magic"),
        "comment": (e, "comment"),
        "sl_distance": (e, "sl_distance"),
        "sl_distance_by_symbol": (e, "sl_distance_by_symbol"),
        "tp_ratio_r": (e, "tp_ratio_r"),
        "deviation_points": (e, "deviation_points"),
        "symbols_allow": (e, "symbols_allow"),
        "min_score": (s, "min_score"),
        "min_rr": (s, "min_rr"),
        "setup_ttl_minutes": (s, "setup_ttl_minutes"),
    }
    for key, (src, k) in scalar.items():
        if k in src:
            flat[key] = src[k]

    flat["risk_weights"] = _parse_weights(s.get("weights"))
    flat["killzones"] = _parse_killzones(doc.get("killzones"))
    gth = s.get("grade_thresholds")
    if isinstance(gth, dict):
        flat["grade_thresholds"] = gth
    elif risk_engine is not None:
        flat["grade_thresholds"] = dict(risk_engine.DEFAULT_GRADE_THRESHOLDS)

    for key in scalar:
        spec = _spec_for(key)
        if spec is not None:
            _check(flat[key], spec, key)
    return flat


# ============================================================
# Resolvers (aceptan la config plana y la anidada)
# ============================================================


def min_score_for(cfg: Optional[Dict[str, Any]]) -> float:
    """El umbral de score resuelto desde la config, en un solo sitio.

    Distingue "no configurado" de "configurado a 0.0", que es un valor legal (el rango
    validado es 0..100). El patrón `float(cfg.get("min_score") or 80.0)` no podía: un 0
    explícito se caía al `or` y se ejecutaba con 80 sin que nada lo dijera.
    """
    raw = _primer_valor(cfg, ("min_score",), ("score", "min_score"))
    if raw is None or isinstance(raw, bool):
        return DEFAULT_MIN_SCORE
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_SCORE


def tp_ratio_for(cfg: Optional[Dict[str, Any]]) -> float:
    """El múltiplo de R del objetivo, resuelto desde la config.

    Acepta la forma plana (`tp_ratio_r`) y la anidada (`execution.tp_ratio_r`) porque
    `store.get_trading_config()` devuelve el YAML CRUDO. Sin esto, el gate leería
    `cfg.get("tp_ratio_r")` sobre el dict anidado, no lo encontraría y usaría su
    default: un `tp_ratio_r: 3.0` en el YAML se ignoraría en silencio.

    Un valor no positivo o no numérico cae al default: un TP en la propia entrada (o
    detrás) no es un plan. `DEFAULTS["tp_ratio_r"]` es la misma política que el fallo de
    `plan_levels`, declarada aquí para que el YAML sea la única fuente.
    """
    raw = _primer_valor(cfg, ("tp_ratio_r",), ("execution", "tp_ratio_r"))
    if raw is None or isinstance(raw, bool):
        return float(DEFAULTS["tp_ratio_r"])
    try:
        valor = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULTS["tp_ratio_r"])
    return valor if valor > 0 else float(DEFAULTS["tp_ratio_r"])


def risk_weights_for(cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, float]]:
    """Los pesos del score desde la config, o `None` si no hay ninguno utilizable.

    `None` NO es lo mismo que `{}`: `None` deja que `risk_engine` aplique sus defaults,
    y `{}` haría lo mismo pero fingiendo que la configuración dijo algo. La diferencia
    permite avisar de que se mide con la calibración de fábrica.
    """
    pesos = _json_o_estructura(
        _primer_valor(cfg, ("risk_weights",), ("score", "weights"))
    )
    if not isinstance(pesos, dict):
        return None
    salida: Dict[str, float] = {}
    for clave, valor in pesos.items():
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            continue
        salida[str(clave)] = float(valor)
    return salida or None


def killzones_for(cfg: Optional[Dict[str, Any]] = None) -> Optional[List[Dict[str, str]]]:
    """Las ventanas de killzone desde la config, o `None` si no hay ninguna."""
    ventanas = _primer_valor(cfg, ("killzones",), ("score", "killzones"))
    ventanas = _json_o_estructura(ventanas)
    if isinstance(ventanas, dict):
        ventanas = ventanas.get("windows")
    if not isinstance(ventanas, list):
        return None
    salida: List[Dict[str, str]] = []
    for w in ventanas:
        if not isinstance(w, dict):
            continue
        inicio, fin = w.get("start"), w.get("end")
        if not isinstance(inicio, str) or not isinstance(fin, str):
            continue
        if ":" not in inicio or ":" not in fin:
            continue
        salida.append({"name": str(w.get("name") or "?"), "start": inicio, "end": fin})
    return salida or None


def sl_distance_for(cfg: Optional[Dict[str, Any]] = None,
                    symbol: Optional[str] = None,
                    spec: Any = None) -> Tuple[Optional[float], str]:
    """`(distancia, origen)` de la distancia de SL.

    Orden de resolución (el primero que exista gana):
      1. `sl_distance_by_symbol[SYMBOL]` — la fuente de verdad por mercado.
      2. `sl_distance` — valor global (un único número no puede ser correcto en dos
         mercados a la vez; sirve de respaldo para el símbolo principal).
      3. `sl_default_pips` (LEGACY) x `spec.pip` — puente para un YAML viejo.

    El origen se propaga a la UI para que avise cuando se usa el valor GLOBAL en un
    símbolo que no lo pidió: 0.0012 son 12 pips en EURUSD y una stop de risa en oro, y
    ese error tiene que ser visible, no silencioso.
    """
    by_symbol = _primer_valor(
        cfg, ("sl_distance_by_symbol",), ("execution", "sl_distance_by_symbol")
    )
    if isinstance(by_symbol, dict) and symbol:
        dist = _positive(by_symbol.get(str(symbol).strip().upper()))
        if dist is not None:
            return dist, "symbol"
    dist = _positive(_primer_valor(cfg, ("sl_distance",), ("execution", "sl_distance")))
    if dist is not None:
        return dist, "global"
    legacy = _positive(_primer_valor(cfg, (LEGACY_SL_PIPS_KEY,)))
    pip = getattr(spec, "pip", None) if spec is not None else None
    if legacy is not None and pip:
        try:
            return legacy * float(pip), "legacy_pips"
        except (TypeError, ValueError):
            return None, "none"
    return None, "none"


def _positive(value: Any) -> Optional[float]:
    """El valor como float si es un número > 0; `None` si no o si es <= 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULTS",
    "LEGACY_SL_PIPS_KEY",
    "StrategyConfigError",
    "build_flat",
    "killzones_for",
    "min_score_for",
    "risk_weights_for",
    "sl_distance_for",
    "tp_ratio_for",
]

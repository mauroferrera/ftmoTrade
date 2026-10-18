"""Persistencia: la ÚNICA capa que habla con SQLite.

Todo lo que entra o sale de `trading.db` pasa por aquí. Por debajo,
`database.models` es la fuente de verdad del esquema y `core.clock` la del
tiempo; este módulo solo escribe SQL y traduce filas.

Dos reglas que este módulo respeta:

  1. `DB_PATH` se IMPORTA de `core.paths`, nunca se recalcula aquí. Recalcularlo
     crea un segundo `trading.db` en cuanto el proceso se lanza desde otro
     directorio, y la auditoría "desaparece" sin que nada falle.
  2. La configuración entra por un seam inyectable con default perezoso a
     `settings.strategy_source`. Así importar `store` no arrastra `yaml` ni
     `core.strategy`, y los tests meten una config falsa sin tocar el disco.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from typing import Any, Optional

from core import clock
from core import paths as _paths
from database import models

PROJECT_DIR = _paths.PROJECT_DIR
DB_PATH = _paths.DB_PATH


def now_iso() -> str:
    """Sello de auditoría UTC (+00:00). Único generador de sellos del proyecto."""
    return clock.now_iso()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _safe_json(raw, default):
    """Parsea una columna JSON sin que UNA fila corrupta tumbe el listado.

    `json.loads` LANZA sobre JSON inválido, no devuelve el default. En un
    listado eso no pierde solo la fila: la excepción sube antes de devolver nada
    y se lleva por delante TODAS las demás. `setup_log` tiene cinco columnas
    JSON y la auditoría depende de las cinco.
    """
    if raw is None or raw == "":
        return default
    try:
        out = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default
    if isinstance(default, (dict, list)) and not isinstance(out, type(default)):
        return default
    return out


def _dumps(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


# ============================================================
# Seam de configuración
# ============================================================


class ConfigUnavailable(RuntimeError):
    """No hay `ConfigSource` utilizable (o no hay `strategy.yaml` legible).

    Tipo propio, y no un `RuntimeError` genérico, porque hay dos reacciones
    distintas ante el mismo fallo: `default_trading_config()` debe estallar,
    mientras que la siembra de un rol vacío debe poder seguir.
    """


class ConfigSource:
    """Lo que `store` necesita de la configuración. Lo mínimo, no la API de `strategy`."""

    def get_config(self) -> dict: ...
    def save_config(self, cfg: dict) -> None: ...
    def prop_profiles(self) -> Any: ...
    def watcher_config(self) -> dict: ...


class _LazyStrategyConfig:
    """Default del seam: importa `settings.strategy_source` al PRIMER uso.

    El import va dentro del método a propósito: si estuviera arriba del todo,
    bastaría con que un test importara `store` para que el YAML se cargara y el
    seam no serviría de nada.
    """

    def _mod(self):
        try:
            from settings import strategy_source  # noqa: PLC0415 - import tardío
        except ImportError as exc:  # pragma: no cover
            raise ConfigUnavailable(
                "store necesita la configuración y 'settings.strategy_source' no "
                "se puede importar. Inyecta una con store.set_config_source(...)."
            ) from exc
        return strategy_source

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - se traduce a ConfigUnavailable
            raise ConfigUnavailable(str(exc)) from exc

    def get_config(self) -> dict:
        return self._call(self._mod().get_config)

    def save_config(self, cfg: dict) -> None:
        self._call(self._mod().save, cfg or {})

    def prop_profiles(self) -> Any:
        """Los perfiles de prop firm por cuenta (login MT5 -> `PropProfile`).

        Igual que la config de estrategia, la fuente sabe resolver su propio error:
        un fichero ausente es un registro vacío (`empty_profiles()`), uno mal
        formado lanza `PropProfilesError`. Ese error NO se traduce a
        `ConfigUnavailable`: un tope de DD mal escrito tiene que verse, no
        esconderse tras "aquí no hay config". Solo el import tardío se traduce.
        """
        try:
            from settings import prop_profiles_source  # noqa: PLC0415 - tardío
        except ImportError as exc:  # pragma: no cover
            raise ConfigUnavailable(
                "store necesita los perfiles de prop firm y "
                "'settings.prop_profiles_source' no se puede importar."
            ) from exc
        return prop_profiles_source.load()

    def watcher_config(self) -> dict:
        cfg = self.get_config() or {}
        return dict(cfg.get("watcher") or {})


_CONFIG_SOURCE: ConfigSource = _LazyStrategyConfig()


def set_config_source(source: Optional[ConfigSource]) -> None:
    """Cambia el seam. `None` vuelve al default perezoso (los tests lo usan)."""
    global _CONFIG_SOURCE
    _CONFIG_SOURCE = source if source is not None else _LazyStrategyConfig()


def get_config_source() -> ConfigSource:
    return _CONFIG_SOURCE


def _cfg():
    try:
        return _CONFIG_SOURCE.get_config() or {}
    except ConfigUnavailable:
        return {}


def get_trading_config() -> dict:
    """Config de trading completa (forma plana). Nunca lanza: vacío si no hay."""
    try:
        return _CONFIG_SOURCE.get_config() or {}
    except ConfigUnavailable:
        return {}


def default_trading_config() -> dict:
    """Como `get_config_source().get_config()`, pero estalla si no hay config."""
    return _CONFIG_SOURCE.get_config()


def set_trading_config(cfg: dict) -> None:
    _CONFIG_SOURCE.save_config(cfg)


def get_prop_profiles() -> Any:
    """`core.prop_profiles.PropProfiles` o None si no está configurado."""
    try:
        return _CONFIG_SOURCE.prop_profiles()
    except ConfigUnavailable:
        return None


def get_watcher_config() -> dict:
    try:
        return _CONFIG_SOURCE.watcher_config() or {}
    except ConfigUnavailable:
        return {}


# ============================================================
# Init / seed
# ============================================================


def init_db() -> dict:
    """Crea el esquema y siembra los mínimos imprescindibles. Idempotente."""
    with closing(get_db()) as conn:
        models.init_schema(conn)
        seeded = _seed_defaults(conn)
        conn.commit()
    return seeded


def _seed_defaults(conn: sqlite3.Connection) -> dict:
    """Roles y settings por defecto. Idempotente (nada se pisa)."""
    out = {"roles": 0, "settings": 0}
    stamp = now_iso()
    if not conn.execute("SELECT 1 FROM roles LIMIT 1").fetchone():
        conn.execute(
            "INSERT INTO roles (id, name, system_prompt, allowed_tools, provider,"
            " model, active, updated_at) VALUES (?,?,?,?,?,?,1,?)",
            (
                "copilot",
                "Copiloto FTMO",
                "Eres el copiloto de trading de una cuenta FTMO. Respondes en "
                "español, con datos reales del sistema y sin recomendar operaciones "
                "concretas: describes estructura, riesgo y reglas; la decisión es "
                "siempre del operador.",
                '["account_info","positions_list","price","candles","levels",'
                '"prop_state","daily_risk_state","trade_query","journal_append",'
                '"journal_list","setup_eval","now","chart_annotate"]',
                "gemini",
                "",
                stamp,
            ),
        )
        out["roles"] = 1
    for key, value in (("agent_role_active", "copilot"),):
        if not conn.execute("SELECT 1 FROM agent_settings WHERE key=?", (key,)).fetchone():
            conn.execute(
                "INSERT INTO agent_settings (key, value) VALUES (?,?)", (key, value)
            )
            out["settings"] += 1
    return out


# ============================================================
# Settings (clave/valor genérico)
# ============================================================


def get_settings(table: str = "agent_settings") -> dict:
    with closing(get_db()) as conn:
        if table not in ("agent_settings", "watcher_settings", "prop_state"):
            raise ValueError(f"tabla de settings no soportada: {table}")
        rows = conn.execute(f"SELECT key, value FROM {table}").fetchall()
    return {r["key"]: _safe_json(r["value"], r["value"]) for r in rows}


def set_setting(key: str, value: Any, table: str = "agent_settings") -> None:
    if table not in ("agent_settings", "watcher_settings", "prop_state"):
        raise ValueError(f"tabla de settings no soportada: {table}")
    text = value if isinstance(value, str) else _dumps(value)
    with closing(get_db()) as conn:
        conn.execute(
            f"INSERT INTO {table} (key, value) VALUES (?,?) "
            f"ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, text),
        )
        conn.commit()


def get_setting(key: str, default=None, table: str = "agent_settings"):
    with closing(get_db()) as conn:
        row = conn.execute(
            f"SELECT value FROM {table} WHERE key=?", (key,)
        ).fetchone()
    if row is None:
        return default
    raw = row["value"]
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw


# ============================================================
# Roles / conversaciones / mensajes (agente IA)
# ============================================================


def list_roles() -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM roles ORDER BY active DESC, name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_role(role_id: str) -> Optional[dict]:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM roles WHERE id=?", (role_id,)).fetchone()
    return dict(row) if row else None


def upsert_role(role: dict) -> None:
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO roles (id, name, system_prompt, allowed_tools, provider,"
            " model, active, updated_at) VALUES (:id,:name,:system_prompt,"
            ":allowed_tools,:provider,:model,:active,:updated_at) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            " system_prompt=excluded.system_prompt,"
            " allowed_tools=excluded.allowed_tools, provider=excluded.provider,"
            " model=excluded.model, active=excluded.active,"
            " updated_at=excluded.updated_at",
            {
                **role,
                "updated_at": now_iso(),
                "active": int(role.get("active", 1)),
            },
        )
        conn.commit()


def delete_role(role_id: str) -> bool:
    with closing(get_db()) as conn:
        cur = conn.execute("DELETE FROM roles WHERE id=?", (role_id,))
        conn.commit()
    return cur.rowcount > 0


def create_conversation(title: str, role_id: str, conv_id: Optional[str] = None) -> dict:
    stamp = now_iso()
    conv_id = conv_id or uuid.uuid4().hex[:12]
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO conversations (id, title, role_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (conv_id, title or "Sin título", role_id, stamp, stamp),
        )
        conn.commit()
    return {"id": conv_id, "title": title or "Sin título", "role_id": role_id,
            "created_at": stamp, "updated_at": stamp}


def list_conversations() -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: str) -> Optional[dict]:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
    return dict(row) if row else None


def touch_conversation(conv_id: str) -> None:
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (now_iso(), conv_id),
        )
        conn.commit()


def delete_conversation(conv_id: str) -> bool:
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
        cur = conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        conn.commit()
    return cur.rowcount > 0


def add_message(conversation_id: str, role: str, content: str,
                tool_call_id: Optional[str] = None, name: Optional[str] = None) -> int:
    with closing(get_db()) as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, timestamp,"
            " tool_call_id, name) VALUES (?,?,?,?,?,?)",
            (conversation_id, role, content, now_iso(), tool_call_id, name),
        )
        conn.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (now_iso(), conversation_id),
        )
        conn.commit()
        return cur.lastrowid


def get_messages(conversation_id: str, limit: int = 60) -> list[dict]:
    """Historial visible por el modelo.

    Los mensajes `role="tool"` se FILTRAN a propósito: son traza de debug de la
    ronda de tool-calling. Devolverlos sin tool_calls emparejados deja al modelo
    en el siguiente turno con tool-calls huérfanos, que es una petición inválida
    para la mayoría de proveedores.
    """
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id=?"
            " ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (conversation_id, limit),
        ).fetchall()
    return [
        dict(r) for r in rows if r["role"] in ("user", "assistant", "system")
    ]


# ============================================================
# Trades (espejo del broker)
# ============================================================


def upsert_trades(rows: list[dict]) -> int:
    if not rows:
        return 0
    stamp = now_iso()
    fields = ("ticket", "symbol", "action", "volume", "price_open", "price_close",
              "profit", "time_open", "time_close", "commission", "swap",
              "exit_reason")
    with closing(get_db()) as conn:
        n = 0
        for row in rows:
            values = {k: row.get(k) for k in fields}
            values["synced_at"] = stamp
            conn.execute(
                "INSERT INTO trades (ticket, symbol, action, volume, price_open,"
                " price_close, profit, time_open, time_close, commission, swap,"
                " exit_reason, synced_at) VALUES (:ticket,:symbol,:action,:volume,"
                ":price_open,:price_close,:profit,:time_open,:time_close,"
                ":commission,:swap,:exit_reason,:synced_at) "
                "ON CONFLICT(ticket) DO UPDATE SET symbol=excluded.symbol,"
                " action=excluded.action, volume=excluded.volume,"
                " price_open=excluded.price_open, price_close=excluded.price_close,"
                " profit=excluded.profit, time_open=excluded.time_open,"
                " time_close=excluded.time_close, commission=excluded.commission,"
                " swap=excluded.swap, exit_reason=excluded.exit_reason,"
                " synced_at=excluded.synced_at",
                values,
            )
            n += 1
        conn.commit()
    return n


def list_trades(symbol: Optional[str] = None, limit: int = 500) -> list[dict]:
    sql = "SELECT * FROM trades"
    args: list[Any] = []
    if symbol:
        sql += " WHERE symbol=?"
        args.append(symbol)
    sql += " ORDER BY COALESCE(time_close, '') DESC LIMIT ?"
    args.append(limit)
    with closing(get_db()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def get_trade(ticket: int) -> Optional[dict]:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE ticket=?", (ticket,)
        ).fetchone()
    return dict(row) if row else None


def clear_trades() -> None:
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()


# ============================================================
# Bitácora (journal)
# ============================================================


def add_journal_entry(entry: dict) -> int:
    data = {
        "conversation_id": entry.get("conversation_id"),
        "ticket": entry.get("ticket"),
        "symbol": entry.get("symbol") or "EURUSD",
        "action": entry.get("action"),
        "poi_type": entry.get("poi_type"),
        "liquidity_swept": entry.get("liquidity_swept"),
        "setup_json": _dumps(entry.get("setup_json")),
        "emotion": entry.get("emotion"),
        "plan_compliance": entry.get("plan_compliance"),
        "tags": json.dumps(entry.get("tags") or [], ensure_ascii=False),
        "notes": entry.get("notes"),
        "entry_price": entry.get("entry_price"),
        "sl_price": entry.get("sl_price"),
        "tp_price": entry.get("tp_price"),
        "time_open": entry.get("time_open"),
        "time_close": entry.get("time_close"),
        "timestamp": entry.get("timestamp") or now_iso(),
    }
    with closing(get_db()) as conn:
        cur = conn.execute(
            "INSERT INTO journal (conversation_id, ticket, symbol, action,"
            " poi_type, liquidity_swept, setup_json, emotion, plan_compliance,"
            " tags, notes, entry_price, sl_price, tp_price, time_open, time_close,"
            " timestamp) VALUES (:conversation_id,:ticket,:symbol,:action,"
            ":poi_type,:liquidity_swept,:setup_json,:emotion,:plan_compliance,"
            ":tags,:notes,:entry_price,:sl_price,:tp_price,:time_open,"
            ":time_close,:timestamp)",
            data,
        )
        conn.commit()
        return cur.lastrowid


def get_journal_entry(jid: int) -> Optional[dict]:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM journal WHERE id=?", (jid,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["setup_json"] = _safe_json(out.get("setup_json"), {})
    out["tags"] = _safe_json(out.get("tags"), [])
    return out


def list_journal(limit: int = 200, symbol: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM journal"
    args: list[Any] = []
    if symbol:
        sql += " WHERE symbol=?"
        args.append(symbol)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    args.append(limit)
    with closing(get_db()) as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["setup_json"] = _safe_json(item.get("setup_json"), {})
        item["tags"] = _safe_json(item.get("tags"), [])
        out.append(item)
    return out


def update_journal_entry(jid: int, entry: dict) -> bool:
    """PUT, no PATCH: reemplaza la fila entera con lo que llega."""
    current = get_journal_entry(jid)
    if not current:
        return False
    merged = {**current, **entry, "id": jid}
    fields = ("conversation_id", "ticket", "symbol", "action", "poi_type",
              "liquidity_swept", "emotion", "plan_compliance", "notes",
              "entry_price", "sl_price", "tp_price", "time_open", "time_close",
              "timestamp")
    with closing(get_db()) as conn:
        sets = ", ".join(f"{f}=?" for f in fields)
        args = [merged.get(f) for f in fields]
        args += [_dumps(merged.get("setup_json")),
                 json.dumps(merged.get("tags") or [], ensure_ascii=False), jid]
        conn.execute(
            f"UPDATE journal SET {sets}, setup_json=?, tags=? WHERE id=?", args
        )
        conn.commit()
    return True


def delete_journal_entry(jid: int) -> bool:
    with closing(get_db()) as conn:
        cur = conn.execute("DELETE FROM journal WHERE id=?", (jid,))
        conn.commit()
    return cur.rowcount > 0


# ============================================================
# Setup log (post-mortem) y estado del watcher
# ============================================================


def log_setup(row: dict) -> int:
    with closing(get_db()) as conn:
        cur = conn.execute(
            "INSERT INTO setup_log (symbol, timeframe, direction, verdict, score,"
            " breakdown_json, entry, sl, target, invalidate_level, validated,"
            " reject_reasons, risk_state_json, trade_result_json, context_json,"
            " snapshot_age_s, source, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.get("symbol"), row.get("timeframe") or "M15",
                row.get("direction") or "", row.get("verdict") or "",
                float(row.get("score") or 0.0),
                _dumps(row.get("breakdown_json")),
                row.get("entry"), row.get("sl"), row.get("target"),
                row.get("invalidate_level"), row.get("validated"),
                json.dumps(row.get("reject_reasons") or [], ensure_ascii=False),
                _dumps(row.get("risk_state_json")),
                _dumps(row.get("trade_result_json")),
                _dumps(row.get("context_json")),
                row.get("snapshot_age_s"), row.get("source") or "",
                row.get("timestamp") or now_iso(),
            ),
        )
        conn.commit()
        return cur.lastrowid


def update_trade_result(setup_id: int, result: dict) -> None:
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE setup_log SET trade_result_json=? WHERE id=?",
            (_dumps(result), setup_id),
        )
        conn.commit()


def list_setup_log(limit: int = 200, verdict: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM setup_log"
    args: list[Any] = []
    if verdict:
        sql += " WHERE verdict=?"
        args.append(verdict)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with closing(get_db()) as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        for col in ("breakdown_json", "risk_state_json", "trade_result_json",
                    "context_json"):
            item[col] = _safe_json(item.get(col), {})
        item["reject_reasons"] = _safe_json(item.get("reject_reasons"), [])
        out.append(item)
    return out


def get_setup_row(setup_id: int) -> Optional[dict]:
    rows = [r for r in list_setup_log(limit=10_000) if r["id"] == setup_id]
    return rows[0] if rows else None


def get_setup_state(symbol: str, timeframe: str) -> Optional[dict]:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM setup_state WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
    return dict(row) if row else None


def list_setup_states() -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM setup_state ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def new_setup_state(symbol: str, timeframe: str, row: dict) -> None:
    stamp = now_iso()
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO setup_state (symbol, timeframe, direction, score, verdict,"
            " entry, sl, target, invalidate_level, status, notified, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?) "
            "ON CONFLICT(symbol, timeframe) DO UPDATE SET direction=excluded.direction,"
            " score=excluded.score, verdict=excluded.verdict, entry=excluded.entry,"
            " sl=excluded.sl, target=excluded.target,"
            " invalidate_level=excluded.invalidate_level, status=excluded.status,"
            " notified=0, updated_at=excluded.updated_at",
            (symbol, timeframe, row.get("direction"), row.get("score"),
             row.get("verdict"), row.get("entry"), row.get("sl"),
             row.get("target"), row.get("invalidate_level"),
             row.get("status") or "active", stamp, stamp),
        )
        conn.commit()


def update_setup_state(symbol: str, timeframe: str, patch: dict) -> None:
    allowed = {"direction", "score", "verdict", "entry", "sl", "target",
               "invalidate_level", "status", "notified"}
    items = [(k, v) for k, v in patch.items() if k in allowed]
    if not items:
        return
    sets = ", ".join(f"{k}=?" for k, _ in items)
    with closing(get_db()) as conn:
        conn.execute(
            f"UPDATE setup_state SET {sets}, updated_at=? WHERE symbol=? AND timeframe=?",
            [v for _, v in items] + [now_iso(), symbol, timeframe],
        )
        conn.commit()


# ============================================================
# Marcas post-trade (trade_marks)
# ============================================================


def save_trade_mark(mark: dict) -> None:
    data = {
        "ticket": mark["ticket"],
        "symbol": mark.get("symbol") or "",
        "direction": mark.get("direction") or "",
        "setup_id": mark.get("setup_id"),
        "volume": mark.get("volume"),
        "price_open": mark.get("price_open"),
        "price_close": mark.get("price_close"),
        "sl": mark.get("sl"),
        "tp": mark.get("tp"),
        "profit": mark.get("profit"),
        "r_multiple": mark.get("r_multiple"),
        "exit_reason": mark.get("exit_reason"),
        "exit_quality": mark.get("exit_quality") or "",
        "duration_s": mark.get("duration_s"),
        "capture_path": mark.get("capture_path"),
        "outcome_json": _dumps(mark.get("outcome_json")),
        "context_json": _dumps(mark.get("context_json")),
        "source": mark.get("source") or "",
        "created_at": mark.get("created_at") or now_iso(),
    }
    cols = ", ".join(data.keys())
    ph = ", ".join(f":{k}" for k in data)
    updates = ", ".join(
        f"{k}=excluded.{k}" for k in data if k not in ("ticket", "created_at")
    )
    with closing(get_db()) as conn:
        conn.execute(
            f"INSERT INTO trade_marks ({cols}) VALUES ({ph}) "
            f"ON CONFLICT(ticket) DO UPDATE SET {updates}",
            data,
        )
        conn.commit()


def get_trade_mark(ticket: int) -> Optional[dict]:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM trade_marks WHERE ticket=?", (ticket,)
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["outcome_json"] = _safe_json(out.get("outcome_json"), {})
    out["context_json"] = _safe_json(out.get("context_json"), {})
    return out


def list_trade_marks(limit: int = 500) -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM trade_marks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["outcome_json"] = _safe_json(item.get("outcome_json"), {})
        item["context_json"] = _safe_json(item.get("context_json"), {})
        out.append(item)
    return out


# ============================================================
# Dibujos del gráfico
# ============================================================


def get_drawings(symbol: str, timeframe: str) -> list[dict]:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT drawings_json FROM chart_drawings WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()
    return _safe_json(row["drawings_json"], []) if row else []


def save_drawings(symbol: str, timeframe: str, drawings: list[dict]) -> None:
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO chart_drawings (symbol, timeframe, drawings_json, updated_at)"
            " VALUES (?,?,?,?) ON CONFLICT(symbol, timeframe) DO UPDATE SET"
            " drawings_json=excluded.drawings_json, updated_at=excluded.updated_at",
            (symbol, timeframe, json.dumps(drawings or [], ensure_ascii=False),
             now_iso()),
        )
        conn.commit()


def delete_drawings(symbol: str, timeframe: str) -> bool:
    with closing(get_db()) as conn:
        cur = conn.execute(
            "DELETE FROM chart_drawings WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        )
        conn.commit()
    return cur.rowcount > 0


# ============================================================
# Visor de base de datos (solo lectura)
# ============================================================


def list_db_tables() -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        out = []
        for r in rows:
            name = r["name"]
            count = conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"]
            out.append({"name": name, "rows": count})
    return out


def query_db_table(table: str, limit: int = 100, offset: int = 0,
                   search: str = "") -> list[dict]:
    if table not in models.TABLE_NAMES:
        raise ValueError(f"tabla desconocida: {table}")
    with closing(get_db()) as conn:
        if search:
            cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            where = " WHERE " + " OR ".join(
                f'CAST("{c}" AS TEXT) LIKE ?' for c in cols
            )
            args: list[Any] = [f"%{search}%"] * len(cols)
        else:
            where, args = "", []
        sql = (
            f'SELECT * FROM "{table}"{where} ORDER BY rowid DESC LIMIT ? OFFSET ?'
        )
        rows = conn.execute(sql, args + [limit, offset]).fetchall()
    return [dict(r) for r in rows]

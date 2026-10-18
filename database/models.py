"""Esquema de la base de datos: la fuente de verdad, en un solo sitio.

Separación de responsabilidades:

  - `SCHEMA`: los `CREATE TABLE`/`CREATE INDEX` de la base actual.
  - `INDEXES`: los `CREATE INDEX` explícitos (SQLite crea los suyos con nombres
    `sqlite_autoindex_*` y no se pueden pedir).
  - `MIGRATIONS`: columnas añadidas con el tiempo. Idempotentes (`if not in`
    sobre `PRAGMA table_info`), seguras de correr contra una base ya viva.
  - `init_schema(conn)`: aplica `SCHEMA` + `INDEXES` + `MIGRATIONS`.

Las filas iniciales (roles, settings) NO viven aquí: necesitan la config del
agente, que es un seam perezoso de `store.py`.

`schema.sql` se genera desde aquí con `python -m database.models > database/schema.sql`
y `tests/unit/test_schema.py` falla si se desincronizan. Sin ese test, el fichero
.sql sería una segunda fuente de verdad que nadie actualiza.

NOTA SOBRE LOS `NULL`: una columna añadida por migración queda `NULL` en las
filas viejas. Es deliberado: `NULL` significa "no medido", `0` significaría
"medido y valía cero", y para el post-mortem no es lo mismo. Rellenar con ceros
fabricaría evidencia.

Tablas (12): recorte deliberado del proyecto de referencia. Fuera `cot_reports`
(sin COT), `chart_alerts` (alertas de precio) y `trade_excursions` (MFE/MAE
muestreado), pendientes de decidir. Ver `docs/ALCANCE.md`.
"""

from __future__ import annotations

import sqlite3

# ============================================================
# Tablas
# ============================================================

SCHEMA: tuple[str, ...] = (
    # --- Agente IA: roles, conversaciones, mensajes ---
    """
    CREATE TABLE IF NOT EXISTS roles (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        system_prompt TEXT NOT NULL,
        allowed_tools TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'auto',
        model TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        role_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        tool_call_id TEXT,
        name TEXT
    )
    """,
    # --- Espejo de operaciones del broker ---
    # Sin hora de apertura no hay duración; sin comisión/swap el profit neto no
    # es el neto; sin motivo de cierre no se separa "lo paró el SL" de "lo cerró
    # el operador a mano", que es justo la diferencia entre un stop que funciona
    # y uno que se toca.
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket INTEGER NOT NULL UNIQUE,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL,
        volume REAL,
        price_open REAL,
        price_close REAL,
        profit REAL,
        time_open REAL,
        time_close TEXT,
        commission REAL,
        swap REAL,
        exit_reason INTEGER,
        synced_at TEXT NOT NULL
    )
    """,
    # --- Bitácora / journal ---
    """
    CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT,
        ticket TEXT,
        symbol TEXT NOT NULL DEFAULT 'EURUSD',
        action TEXT,
        poi_type TEXT,
        liquidity_swept TEXT,
        setup_json TEXT NOT NULL DEFAULT '{}',
        emotion TEXT,
        plan_compliance INTEGER,
        tags TEXT NOT NULL DEFAULT '[]',
        notes TEXT,
        entry_price REAL,
        sl_price REAL,
        tp_price REAL,
        time_open TEXT,
        time_close TEXT,
        timestamp TEXT NOT NULL
    )
    """,
    # --- Ajustes del agente (clave/valor) ---
    """
    CREATE TABLE IF NOT EXISTS agent_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # --- Registro append-only de setups (post-mortem) ---
    # Cada fila es UNA decisión: entró, salió, o fue rechazada y por qué. Es la
    # materia prima para medir después si el sistema hace lo que dice.
    """
    CREATE TABLE IF NOT EXISTS setup_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        direction TEXT NOT NULL,
        verdict TEXT NOT NULL,
        score REAL NOT NULL,
        breakdown_json TEXT NOT NULL DEFAULT '{}',
        entry REAL,
        sl REAL,
        target REAL,
        invalidate_level REAL,
        validated INTEGER,
        reject_reasons TEXT NOT NULL DEFAULT '[]',
        risk_state_json TEXT NOT NULL DEFAULT '{}',
        trade_result_json TEXT NOT NULL DEFAULT '{}',
        context_json TEXT NOT NULL DEFAULT '{}',
        snapshot_age_s REAL,
        source TEXT NOT NULL DEFAULT '',
        timestamp TEXT NOT NULL
    )
    """,
    # --- Marcas post-trade del copiloto ---
    # Tabla DEDICADA, separada de `journal`: la bitácora alimenta contadores de
    # disciplina, y volcar aquí cada cierre los inflaría. Una marca es el
    # desenlace de UNA posición cerrada (R múltiplo, pnl, motivo, duración) más
    # la ruta de su captura. `ticket` UNIQUE = idempotente por operación.
    """
    CREATE TABLE IF NOT EXISTS trade_marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket INTEGER NOT NULL UNIQUE,
        symbol TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL DEFAULT '',
        setup_id INTEGER,
        volume REAL,
        price_open REAL,
        price_close REAL,
        sl REAL,
        tp REAL,
        profit REAL,
        r_multiple REAL,
        exit_reason INTEGER,
        exit_quality TEXT NOT NULL DEFAULT '',
        duration_s REAL,
        capture_path TEXT,
        outcome_json TEXT NOT NULL DEFAULT '{}',
        context_json TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    # --- Estado del watcher: dedup por symbol/timeframe ---
    """
    CREATE TABLE IF NOT EXISTS setup_state (
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        direction TEXT,
        score REAL,
        verdict TEXT,
        entry REAL,
        sl REAL,
        target REAL,
        invalidate_level REAL,
        status TEXT NOT NULL DEFAULT 'active',
        notified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe)
    )
    """,
    # Overrides del watcher persistidos.
    """
    CREATE TABLE IF NOT EXISTS watcher_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # Estado del gate FTMO (balance de partida del día, pico, contador de días).
    # Filas JSON clave->valor para no tocar el esquema por cada métrica nueva.
    """
    CREATE TABLE IF NOT EXISTS prop_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # Dibujos manuales del gráfico, por symbol/timeframe. Cada item lleva
    # origin: "user" | "agent".
    """
    CREATE TABLE IF NOT EXISTS chart_drawings (
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        drawings_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe)
    )
    """,
)


INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_setup_log_time ON setup_log(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_setup_log_verdict ON setup_log(verdict);",
    "CREATE INDEX IF NOT EXISTS idx_setup_log_age ON setup_log(snapshot_age_s);",
    "CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);",
    "CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time_close);",
    "CREATE INDEX IF NOT EXISTS idx_journal_time ON journal(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_trade_marks_time ON trade_marks(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_trade_marks_symbol ON trade_marks(symbol);",
)


# ============================================================
# Migraciones de esquema (idempotentes)
# ============================================================
#
# Cada entrada es (tabla, columna, definición del ALTER). Todas se comprueban
# antes de aplicarse: correrlas sobre una base ya actualizada no hace nada, y
# sobre una nueva tampoco rompe.
#
# Se conservan aunque la base nazca con la columna ya declarada arriba: una base
# en producción se crea desde un `init_db()` de hace meses, no desde este
# fichero, y quitar la migración significaría que esa base no levanta.

MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("setup_log", "context_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("setup_log", "snapshot_age_s", "REAL"),
    ("setup_log", "source", "TEXT NOT NULL DEFAULT ''"),
)


TABLE_NAMES: tuple[str, ...] = (
    "agent_settings",
    "chart_drawings",
    "conversations",
    "journal",
    "messages",
    "prop_state",
    "roles",
    "setup_log",
    "setup_state",
    "trade_marks",
    "trades",
    "watcher_settings",
)


# ============================================================
# Aplicación del esquema
# ============================================================


def existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Columnas actuales de `table` (vacío si la tabla no existe)."""
    return {r["name"] for r in conn.execute('PRAGMA table_info("%s")' % table)}


def _known_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Aplica las columnas que falten. Devuelve las que realmente añadió.

    Salta las tablas que no existen. En `init_schema` es irrelevante (el DDL va
    primero), pero `apply_migrations` es pública y se usa sola: contra una base a
    medio migrar, o una donde alguien dropeó una tabla, un `ALTER TABLE`
    inexistente aborta el arranque entero.
    """
    added: list[str] = []
    for table, column, decl in MIGRATIONS:
        present = existing_columns(conn, table)
        if not present and table not in _known_tables(conn):
            continue
        if column in present:
            continue
        conn.execute('ALTER TABLE "%s" ADD COLUMN "%s" %s' % (table, column, decl))
        added.append("%s.%s" % (table, column))
    return added


def init_schema(conn: sqlite3.Connection) -> list[str]:
    """Crea el esquema completo y aplica las migraciones pendientes."""
    for statement in SCHEMA:
        conn.execute(statement)
    for statement in INDEXES:
        conn.execute(statement)
    return apply_migrations(conn)


def render_sql() -> str:
    """El esquema como `schema.sql` legible.

    Se genera desde las MISMAS tuplas que ejecuta `init_schema`, no se escribe a
    mano: un `.sql` mantenido a mano es una segunda fuente de verdad, y
    `tests/unit/test_schema.py` compara ambos para que no se separen.
    """
    out = [
        "-- GENERADO, no editar a mano:",
        "--   python -m database.models > database/schema.sql",
        "-- Fuente de verdad: database/models.py (SCHEMA, INDEXES, MIGRATIONS).",
        "-- `tests/unit/test_schema.py::test_schema_sql_al_dia` falla si se separan.",
        "",
    ]
    for statement in SCHEMA:
        out.append("%s;" % statement.strip())
        out.append("")
    out.append("-- Índices")
    for statement in INDEXES:
        out.append(statement)
        out.append("")
    out.append("-- Migraciones idempotentes (ALTER TABLE ... ADD COLUMN)")
    out.append("-- Se aplican en init_schema(); aquí solo como referencia.")
    for table, column, decl in MIGRATIONS:
        out.append("-- %-14s ADD COLUMN %-14s %s" % (table, column, decl))
    out.append("")
    return "\n".join(out)


def write_sql(path: "os.PathLike[str] | str") -> None:
    """Escribe `schema.sql` como UTF-8 con finales de línea LF.

    Existe como función y no como `python -m database.models > schema.sql` por un
    motivo concreto: la redirección `>` de PowerShell 5.1 escribe **UTF-16LE**.
    Generado así, `schema.sql` era un fichero que `sqlite3` no podía leer.
    """
    text = render_sql()
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


if __name__ == "__main__":  # pragma: no cover - generador de schema.sql
    import os.path
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "schema.sql"
    )
    write_sql(target)
    print("escrito %s" % target, file=sys.stderr)

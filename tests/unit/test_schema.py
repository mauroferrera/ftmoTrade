"""El esquema de `database/models.py` es el de PRODUCCION, y no una aproximacion.

El esquema es una tupla de texto, y eso habilita las tres comprobaciones:

  1. Coincide con el snapshot congelado en `tests/fixtures/schema_snapshot.json`.
  2. `database/schema.sql` no se ha desincronizado de las tuplas que lo generaron.
  3. Las migraciones son idempotentes y solo anaden lo que falta.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from database import models

EXPECTED_TABLES = 12

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "schema_snapshot.json"
SCHEMA_SQL = Path(__file__).resolve().parents[2] / "database" / "schema.sql"


@pytest.fixture()
def fresh_conn(tmp_path):
    """Base nueva, en disco temporal, con el esquema recien creado."""
    conn = sqlite3.connect(tmp_path / "nueva.db")
    conn.row_factory = sqlite3.Row
    models.init_schema(conn)
    yield conn
    conn.close()


def _snapshot_of(conn):
    out = {}
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for t in tables:
        cols = {}
        for r in conn.execute('PRAGMA table_info("%s")' % t):
            cols[r[1]] = {
                "type": r[2].upper(),
                "notnull": r[3],
                "default": r[4],
                "pk": r[5],
            }
        idx = sorted(
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND name NOT LIKE 'sqlite_%'",
                (t,),
            )
        )
        out[t] = {"columns": cols, "indexes": idx}
    return out


class TestEsquemaDeProduccion:

    def test_coincide_con_el_snapshot_congelado(self, fresh_conn):
        """Tabla por tabla, columna por columna, contra el snapshot."""
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["tables"]
        got = _snapshot_of(fresh_conn)

        assert sorted(got) == sorted(expected), (
            "tablas distintas: faltan %s / sobran %s"
            % (sorted(set(expected) - set(got)), sorted(set(got) - set(expected)))
        )

        for table in sorted(expected):
            want_cols = expected[table]["columns"]
            got_cols = got[table]["columns"]
            assert sorted(got_cols) == sorted(want_cols), (
                "%s: faltan %s / sobran %s"
                % (
                    table,
                    sorted(set(want_cols) - set(got_cols)),
                    sorted(set(got_cols) - set(want_cols)),
                )
            )
            for col in sorted(want_cols):
                assert got_cols[col] == want_cols[col], "%s.%s" % (table, col)
            assert got[table]["indexes"] == expected[table]["indexes"], table

    def test_tiene_las_doce_tablas_esperadas(self, fresh_conn):
        got = [
            r[0]
            for r in fresh_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        assert got == list(models.TABLE_NAMES)
        assert len(got) == EXPECTED_TABLES

    def test_tabla_setup_log_conserva_las_columnas_del_post_mortem(self, fresh_conn):
        """Las que hacen posible el post-mortem; si desaparecen, se pierde el audit."""
        cols = models.existing_columns(fresh_conn, "setup_log")
        for required in (
            "breakdown_json",
            "reject_reasons",
            "risk_state_json",
            "trade_result_json",
            "context_json",
            "snapshot_age_s",
            "source",
            "validated",
            "invalidate_level",
        ):
            assert required in cols, required

    def test_trades_tiene_lo_que_distingue_sl_de_cierre_manual(self, fresh_conn):
        cols = models.existing_columns(fresh_conn, "trades")
        for required in ("time_open", "commission", "swap", "exit_reason"):
            assert required in cols, required


class TestSchemaSqlAlDia:
    """`schema.sql` es un artefacto generado. Sin este test seria una segunda
    fuente de verdad."""

    def test_el_fichero_existe_y_se_puede_generar(self):
        assert SCHEMA_SQL.exists(), "falta database/schema.sql"
        assert models.render_sql() == SCHEMA_SQL.read_text(encoding="utf-8"), (
            "database/schema.sql no coincide con models.py. Regenerar con:\n"
            "    python -m database.models"
        )

    def test_el_sql_generado_es_ejecutable(self, tmp_path):
        """Un .sql que no se puede ejecutar es documentación, no esquema."""
        conn = sqlite3.connect(tmp_path / "desde_sql.db")
        conn.row_factory = sqlite3.Row
        for stmt in models.SCHEMA:
            conn.execute(stmt)
        for stmt in models.INDEXES:
            conn.execute(stmt)
        models.apply_migrations(conn)
        conn.commit()
        assert len(
            [
                r
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
        ) == EXPECTED_TABLES
        conn.close()

    def test_cada_tabla_del_snapshot_aparece_en_el_sql(self):
        sql = SCHEMA_SQL.read_text(encoding="utf-8")
        for table in models.TABLE_NAMES:
            assert "CREATE TABLE IF NOT EXISTS %s (" % table in sql, table


class TestMigraciones:
    """Idempotentes y aditivas: es lo unico que permite correrlas en caliente."""

    def test_sobre_base_nueva_no_anade_nada(self, fresh_conn):
        """La base ya nace con todo: una migracion que anade algo esta obsoleta."""
        assert models.apply_migrations(fresh_conn) == []

    def test_correrlas_dos_veces_no_cambia_nada(self, fresh_conn):
        assert models.apply_migrations(fresh_conn) == []
        assert models.apply_migrations(fresh_conn) == []

    def test_anade_lo_que_falta_en_una_base_vieja(self, tmp_path):
        """Simula una base sin las columnas del post-mortem: las migraciones las
        anaden. Es el caso que las migraciones existen para cubrir."""
        conn = sqlite3.connect(tmp_path / "vieja.db")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE setup_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT 'M15', "
            "direction TEXT NOT NULL, verdict TEXT NOT NULL, score REAL NOT NULL, "
            "breakdown_json TEXT NOT NULL DEFAULT '{}', entry REAL, sl REAL, "
            "target REAL, invalidate_level REAL, validated INTEGER, "
            "reject_reasons TEXT NOT NULL DEFAULT '[]', "
            "risk_state_json TEXT NOT NULL DEFAULT '{}', "
            "trade_result_json TEXT NOT NULL DEFAULT '{}', timestamp TEXT NOT NULL)"
        )
        added = models.apply_migrations(conn)
        conn.commit()

        assert "setup_log.context_json" in added
        assert "setup_log.snapshot_age_s" in added
        assert "setup_log.source" in added
        assert "context_json" in models.existing_columns(conn, "setup_log")
        # Y no duplica lo que ya estaba.
        assert models.apply_migrations(conn) == []
        conn.close()

    def test_las_filas_viejas_no_se_rellenan_con_inventos(self, tmp_path):
        """NULL = "no medido". Rellenarlo de ceros fabricaría evidencia."""
        conn = sqlite3.connect(tmp_path / "vieja.db")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE setup_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT 'M15', "
            "direction TEXT NOT NULL, verdict TEXT NOT NULL, score REAL NOT NULL, "
            "breakdown_json TEXT NOT NULL DEFAULT '{}', entry REAL, sl REAL, "
            "target REAL, invalidate_level REAL, validated INTEGER, "
            "reject_reasons TEXT NOT NULL DEFAULT '[]', "
            "risk_state_json TEXT NOT NULL DEFAULT '{}', "
            "trade_result_json TEXT NOT NULL DEFAULT '{}', timestamp TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO setup_log (symbol, direction, verdict, score, timestamp) "
            "VALUES ('EURUSD','BUY','ok',70,'t')"
        )
        models.apply_migrations(conn)
        row = conn.execute("SELECT * FROM setup_log").fetchone()

        # snapshot_age_s no tiene default: queda NULL ("no medido"), no 0.
        assert row["snapshot_age_s"] is None
        # source sí tiene default '' : "de fuente desconocida", que es honesto.
        assert row["source"] == ""
        conn.close()

    def test_init_schema_es_idempotente(self, tmp_path):
        """init_db() se llama en cada arranque: no puede fallar la segunda vez."""
        conn = sqlite3.connect(tmp_path / "dos.db")
        conn.row_factory = sqlite3.Row
        models.init_schema(conn)
        models.init_schema(conn)
        conn.commit()
        assert len(
            [
                r
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
        ) == EXPECTED_TABLES
        conn.close()

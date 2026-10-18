"""CRUD del `store` contra una base temporal.

`store` es la ÚNICA capa que habla con SQLite, así que aquí se ejercita lo que
M0 necesita: siembra de roles, conversaciones/mensajes, settings, bitácora y
dibujos. La base siempre es `tmp_store` (nunca la real: lo vigila el canario).
"""

from __future__ import annotations


class TestSiembra:

    def test_init_db_siembra_el_rol_copiloto(self, tmp_store):
        roles = tmp_store.list_roles()
        assert any(r["id"] == "copilot" and r["active"] == 1 for r in roles)

    def test_init_db_es_idempotente(self, tmp_store):
        first = tmp_store.init_db()
        second = tmp_store.init_db()
        assert first["roles"] == 0 or first["roles"] == 1
        assert second == {"roles": 0, "settings": 0}
        assert len(tmp_store.list_roles()) == 1

    def test_list_db_tables_cuenta_las_doce(self, tmp_store):
        tablas = {t["name"] for t in tmp_store.list_db_tables()}
        assert len(tablas) == 12
        assert "setup_log" in tablas and "trades" in tablas


class TestConversaciones:

    def test_ciclo_completo(self, tmp_store):
        conv = tmp_store.create_conversation("mi chat", "copilot")
        assert tmp_store.get_conversation(conv["id"])["title"] == "mi chat"
        tmp_store.add_message(conv["id"], "user", "hola")
        tmp_store.add_message(conv["id"], "assistant", "buenas")
        msgs = tmp_store.get_messages(conv["id"])
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert tmp_store.delete_conversation(conv["id"]) is True
        assert tmp_store.get_conversation(conv["id"]) is None

    def test_get_messages_filtra_los_tool(self, tmp_store):
        """Los mensajes `tool` no van al modelo: son traza de la ronda."""
        conv = tmp_store.create_conversation("c", "copilot")
        tmp_store.add_message(conv["id"], "user", "u")
        tmp_store.add_message(conv["id"], "tool", "{}", tool_call_id="x", name="price")
        tmp_store.add_message(conv["id"], "assistant", "a")
        roles = [m["role"] for m in tmp_store.get_messages(conv["id"])]
        assert roles == ["user", "assistant"]


class TestSettings:

    def test_roundtrip_de_valor_json(self, tmp_store):
        tmp_store.set_setting("k", {"a": 1})
        assert tmp_store.get_setting("k") == {"a": 1}

    def test_default_cuando_no_existe(self, tmp_store):
        assert tmp_store.get_setting("nada", default=7) == 7

    def test_valor_texto_plano(self, tmp_store):
        tmp_store.set_setting("s", "hola")
        assert tmp_store.get_setting("s") == "hola"


class TestJournal:

    def test_ciclo_completo(self, tmp_store):
        jid = tmp_store.add_journal_entry({
            "symbol": "EURUSD", "action": "BUY", "notes": "setup limpio",
            "tags": ["smc"], "setup_json": {"score": 70},
        })
        entry = tmp_store.get_journal_entry(jid)
        assert entry["symbol"] == "EURUSD"
        assert entry["tags"] == ["smc"]
        assert entry["setup_json"] == {"score": 70}

        tmp_store.update_journal_entry(jid, {"notes": "corregido"})
        assert tmp_store.get_journal_entry(jid)["notes"] == "corregido"

        assert tmp_store.delete_journal_entry(jid) is True
        assert tmp_store.get_journal_entry(jid) is None

    def test_list_journal_parsea_todo(self, tmp_store):
        tmp_store.add_journal_entry({"symbol": "XAUUSD", "action": "SELL"})
        rows = tmp_store.list_journal()
        assert rows and rows[0]["tags"] == [] and rows[0]["setup_json"] == {}


class TestDrawings:

    def test_roundtrip_por_symbol_timeframe(self, tmp_store):
        tmp_store.save_drawings("EURUSD", "M15", [{"type": "hline", "price": 1.1}])
        assert tmp_store.get_drawings("EURUSD", "M15") == [
            {"type": "hline", "price": 1.1}]
        # Otro symbol no ve lo del primero.
        assert tmp_store.get_drawings("XAUUSD", "M15") == []
        assert tmp_store.delete_drawings("EURUSD", "M15") is True

    def test_guardar_reemplaza(self, tmp_store):
        tmp_store.save_drawings("XAUUSD", "M15", [{"a": 1}])
        tmp_store.save_drawings("XAUUSD", "M15", [{"b": 2}])
        assert tmp_store.get_drawings("XAUUSD", "M15") == [{"b": 2}]

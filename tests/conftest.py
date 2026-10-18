"""Fixtures compartidas y el guardián que protege la `trading.db` real.

Dos cosas viven aquí y las dos son deliberadas:

- **Canario de solo lectura** (`_real_db_readonly_canary`, autouse). La
  `trading.db` es el activo más valioso del proyecto: el post-mortem, los
  win-rate por componente y la calibración salen de ella. Un test que la abra
  por accidente no falla: escribe basura que parece correcta. Se mide su huella
  (sha256 + tamaño, NO la mtime: OneDrive la reescribe al sincronizar) antes y
  después de la sesión, y la suite falla si cambió.

- **`tmp_store`**: un `store` apuntando a una base temporal. La redirección es
  monkeypatch sobre `store.DB_PATH` (que `get_db` lee en cada llamada), así que
  ningún test de persistencia toca el fichero real.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core import paths as _paths


def _db_fingerprint(path: Path) -> tuple:
    if not path.exists():
        return ("missing",)
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return (path.stat().st_size, h.hexdigest())


@pytest.fixture(scope="session", autouse=True)
def _real_db_readonly_canary():
    real = Path(_paths.DB_PATH)
    before = _db_fingerprint(real)
    wal_before = Path(str(real) + "-wal").exists()
    yield
    after = _db_fingerprint(real)
    wal_after = Path(str(real) + "-wal").exists()
    if before != after:
        raise AssertionError(
            "The test suite MODIFIED the real trading.db (%s):\n"
            "  before:   %s\n  after:    %s\n"
            "Any test that needs to write must redirect store.DB_PATH to a "
            "tmp_path." % (real, before, after)
        )
    if wal_after and not wal_before:
        raise AssertionError(
            "The suite left the real trading.db open (%s-wal): someone opened it "
            "without closing it. Isolate that test or its DB_PATH." % real
        )


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """Un `store` inicializado contra una base temporal. Nunca toca la real."""
    from database import store

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "trading.db"))
    store.init_db()
    return store

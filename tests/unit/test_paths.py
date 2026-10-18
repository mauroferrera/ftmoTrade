"""Invariantes de las rutas del proyecto.

`core/paths.py` existe para que mover un fichero no cambie dónde acaba el
`trading.db`. Un fallo aquí NO da error: da silencio. Si el `.db` se busca en el
sitio equivocado, la app simplemente no ofrece replay histórico y no se queja
nadie. Por eso no basta con probar que las rutas existen: hay que probar que
están donde dicen estar.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import paths

IN_REPO_ABSOLUTE = (
    "DB_PATH",
    "STRATEGY_PATH",
    "SYMBOLS_PATH",
    "PROP_PROFILES_PATH",
    "TEST_FIXTURES_DIR",
)

EXTERNAL_DATA = (
    "DATA_ROOT",
    "DATA_DIR",
    "COPILOT_DIR",
    "COPILOT_MARKS_DIR",
)


class TestPathsAreAbsolute:

    def test_project_dir_is_the_repo_root(self):
        """La raíz se deriva de core/, un nivel arriba, no del CWD."""
        assert os.path.isabs(paths.PROJECT_DIR)
        assert paths.PROJECT_DIR == str(Path(__file__).resolve().parent.parent.parent)

    @pytest.mark.parametrize("name", IN_REPO_ABSOLUTE)
    def test_toda_ruta_es_absoluta_y_dentro_del_repo(self, name):
        value = getattr(paths, name)
        assert os.path.isabs(value), f"{name} es relativa: dependería del CWD"
        assert value.startswith(paths.PROJECT_DIR), (
            f"{name} se sale de la raíz del proyecto: {value}"
        )

    def test_db_vive_en_database_no_en_la_raiz(self):
        """El trading.db real vive en database/, no suelto en la raíz."""
        assert paths.DB_PATH == os.path.join(
            paths.PROJECT_DIR, "database", "trading.db")
        assert os.path.dirname(paths.DB_PATH) == paths.DATABASE_DIR

    def test_strategy_vive_en_config(self):
        """strategy.yaml es la fuente de verdad de producción y va en config/."""
        assert paths.STRATEGY_PATH == os.path.join(
            paths.PROJECT_DIR, "config", "strategy.yaml")
        assert os.path.dirname(paths.STRATEGY_PATH) == paths.CONFIG_DIR

    def test_no_hay_db_suelto_en_la_raiz(self):
        """Si alguien crea un trading.db en la raíz, este test lo delata."""
        assert not (Path(paths.PROJECT_DIR) / "trading.db").exists(), (
            "hay un trading.db en la raíz además del de database/: "
            "la app y el análisis abrirán ficheros distintos"
        )

    def test_el_fichero_de_estrategia_existe(self):
        assert Path(paths.STRATEGY_PATH).is_file(), (
            f"no existe el fichero de estrategia en {paths.STRATEGY_PATH}"
        )

    def test_config_dir_existe(self):
        assert Path(paths.CONFIG_DIR).is_dir()


class TestRutasExternas:
    """Las rutas de DATOS no viven en el repo, y ese es el punto."""

    @pytest.mark.parametrize("name", EXTERNAL_DATA)
    def test_datos_fuera_del_repo(self, name):
        value = getattr(paths, name)
        assert os.path.isabs(value), f"{name} es relativa: dependería del CWD"
        assert not paths._is_within(value, paths.PROJECT_DIR), (
            f"{name} sigue dentro del repo ({value}): OneDrive lo sincronizaría "
            "y git podría versionarlo"
        )

    def test_la_raiz_del_default_no_es_onedrive(self):
        """El default elegido tiene que pasar su propia regla."""
        assert paths.data_root_guard_error(paths.DATA_ROOT) is None, (
            paths.data_root_guard_error(paths.DATA_ROOT)
        )


class TestGuardianDeRaizDeDatos:

    def test_una_ruta_valida_no_dispara(self, tmp_path):
        assert paths.data_root_guard_error(str(tmp_path)) is None

    def test_relativa_dispara(self):
        motivo = paths.data_root_guard_error("data" + os.sep + "cinta")
        assert motivo is not None
        assert "CWD" in motivo

    def test_vacia_dispara(self):
        assert paths.data_root_guard_error("") is not None
        assert paths.data_root_guard_error("   ") is not None
        assert paths.data_root_guard_error(None) is not None

    def test_dentro_del_repo_dispara(self):
        motivo = paths.data_root_guard_error(
            os.path.join(paths.PROJECT_DIR, "parquet"))
        assert motivo is not None
        assert "dentro del repo" in motivo

    def test_dentro_de_onedrive_dispara(self):
        motivo = paths.data_root_guard_error(
            os.path.join("C:\\", "Users", "x", "OneDrive", "cintas"))
        assert motivo is not None
        assert "OneDrive" in motivo

    def test_dentro_de_ref_dispara(self):
        motivo = paths.data_root_guard_error(
            os.path.join(paths.REFERENCE_DIR, "cintas"))
        assert motivo is not None
        assert "solo lectura" in motivo

    @pytest.mark.parametrize("bad", [
        "", "   ", None, "cinta", os.path.join("C:\\", "Users", "OneDrive", "x"),
    ])
    def test_todo_lo_malo_dispara(self, bad):
        assert paths.data_root_guard_error(bad) is not None

    def test_importar_no_crea_directorios(self):
        """paths.py debe declarar, no crear.

        `ensure_dir` es la ÚNICA puerta de escritura del módulo. Sin esta
        aserción, un `os.makedirs` al importar crearía la carpeta de datos en
        cualquier ordenador que importara el módulo, con solo abrir la app.
        """
        source = Path(paths.__file__).read_text(encoding="utf-8")
        assert source.count("os.makedirs(") == 1, (
            "paths.py solo debe crear directorios dentro de ensure_dir")


class TestPathsHaveNoProjectImports:

    def test_paths_no_importa_el_proyecto(self):
        """Si paths necesitara importar algo del proyecto, sería que una ruta está
        mal de sitio: el módulo raíz no puede depender de las capas de arriba."""
        source = Path(paths.__file__).read_text(encoding="utf-8")
        for forbidden in ("import store", "import strategy", "import risk_engine",
                          "import app", "import agent", "import adapters",
                          "from adapters", "import api", "import settings"):
            assert forbidden not in source, (
                f"core/paths.py no puede depender del proyecto ({forbidden})"
            )

    def test_ensure_dir_crea_y_devuelve(self, tmp_path):
        target = tmp_path / "nuevo" / "anidado"
        assert paths.ensure_dir(str(target)) == str(target)
        assert target.is_dir()
        # Idempotente: llamarla dos veces no debe fallar.
        assert paths.ensure_dir(str(target)) == str(target)

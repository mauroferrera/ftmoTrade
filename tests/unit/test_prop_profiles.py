"""Tests del mapa de perfiles prop por cuenta (`core/prop_profiles.py` + source).

Lo que se verifica no es "parsea un YAML", sino las decisiones que hacen segura la
resolución de un perfil FTMO:

1. El login de MT5 (int o texto) resuelve SIEMPRE al mismo perfil.
2. Una cuenta desconocida da `None`, no un default: el shell prop la bloquea.
3. Un perfil mal escrito REVIENTA citando el campo, no resuelve a topes inventados.
4. La fuente lee el fichero con caché por firma y trata el ausente como vacío.

Run:  python -m pytest tests/unit/test_prop_profiles.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import prop_profiles as pp
from settings import prop_profiles_source as source


# ---------------------------------------------------------------------------
# Documento de ejemplo
# ---------------------------------------------------------------------------

_DOC = {
    "profiles": {
        "ftmo_2step_25k": {
            "product": "2step",
            "initial_capital": 25000,
            "phase": "challenge",
            "internal_daily_pct": 4.0,
            "internal_total_pct": 7.0,
            "risk_pct_per_trade": 0.5,
        },
        "ftmo_1step_25k": {
            "product": "1step",
            "initial_capital": 25000,
        },
    },
    "accounts": {
        12345678: "ftmo_2step_25k",
        "87654321": "ftmo_1step_25k",
    },
}


class TestParseProfiles:
    def test_documento_vacio_o_ausente_es_registro_vacio(self):
        for doc in (None, {}):
            reg = pp.parse_profiles(doc)
            assert not reg
            assert reg.nombres() == [] and reg.account_ids() == []

    def test_perfiles_resueltos_con_los_numeros_congelados(self):
        reg = pp.parse_profiles(_DOC)
        assert reg.nombres() == ["ftmo_1step_25k", "ftmo_2step_25k"]

        p = reg.profile("ftmo_2step_25k")
        assert p.ftmo_total_usd == pytest.approx(2_500.0)
        assert p.internal_daily_usd == pytest.approx(1_000.0)
        assert p.internal_total_usd == pytest.approx(1_750.0)
        assert p.risk_usd == pytest.approx(125.0)

        q = reg.profile("ftmo_1step_25k")
        assert q.best_day_rule is True
        assert q.ftmo_total_trailing is True

    def test_login_normaliza_int_y_texto_a_la_misma_cuenta(self):
        reg = pp.parse_profiles(_DOC)
        por_int = reg.for_account(12345678)
        por_texto = reg.for_account("12345678")
        por_espacios = reg.for_account("  12345678  ")
        assert por_int is not None
        assert por_int is por_texto is por_espacios

    def test_cuenta_desconocida_devuelve_none(self):
        reg = pp.parse_profiles(_DOC)
        assert reg.for_account(99999999) is None
        assert reg.for_account(None) is None

    def test_sin_seccion_accounts_todo_es_desconocido(self):
        reg = pp.parse_profiles({"profiles": _DOC["profiles"]})
        assert reg.profile("ftmo_2step_25k") is not None
        assert reg.for_account(12345678) is None

    def test_perfil_por_nombre_desconocido_da_none(self):
        reg = pp.parse_profiles(_DOC)
        assert reg.profile("no_existe") is None

    def test_bool_no_es_un_login_valido_aunque_sea_int(self):
        """`True == 1` en Python; un login booleano es un error de datos, no la cuenta 1."""
        reg = pp.parse_profiles({"profiles": _DOC["profiles"],
                                 "accounts": {True: "ftmo_2step_25k"}})
        assert reg.for_account(1) is None or reg.account_ids() != ["1"]


class TestParseProfilesErrores:
    def test_documento_que_no_es_mapa(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles([1, 2, 3])
        assert "mapa" in str(exc.value)

    def test_clave_desconocida_en_la_raiz(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {}, "account": {}})
        assert "account" in str(exc.value)

    def test_profiles_no_es_mapa(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": ["x"]})
        assert "profiles" in str(exc.value)

    def test_perfil_que_no_es_mapa(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {"p": 3}})
        assert "'p'" in str(exc.value)

    def test_perfil_con_clave_desconocida(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {"p": {
                "product": "2step", "initial_capital": 25000, "target_pct": 10}}})
        assert "target_pct" in str(exc.value)

    def test_falta_product(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {"p": {"initial_capital": 25000}}})
        assert "product" in str(exc.value)

    def test_falta_initial_capital(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {"p": {"product": "2step"}}})
        assert "initial_capital" in str(exc.value)

    def test_producto_invalido_cita_los_validos(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {"p": {
                "product": "3step", "initial_capital": 25000}}})
        assert "2step" in str(exc.value) and "1step" in str(exc.value)

    def test_fase_invalida(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {"p": {
                "product": "1step", "initial_capital": 25000,
                "phase": "verification"}}})
        assert "challenge" in str(exc.value)

    def test_corte_interno_mas_ancho_que_el_de_ftmo(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {"p": {
                "product": "2step", "initial_capital": 25000,
                "internal_daily_pct": 6.0}}})
        assert "internal_daily_pct" in str(exc.value)

    def test_campo_numerico_no_numerico(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {"p": {
                "product": "2step", "initial_capital": "mucho"}}})
        assert "initial_capital" in str(exc.value)

    def test_accounts_no_es_mapa(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {}, "accounts": [1]})
        assert "accounts" in str(exc.value)

    def test_cuenta_apunta_a_perfil_inexistente(self):
        with pytest.raises(pp.PropProfilesError) as exc:
            pp.parse_profiles({"profiles": {"p": {
                "product": "2step", "initial_capital": 25000}},
                "accounts": {111: "no_existe"}})
        assert "no_existe" in str(exc.value)


# ---------------------------------------------------------------------------
# El fichero real del repositorio
# ---------------------------------------------------------------------------


class TestFicheroReal:
    def test_el_yaml_del_repo_es_valido_y_trae_los_dos_perfiles(self):
        from core import paths as paths_mod

        reg = pp.parse_profiles(source._safe_load(
            Path(paths_mod.PROP_PROFILES_PATH).read_text(encoding="utf-8")))
        assert "ftmo_2step_25k" in reg.nombres()
        assert "ftmo_1step_25k" in reg.nombres()

    def test_el_2step_del_repo_cuadra_con_el_plan(self):
        reg = source.load(force=True)
        p = reg.profile("ftmo_2step_25k")
        assert p.internal_daily_usd == pytest.approx(1_000.0)
        assert p.internal_total_usd == pytest.approx(1_750.0)
        assert p.risk_usd == pytest.approx(125.0)


# ---------------------------------------------------------------------------
# El lector del fichero (settings.prop_profiles_source)
# ---------------------------------------------------------------------------


@pytest.fixture()
def perfiles_tmp(tmp_path, monkeypatch):
    """Un `prop_profiles.yaml` temporal, con la caché del source vaciada.

    `FTMO_PROFILE` se borra del entorno: es una variable del `.env` del operador
    y, si el proceso de test la arrastra, decide el mapeo por encima del YAML
    temporal y convierte cada aserción en una función de lo que haya en esa máquina.
    Los tests que quieran probarla la vuelven a poner con `setenv`.
    """
    target = tmp_path / "prop_profiles.yaml"
    target.write_text(
        "profiles:\n"
        "  ftmo_2step_25k:\n"
        "    product: 2step\n"
        "    initial_capital: 25000\n"
        "accounts:\n"
        "  12345678: ftmo_2step_25k\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(source, "PROP_PROFILES_PATH", str(target))
    monkeypatch.delenv("FTMO_PROFILE", raising=False)
    source.invalidate()
    yield target
    source.invalidate()


class TestPropProfilesSource:
    def test_load_del_fichero_temporal(self, perfiles_tmp: Path) -> None:
        reg = source.load()
        assert reg.for_account(12345678) is not None
        assert reg.profile("ftmo_2step_25k").internal_total_usd == pytest.approx(1_750.0)

    def test_fichero_ausente_es_registro_vacio(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(source, "PROP_PROFILES_PATH", str(tmp_path / "no.yaml"))
        source.invalidate()
        assert not source.load()

    def test_fichero_mal_formado_lanza(self, tmp_path, monkeypatch) -> None:
        target = tmp_path / "prop_profiles.yaml"
        target.write_text("profiles: [", encoding="utf-8")
        monkeypatch.setattr(source, "PROP_PROFILES_PATH", str(target))
        source.invalidate()
        with pytest.raises(pp.PropProfilesError):
            source.load()

    def test_la_cache_se_invalida_sola_al_editar_el_fichero(self, perfiles_tmp: Path) -> None:
        assert source.load().for_account(12345678) is not None
        perfiles_tmp.write_text(
            "profiles:\n  ftmo_2step_25k:\n    product: 2step\n"
            "    initial_capital: 50000\n",
            encoding="utf-8",
        )
        reg = source.load()
        assert reg.profile("ftmo_2step_25k").initial_capital == 50000
        assert reg.for_account(12345678) is None

    def test_carga_cuenta_desde_mt5_login_env(self, perfiles_tmp: Path, monkeypatch) -> None:
        monkeypatch.setenv("MT5_LOGIN", "999888")
        source.invalidate()
        reg = source.load()
        assert reg.for_account(999888) is not None
        assert reg.accounts.get("999888") == "ftmo_2step_25k"
        assert reg.for_account("999888").product == "2step"

    def test_carga_cuenta_con_ftmo_profile_especifico(self, perfiles_tmp: Path, monkeypatch) -> None:
        perfiles_tmp.write_text(
            "profiles:\n"
            "  ftmo_2step_25k:\n"
            "    product: 2step\n"
            "    initial_capital: 25000\n"
            "  ftmo_1step_25k:\n"
            "    product: 1step\n"
            "    initial_capital: 25000\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MT5_LOGIN", "888777")
        monkeypatch.setenv("FTMO_PROFILE", "ftmo_1step_25k")
        source.invalidate()
        reg = source.load()
        assert reg.for_account(888777) is not None
        assert reg.accounts.get("888777") == "ftmo_1step_25k"
        assert reg.for_account("888777").product == "1step"


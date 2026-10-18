"""Tests de `core.smc_engine`: FVG, Order Blocks, Sweeps.

El motor es puro: se le pasa una lista de velas y devuelve estructuras. Aquí se
fijan los contratos que el resto del sistema espera, no los números exactos de un
fixture gigante:

  - Un FVG detectado lleva `type`/`top`/`bottom`/`start_time` y su `top > bottom`.
  - Una zona MITIGADA por una mecha posterior desaparece.
  - Un sweep exige que la vela pinche PDH/PDL y CIERRE de vuelta dentro.
  - `classify_entry` cruza entrada y zona por tiempo y precio.
"""

from __future__ import annotations

from core import smc_engine as smc


def _vela(t, o, h, l, c):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


def _plana(t, c=1.10):
    return _vela(t, c, c, c, c)


# ---------------------------------------------------------------------------
# Fair Value Gaps
# ---------------------------------------------------------------------------


class TestDetectFvgs:
    def test_un_hueco_alcista_se_detecta(self):
        """Vela i abre por encima del high de i-2: hueco [prev.high, cur.low]."""
        velas = [
            _vela(1, 1.10, 1.11, 1.09, 1.10),
            _vela(2, 1.11, 1.12, 1.10, 1.11),
            _vela(3, 1.13, 1.14, 1.13, 1.135),  # low 1.13 > high i-2 (1.11)
        ]
        fvgs = smc.detect_fvgs(velas)
        assert len(fvgs) == 1
        fg = fvgs[0]
        assert fg["type"] == smc.BULLISH_FVG
        assert fg["bottom"] == 1.11
        assert fg["top"] == 1.13
        assert fg["top"] > fg["bottom"]
        assert fg["start_time"] == 3

    def test_un_hueco_bajista_se_detecta(self):
        velas = [
            _vela(1, 1.10, 1.11, 1.09, 1.10),
            _vela(2, 1.09, 1.10, 1.08, 1.09),
            _vela(3, 1.07, 1.07, 1.06, 1.065),  # high 1.07 < low i-2 (1.09)
        ]
        fvgs = smc.detect_fvgs(velas)
        assert len(fvgs) == 1
        assert fvgs[0]["type"] == smc.BEARISH_FVG
        assert fvgs[0]["top"] == 1.09
        assert fvgs[0]["bottom"] == 1.07

    def test_sin_hueco_no_hay_fvg(self):
        velas = [_plana(t, 1.10) for t in range(1, 6)]
        assert smc.detect_fvgs(velas) == []

    def test_un_fvg_mitigado_desaparece(self):
        """Si una vela posterior mete la mecha en la caja, el FVG ya no está vivo."""
        velas = [
            _vela(1, 1.10, 1.11, 1.09, 1.10),
            _vela(2, 1.11, 1.12, 1.10, 1.11),
            _vela(3, 1.13, 1.14, 1.13, 1.135),  # FVG [1.11, 1.13]
            _vela(4, 1.135, 1.145, 1.115, 1.12),  # mecha baja a 1.115: mitiga
        ]
        assert smc.detect_fvgs(velas) == []


# ---------------------------------------------------------------------------
# Order Blocks
# ---------------------------------------------------------------------------


class TestDetectOrderBlocks:
    def test_un_ob_alcista_tras_romper_estructura(self):
        """Sube, hace un swing high, corrige y rompe: la vela bajista previa es el OB.

        Serie larga a propósito: `swing_pivots` necesita `window` velas a cada
        lado, así que con menos de ~11 velas no hay ningún pivote confirmado y no
        puede haber BOS (ni, por tanto, OB).
        """
        filas = [
            (1.100, 1.105, 1.098, 1.104),
            (1.104, 1.110, 1.103, 1.109),
            (1.109, 1.114, 1.108, 1.113),
            (1.113, 1.118, 1.112, 1.117),
            (1.117, 1.122, 1.116, 1.121),
            (1.121, 1.128, 1.120, 1.127),
            (1.127, 1.135, 1.126, 1.133),  # swing high (high 1.135)
            (1.133, 1.134, 1.125, 1.127),
            (1.127, 1.129, 1.120, 1.122),
            (1.122, 1.124, 1.115, 1.117),
            (1.117, 1.119, 1.111, 1.113),
            (1.113, 1.116, 1.106, 1.108),  # OB: última vela bajista
            (1.108, 1.140, 1.107, 1.138),  # BOS: cierra por encima del swing high
            (1.138, 1.145, 1.136, 1.143),
            (1.143, 1.150, 1.141, 1.148),
            (1.148, 1.155, 1.146, 1.153),
            (1.153, 1.160, 1.151, 1.158),
            (1.158, 1.165, 1.156, 1.163),
            (1.163, 1.170, 1.161, 1.168),
            (1.168, 1.175, 1.166, 1.173),
            (1.173, 1.180, 1.171, 1.178),
            (1.178, 1.185, 1.176, 1.183),
        ]
        velas = [_vela(t + 1, o, h, l, c) for t, (o, h, l, c) in enumerate(filas)]
        obs = smc.detect_order_blocks(velas)
        alcistas = [o for o in obs if o["type"] == smc.BULLISH_OB]
        assert alcistas, "se esperaba un OB alcista"
        assert any(o["top"] == 1.116 and o["bottom"] == 1.106 for o in alcistas)
        # Ninguna vela posterior penetra la caja, así que el OB sigue VIVO.
        assert all(o["top"] >= o["bottom"] for o in obs)

    def test_sin_ruptura_no_hay_ob(self):
        """Precio que solo va y viene: nunca rompe estructura, no hay OB."""
        velas = [_plana(t, 1.10) for t in range(1, 12)]
        assert smc.detect_order_blocks(velas) == []


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------


class TestDetectSweeps:
    def test_pdh_sweep_pincha_y_cierra_dentro(self):
        velas = [_vela(1, 1.10, 1.13, 1.09, 1.11)]  # high 1.13 > pdh 1.12, close 1.11 < 1.12
        sweeps = smc.detect_sweeps(velas, pdh=1.12, pdl=None)
        assert len(sweeps) == 1
        assert sweeps[0]["type"] == smc.PDH_SWEEP
        assert sweeps[0]["price_level"] == 1.12
        assert sweeps[0]["wick_extreme"] == 1.13

    def test_pdl_sweep_pincha_y_cierra_dentro(self):
        velas = [_vela(1, 1.10, 1.11, 1.07, 1.09)]  # low 1.07 < pdl 1.08, close 1.09 > 1.08
        sweeps = smc.detect_sweeps(velas, pdh=None, pdl=1.08)
        assert len(sweeps) == 1
        assert sweeps[0]["type"] == smc.PDL_SWEEP

    def test_si_cierra_fuera_del_rango_no_es_sweep(self):
        """Un breakout real (cierra por encima del PDH) NO es un sweep."""
        velas = [_vela(1, 1.10, 1.13, 1.09, 1.125)]  # close 1.125 > pdh 1.12
        assert smc.detect_sweeps(velas, pdh=1.12, pdl=None) == []


# ---------------------------------------------------------------------------
# analyze / classify_entry
# ---------------------------------------------------------------------------


class TestAnalyze:
    def test_devuelve_las_tres_familias(self):
        velas = [_plana(t, 1.10) for t in range(1, 10)]
        out = smc.analyze(velas, 1.12, 1.08, symbol="EURUSD", timeframe="M15")
        assert out["symbol"] == "EURUSD"
        assert out["timeframe"] == "M15"
        assert set(out["patterns"]) == {"fvgs", "order_blocks", "sweeps"}


class TestClassifyEntry:
    def test_entrada_dentro_de_un_fvg_lo_reporta(self):
        velas = [
            _vela(1, 1.10, 1.11, 1.09, 1.10),
            _vela(2, 1.11, 1.12, 1.10, 1.11),
            _vela(3, 1.13, 1.14, 1.13, 1.135),  # FVG [1.11, 1.13]
        ]
        salida = smc.classify_entry(velas, None, None, entry_price=1.12, entry_time=3)
        assert salida["poi_type"] == "Bullish FVG"

    def test_entrada_fuera_de_toda_zona_no_reporta_poi(self):
        velas = [_plana(t, 1.10) for t in range(1, 6)]
        salida = smc.classify_entry(velas, None, None, entry_price=1.10, entry_time=3)
        assert salida["poi_type"] is None
        assert salida["liquidity_swept"] is None

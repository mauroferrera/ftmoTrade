"""I/O de `config/strategy.yaml`: el ÚNICO sitio que abre ese fichero.

Devuelve el dict crudo del YAML. El aplanado y la validación viven en
`core/strategy.py` (que llega en M1); hasta entonces, `store` consume la forma
cruda y el resto del sistema no lee el fichero por su cuenta.

Por qué esta capa existe aunque hoy sea casi trivial: el día que el aplanado
llegue, hay UN sitio que cambia y ningún importador de `yaml` escondido en
`core/` (que por regla de pureza no puede tocar el disco).
"""

from __future__ import annotations

import os
from typing import Any

import yaml

from core import paths


def get_config() -> dict:
    """Lee `strategy.yaml` entero. `{}` si el fichero está vacío o no existe."""
    try:
        with open(paths.STRATEGY_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def save(cfg: dict) -> None:
    """Escribe `strategy.yaml` de forma atómica.

    Se escribe a un `.tmp` y se hace `os.replace`: un guardado a medio camino
    (crash, kill, corte) deja el fichero de estrategia truncado, y de ahí el
    sistema arranca sin configuración y sin saber por qué.
    """
    target = paths.STRATEGY_PATH
    tmp = str(target) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(cfg or {}, fh, allow_unicode=True, sort_keys=False)
    os.replace(tmp, target)


__all__ = ["get_config", "save"]

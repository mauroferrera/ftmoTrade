"""El contenedor de la aplicación: quién tiene qué, y quién enciende qué.

Por qué un `Runtime` y no globals
---------------------------------
El estado del proceso vive en un objeto que se crea en `create_app()` y se
guarda en `app.state.runtime`. Los endpoints lo leen con `deps.runtime(request)`.
Las rutas no importan la app, los tests pueden construir un `Runtime` con dobles,
y `/api/health` puede responder qué hay conectado y qué no.

En M0 el contenedor tenía UNA pieza: `store`. En M1 se añade `market`
(`api.services.market.MarketService`), que es la fachada de lectura de MT5.
Los WebSockets y los servicios (watcher/ejecución) llegan en M2+ y se
enganchan aquí, sin que las rutas cambien de forma.

Sobre qué se enciende al arrancar
---------------------------------
**Nada que hable con la red.** `startup()` deja la base inicializada (idempotente)
y no abre MT5: el adaptador es perezoso (solo conecta en la primera llamada
real), así que instanciar el servicio no abre la terminal. Un proceso que abre
un terminal al importar no se puede testear, y uno que lo enciende todo al
arrancar esconde el fallo de arranque detrás de un 502 en la primera petición.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger("api.runtime")


class Runtime:
    """Las piezas de la aplicación y su ciclo de vida.

    Todos los colaboradores son opcionales e inyectables. `build()` es el único
    sitio que compone la configuración real; un test hace
    `Runtime(store=FakeStore(), market=FakeMarket())` y ya tiene el cableado.
    """

    __slots__ = ("store", "market", "prop", "watcher", "execution", "_store_backend")

    def __init__(
        self,
        *,
        store: Optional[Any] = None,
        market: Optional[Any] = None,
        prop: Optional[Any] = None,
        watcher: Optional[Any] = None,
        execution: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.market = market
        self.prop = prop
        self.watcher = watcher
        self.execution = execution
        self._store_backend = "inyectado" if store is not None else None

    @classmethod
    def build(cls, *, store: Optional[Any] = None) -> "Runtime":
        """Compone el cableado real: `database.store` + `MarketService`.

        Los imports son perezosos a propósito: importar `Runtime` no debe abrir
        la base ni mirar el terminal.

        `_store_backend` se deja en `None` cuando el store NO fue inyectado, para
        que `startup()` sepa que debe llamar a `init_db()`. `__init__` no puede
        decidirlo por su cuenta: para cuando lo ve, `store` ya es el módulo y
        parecería inyectado.

        El `MarketService` se construye aquí pero NO conecta: el adaptador MT5 es
        perezoso y solo abre la terminal en la primera llamada real. Si el
        adaptador no puede ni construirse (entorno rarísimo), el runtime sigue
        arrancando con `market=None`: `/api/health` es justamente para poder
        decir "el mercado no responde".
        """
        from database import store as store_mod

        injected = store is not None
        rt = cls(store=store if injected else store_mod)
        if not injected:
            rt._store_backend = None
        rt.market = cls._build_market()
        rt.prop = cls._build_prop(rt.store, rt.market)
        rt.watcher = cls._build_watcher(rt.store, rt.market)
        rt.execution = cls._build_execution()
        return rt

    @staticmethod
    def _build_market() -> Optional[Any]:
        """El servicio de mercado real, o None si no hay adaptador utilizable."""
        try:
            from api.services.market import MarketService

            return MarketService()
        except Exception as exc:  # noqa: BLE001 - arrancar sin mercado es un hecho
            log.warning("mercado no disponible: %s", exc)
            return None

    @staticmethod
    def _build_prop(store: Optional[Any], market: Optional[Any]) -> Optional[Any]:
        """El servicio de prop, o None si le falta la cuenta o el store.

        Sin cualquiera de los dos no se puede resolver el perfil ni leer la
        cuenta, y sin perfil no hay nada que evaluar. Se deja en `None` y
        `deps.prop_service` responde 503.
        """
        if store is None or market is None:
            return None
        try:
            from api.services.prop import PropService

            return PropService(market=market, store=store)
        except Exception as exc:  # noqa: BLE001 - arrancar sin prop es un hecho
            log.warning("prop no disponible: %s", exc)
            return None

    @staticmethod
    def _build_watcher(store: Optional[Any], market: Optional[Any]) -> Optional[Any]:
        """El servicio del watcher, o None si no hay mercado ni store.

        El watcher solo avisa y audita (D-005): no necesita ejecución, y por eso no
        depende de nada que no sea leer el mercado y escribir la auditoría. Se construye
        AUNQUE falte uno de los dos, porque el servicio degrada y lo declara en su
        respuesta (`enabled`/`motivo`), que es más útil que un 503 en `/status`.
        """
        try:
            from api.services.watcher import WatcherService

            return WatcherService(market=market, store=store)
        except Exception as exc:  # noqa: BLE001 - arrancar sin watcher es un hecho
            log.warning("watcher no disponible: %s", exc)
            return None

    @staticmethod
    def _build_execution() -> Optional[Any]:
        """El puerto de ejecución real (MT5), o None si el adaptador no se puede ni armar.

        **No conecta nada al construirse.** `MT5ExecutionAdapter` comparte la sesión
        perezosa del resto del sistema y solo habla con el terminal en la primera orden
        real; instanciarlo no abre MT5. Se deja en `None` si el paquete no está, y
        entonces el servicio de ejecución sigue montado pero responde 503 en el envío
        (`NO_EXECUTION_PORT`): la instalación analiza pero no opera. Ese `None` es un
        estado declarado, no un fallo de arranque.
        """
        try:
            from adapters.mt5.mt5_execution import adapter as execution_adapter

            return execution_adapter()
        except Exception as exc:  # noqa: BLE001 - arrancar sin ejecución es un hecho
            log.warning("ejecución no disponible: %s", exc)
            return None

    # -- ciclo de vida -----------------------------------------------------------

    async def startup(self) -> None:
        """Prepara lo que hace falta para servir, y nada más.

        Deja la base inicializada (una sola vez; `init_db` es idempotente). Si la
        base no está, la app ARRANCA igual: `/api/health` existe justamente para
        poder decir "la base no responde", y una API que no arranca no puede.
        """
        if self.store is not None and self._store_backend is None:
            try:
                self.store.init_db()
                self._store_backend = "sqlite"
            except Exception as exc:  # noqa: BLE001 - arrancar sin DB es un hecho
                self._store_backend = "error"
                log.warning("base de datos no disponible: %s", exc)
        log.info("runtime listo: store=%s", self._store_backend)

    async def shutdown(self) -> None:
        """Apaga lo que se encendió. Idempotente. En M0 no hay nada que apagar."""
        return None

    # -- diagnóstico -------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Qué está vivo, qué no, y por qué. `/api/health` responde esto.

        Los estados son strings y no booleanos porque lo que un operador necesita
        leer no es si algo está "ok" sino si está `sin_puerto`, `sin_base` o
        `error`, y un `false` no dice cuál de las tres cosas es.
        """
        return {
            "ok": bool(self.store is not None),
            "store": self._estado_store(),
            "market": self._estado_market(),
        }

    def _estado_market(self) -> Dict[str, Any]:
        """Cómo está el mercado, SIN tocar el terminal (`is_open` es un flag)."""
        if self.market is None:
            return {"estado": "sin_puerto"}
        try:
            datos = self.market.estado()
        except Exception as exc:  # noqa: BLE001 - el estado es "no responde"
            return {"estado": "error",
                    "error": "{0}: {1}".format(type(exc).__name__, exc)}
        return {"estado": "ok", **datos}

    def _estado_store(self) -> Dict[str, Any]:
        if self.store is None:
            return {"estado": "sin_puerto"}
        try:
            tablas = self.store.list_db_tables()
        except Exception as exc:  # noqa: BLE001 - el estado es "no responde"
            return {"estado": "error",
                    "error": "{0}: {1}".format(type(exc).__name__, exc)}
        return {"estado": "ok", "tablas": len(tablas or [])}

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return "<Runtime store={0}>".format(self._store_backend)


def token_actual() -> Optional[str]:
    """`API_TOKEN` leído AHORA, o `None` si no hay token configurado.

    Se lee por llamada y no al importar para que un token rotado en el entorno se
    aplique sin reiniciar, y para que los tests puedan monkeypatchearlo. Sin token
    no hay autenticación (el modo por defecto de un desarrollo local).
    """
    valor = os.getenv("API_TOKEN", "").strip()
    return valor or None


__all__ = ["Runtime", "token_actual"]

"""Perfiles de prop firm por CUENTA: el doc de `config/prop_profiles.yaml`, resuelto.

Modulo PURO, como el resto de `core/`: no abre ficheros ni habla con el bróker.
Recibe el documento YA parseado (un dict) y devuelve un registro de perfiles del
que se saca, por login de MT5, el `PropProfile` de esa cuenta.

Por qué existe, separado de `core/prop_rules.py`
------------------------------------------------
`prop_rules` sabe QUÉ es un perfil FTMO (producto 2-Step/1-Step, cortes de FTMO e
internos, buffer, Best Day) y cómo se evalúa. Lo que no sabe es de QUÉ CUENTA es
ese perfil. Esa unión —`login` de MT5 → perfil— es lo que vive aquí, y es la que
hace que dimensionar sobre el buffer de la firma sea posible: sin saber el capital
inicial de la cuenta no hay buffer, y sin buffer el sizing cae al balance.

El reparto es el mismo que `strategy_map` con `strategy_map_source`: aquí la
validación pura y la resolución; en `settings/prop_profiles_source.py` la lectura
del fichero y la caché. Un YAML mal formado tiene que reventar, no resolver en
silencio a un perfil por defecto: un tope de DD equivocado no da error, da una
cuenta reventada.

Qué NO decide este módulo
-------------------------
- No elige qué hacer con una cuenta DESCONOCIDA. Devuelve `None` y quien llama
  decide (el shell prop, fail-closed, bloquea). Meter aquí el "y si no, bloquea"
  sería una política de ejecución dentro de `core/`.
- No lee el reloj, no mide equity y no evalúa: eso es `prop_rules.evaluate`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.prop_rules import RISK_PCT_PER_TRADE, PropProfile, ftmo_profile

__all__ = [
    "PropProfilesError",
    "PropProfiles",
    "empty_profiles",
    "parse_profiles",
]

#: Claves del documento que SÍ se conocen. Cualquier otra se rechaza: una clave
#: mal escrita (`account` por `accounts`) no debe pasar en silencio, porque el
#: efecto es que la cuenta no queda mapeada y el fallo aparece lejos, al operar.
_CLAVES_DOC = ("profiles", "accounts")

#: Claves admitidas por perfil. `product` y `initial_capital` son obligatorias;
#: el resto se omiten para heredar los defaults de `prop_rules`.
_CLAVES_PERFIL = (
    "product", "initial_capital", "phase",
    "internal_daily_pct", "internal_total_pct", "risk_pct_per_trade",
)


class PropProfilesError(ValueError):
    """`prop_profiles.yaml` no describe perfiles usables. Nunca se calla."""


def _norm(account_id: Any) -> str:
    """Normaliza un login a la llave canónica del mapa.

    El login llega como `int` desde MT5 (`account_info()['login']`) pero el YAML
    puede escribirlo con o sin comillas (`12345678` es entero, `"12345678"` es
    texto). Sin normalizar, las dos formas serían cuentas distintas y el perfil
    no se encontraría según cómo se hubiera escrito la llave.
    """
    if account_id is None:
        return ""
    if isinstance(account_id, bool):  # bool es subclase de int; un login no lo es
        return str(account_id)
    if isinstance(account_id, int):
        return str(account_id)
    return str(account_id).strip()


class PropProfiles:
    """Registro inmutable de perfiles resueltos, indexado por login de MT5.

    `accounts` guarda login -> NOMBRE de perfil (no el perfil entero) porque un
    mismo perfil puede servirse a varias cuentas: dos challenges del mismo tamaño
    comparten topes. `profiles` resuelve el nombre a un `PropProfile` ya
    construido. Separar ambos hace visible en el YAML qué se reutiliza.
    """

    __slots__ = ("profiles", "accounts")

    def __init__(self, profiles: Dict[str, PropProfile],
                 accounts: Dict[str, str]) -> None:
        self.profiles = dict(profiles)
        self.accounts = dict(accounts)

    def for_account(self, account_id: Any) -> Optional[PropProfile]:
        """El perfil del login, o `None` si la cuenta no está mapeada.

        `None` es una respuesta legítima que quien llama interpreta: el shell prop
        la trata como fail-closed (no opera sin perfil). Devolver aquí un perfil
        por defecto escondería una cuenta sin configurar detrás de topes que nadie
        eligió para ella.
        """
        nombre = self.accounts.get(_norm(account_id))
        if nombre is None:
            return None
        return self.profiles.get(nombre)

    def profile(self, nombre: str) -> Optional[PropProfile]:
        """El perfil por su NOMBRE (no por login), o `None`."""
        return self.profiles.get(str(nombre))

    def nombres(self) -> List[str]:
        """Los nombres de perfil definidos, ordenados. Para diagnóstico."""
        return sorted(self.profiles)

    def account_ids(self) -> List[str]:
        """Los logins mapeados, ordenados. Para diagnóstico."""
        return sorted(self.accounts)

    def __bool__(self) -> bool:
        """`False` cuando no hay ningún perfil: el fichero ausente o vacío."""
        return bool(self.profiles)

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return "PropProfiles(profiles={0}, accounts={1})".format(
            self.nombres(), self.account_ids())


def empty_profiles() -> PropProfiles:
    """Registro vacío: ni perfiles ni cuentas. Es el estado "sin configurar".

    Es lo que devuelve la fuente cuando el fichero no existe. No es un error (el
    fichero puede no existir todavía) pero tampoco un default silencioso: vacío
    significa que `for_account` da `None` para toda cuenta, y con el shell prop
    encendido eso bloquea en vez de dimensionar a ciegas.
    """
    return PropProfiles({}, {})


def parse_profiles(doc: Any) -> PropProfiles:
    """Construye el registro desde el documento YAML ya parseado.

    Documento ausente o vacío -> registro vacío. Cualquier otra cosa que no sea un
    mapa, o un perfil/cuenta mal formado, revienta con `PropProfilesError` citando
    el campo culpable: un perfil FTMO mal escrito no da un fallo visible, da topes
    equivocados.

    Forma esperada:

        profiles:
          ftmo_2step_25k:
            product: 2step           # 2step | 1step
            initial_capital: 25000
            phase: challenge         # challenge | verification (verification: 2step)
            internal_daily_pct: 4.0  # opcional (default de prop_rules)
            internal_total_pct: 7.0  # opcional
            risk_pct_per_trade: 0.5  # opcional
        accounts:
          12345678: ftmo_2step_25k
    """
    if doc is None:
        return empty_profiles()
    if not isinstance(doc, dict):
        raise PropProfilesError(
            "prop_profiles.yaml debe ser un mapa con 'profiles' y/o 'accounts', "
            "no {0}.".format(type(doc).__name__))
    if not doc:
        return empty_profiles()

    desconocidas = sorted(set(doc) - set(_CLAVES_DOC))
    if desconocidas:
        raise PropProfilesError(
            "claves no reconocidas en prop_profiles.yaml: {0}. Se esperan solo "
            "{1} (¿una llave mal escrita que dejaría la cuenta sin mapear?).".format(
                ", ".join(desconocidas), ", ".join(_CLAVES_DOC)))

    perfiles = _parse_profiles(doc.get("profiles"))
    cuentas = _parse_accounts(doc.get("accounts"), perfiles)
    return PropProfiles(perfiles, cuentas)


def _parse_profiles(raw: Any) -> Dict[str, PropProfile]:
    """Los perfiles, resueltos a `PropProfile` con `prop_rules.ftmo_profile`."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PropProfilesError(
            "'profiles' debe ser un mapa nombre -> perfil, no {0}.".format(
                type(raw).__name__))
    salida: Dict[str, PropProfile] = {}
    for nombre, spec in raw.items():
        salida[str(nombre)] = _parse_un_perfil(str(nombre), spec)
    return salida


def _parse_un_perfil(nombre: str, spec: Any) -> PropProfile:
    """Un perfil a `PropProfile`, con TODAS las claves validadas antes de construir."""
    if not isinstance(spec, dict):
        raise PropProfilesError(
            "el perfil {0!r} debe ser un mapa de reglas, no {1}.".format(
                nombre, type(spec).__name__))
    desconocidas = sorted(set(spec) - set(_CLAVES_PERFIL))
    if desconocidas:
        raise PropProfilesError(
            "perfil {0!r}: claves no reconocidas {1}. Admitidas: {2}.".format(
                nombre, ", ".join(desconocidas), ", ".join(_CLAVES_PERFIL)))
    for obligatoria in ("product", "initial_capital"):
        if spec.get(obligatoria) is None:
            raise PropProfilesError(
                "perfil {0!r}: falta '{1}', obligatoria.".format(nombre, obligatoria))

    # Los campos se convierten ANTES de construir: así un no-numérico se reporta
    # nombrando el campo, y no envuelto en el mensaje genérico de `ftmo_profile`.
    capital = _req_float(spec, "initial_capital", nombre)
    diario = _opt_float(spec, "internal_daily_pct", nombre)
    total = _opt_float(spec, "internal_total_pct", nombre)
    riesgo = _opt_float(spec, "risk_pct_per_trade", nombre)
    if riesgo is None:
        riesgo = RISK_PCT_PER_TRADE

    try:
        return ftmo_profile(
            spec["product"],
            capital,
            phase=str(spec.get("phase", "challenge")),
            internal_daily_pct=diario,
            internal_total_pct=total,
            risk_pct_per_trade=riesgo,
        )
    except ValueError as exc:
        # `ftmo_profile` valida producto/fase/cortes; su motivo ya nombra el campo.
        raise PropProfilesError(
            "perfil {0!r} inválido: {1}".format(nombre, exc)) from exc


def _req_float(spec: Dict[str, Any], clave: str, nombre: str) -> float:
    """El campo numérico obligatorio, o error nombrando el campo."""
    try:
        return float(spec[clave])
    except (TypeError, ValueError) as exc:
        raise PropProfilesError(
            "perfil {0!r}: '{1}' debe ser numérico, no {2!r}.".format(
                nombre, clave, spec.get(clave))) from exc


def _opt_float(spec: Dict[str, Any], clave: str, nombre: str) -> Optional[float]:
    """El campo numérico opcional, o `None` (para que `ftmo_profile` ponga su default)."""
    valor = spec.get(clave)
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError) as exc:
        raise PropProfilesError(
            "perfil {0!r}: '{1}' debe ser numérico, no {2!r}.".format(
                nombre, clave, valor)) from exc


def _parse_accounts(raw: Any, perfiles: Dict[str, PropProfile]) -> Dict[str, str]:
    """El mapa login -> nombre de perfil, con cada nombre ya verificado."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PropProfilesError(
            "'accounts' debe ser un mapa login -> nombre de perfil, no {0}.".format(
                type(raw).__name__))
    salida: Dict[str, str] = {}
    for login, nombre in raw.items():
        clave = _norm(login)
        if not clave:
            raise PropProfilesError("'accounts' tiene una entrada sin login legible.")
        destino = str(nombre)
        if destino not in perfiles:
            raise PropProfilesError(
                "la cuenta {0!r} apunta al perfil {1!r}, que no existe. "
                "Perfiles definidos: {2}.".format(
                    clave, destino, sorted(perfiles) or "ninguno"))
        salida[clave] = destino
    return salida

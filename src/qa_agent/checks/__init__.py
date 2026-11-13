"""Registro de chequeos. Agregar uno = escribir una función y registrarla acá."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..models import CheckResult, Context, Profile
from . import universal, web

CheckFn = Callable[[Context], CheckResult]


@dataclass
class Check:
    name: str
    fn: CheckFn
    needs_web: bool = False  # requiere que el motor levante (o apunte a) la app web
    applies: Callable[[Profile], bool] = lambda profile: True


def _is_web(profile: Profile) -> bool:
    return profile.kind == "web" and bool(profile.web)


# Los chequeos 'estáticos' solo leen; los que ejecutan comandos o navegan exigen un perfil aprobado.
REGISTRY: list[Check] = [
    Check("secrets", universal.check_secrets),
    Check("env-hygiene", universal.check_env_hygiene),
    Check("install", universal.exec_check("install", "critical")),
    Check("build", universal.exec_check("build", "critical")),
    Check("tests", universal.exec_check("test", "critical")),
    Check("lint", universal.exec_check("lint", "normal")),
    Check("dependency-audit", universal.exec_check("audit", "normal")),
    Check("web-load", web.check_web_load, needs_web=True, applies=_is_web),
    Check("web-quality", web.check_web_quality, needs_web=True, applies=_is_web),
    Check("web-flows", web.check_web_flows, needs_web=True, applies=_is_web),
    Check("lighthouse", web.check_lighthouse, needs_web=True, applies=_is_web),
]

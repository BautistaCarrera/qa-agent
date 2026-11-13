"""Almacenamiento por empresa/proyecto. Cada empresa queda aislada en su propia carpeta."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .models import Profile

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def safe_name(value: str, what: str) -> str:
    """Evita rutas arbitrarias (../) en los nombres de empresa y proyecto."""
    if not _SAFE.match(value) or ".." in value:
        raise ValueError(f"{what} inválido: {value!r} (usá letras, números, '.', '_' o '-')")
    return value


def workspace() -> Path:
    return Path(os.environ.get("QA_AGENT_HOME") or Path.cwd() / "workspace")


def project_dir(company: str, project: str) -> Path:
    return workspace() / safe_name(company, "empresa") / safe_name(project, "proyecto")


def profile_path(company: str, project: str) -> Path:
    return project_dir(company, project) / "profile.yaml"


def save_profile(profile: Profile) -> Path:
    path = profile_path(profile.company, profile.project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(profile.to_dict(), sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def load_profile(company: str, project: str) -> Profile:
    path = profile_path(company, project)
    if not path.exists():
        raise FileNotFoundError(f"No hay perfil para {company}/{project}. Corré primero: qa-agent onboard")
    return Profile.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def list_profiles() -> list[tuple[str, str, bool]]:
    out = []
    root = workspace()
    if not root.exists():
        return out
    for pf in sorted(root.glob("*/*/profile.yaml")):
        data = yaml.safe_load(pf.read_text(encoding="utf-8"))
        out.append((pf.parent.parent.name, pf.parent.name, bool(data.get("approved"))))
    return out

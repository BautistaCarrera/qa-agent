"""Onboarding: descubre qué es un proyecto y cómo se verifica, sin conocerlo de antemano.

La detección es determinista (archivos reconocibles). Si algo es ambiguo o no se reconoce,
queda anotado en el perfil: nunca se inventan comandos. Una persona revisa y aprueba el perfil.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .models import Profile

IGNORED_DIRS = {"node_modules", ".git", ".next", "venv", ".venv", "dist", "build", "__pycache__", "target", ".idea"}
NPM_DEFAULT_TEST = re.compile(r"no test specified")


def _walk(root: Path, depth: int = 2):
    """Recorre el proyecto hasta cierta profundidad ignorando carpetas generadas."""
    stack = [(root, 0)]
    while stack:
        current, level = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in IGNORED_DIRS and level < depth:
                    stack.append((entry, level + 1))
            else:
                yield entry


def detect(path: Path, company: str, project: str) -> Profile:
    root = path.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"No existe la carpeta del proyecto: {root}")
    profile = Profile(company=company, project=project, path=str(root))
    files = list(_walk(root))
    names_at_root = {p.name for p in root.iterdir()}

    detectors = (_node, _python, _go, _dotnet, _java, _odoo)
    for detector in detectors:
        detector(root, names_at_root, files, profile)

    _cross_cutting(root, names_at_root, files, profile)

    if not profile.stacks:
        profile.kind = "unknown"
        profile.notes.append(
            "No se reconoció el stack. Solo se aplican chequeos estáticos (secretos, higiene de .env). "
            "Agregá a mano los comandos en el perfil si querés más cobertura."
        )
    elif len(profile.stacks) > 1:
        profile.notes.append(
            f"Se detectaron varios stacks ({', '.join(profile.stacks)}): puede ser un monorepo. "
            "Revisá que los comandos sean los correctos."
        )
    return profile


def _node(root: Path, names: set, files: list, profile: Profile) -> None:
    pkg_file = root / "package.json"
    if not pkg_file.exists():
        return
    try:
        pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        profile.notes.append("package.json existe pero no se pudo leer.")
        return
    profile.stacks.append("node")
    profile.signals.append("package.json")
    scripts = pkg.get("scripts") or {}
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}

    pm = "pnpm" if "pnpm-lock.yaml" in names else "yarn" if "yarn.lock" in names else "npm"
    profile.signals.append(f"gestor de paquetes: {pm}")

    if "node_modules" in names:
        profile.commands["install"] = None
        profile.notes.append("node_modules ya existe: no se reinstalan dependencias.")
    else:
        profile.commands["install"] = [pm, "ci"] if pm == "npm" and "package-lock.json" in names else [pm, "install"]

    if "build" in scripts:
        profile.commands["build"] = [pm, "run", "build"]
    test_script = scripts.get("test", "")
    if test_script and not NPM_DEFAULT_TEST.search(test_script):
        profile.commands["test"] = [pm, "test"]
    if "lint" in scripts:
        profile.commands["lint"] = [pm, "run", "lint"]
    if pm == "npm":
        profile.commands["audit"] = ["npm", "audit", "--omit=dev"]

    web = {"next", "react", "vue", "svelte", "@angular/core", "vite", "nuxt", "astro"}
    api = {"express", "fastify", "@nestjs/core", "koa", "hono"}
    if deps.keys() & web:
        profile.kind = "web"
        profile.signals.append("framework web: " + ", ".join(sorted(deps.keys() & web)))
    elif deps.keys() & api:
        profile.kind = "api"
        profile.signals.append("framework de API: " + ", ".join(sorted(deps.keys() & api)))
    elif pkg.get("bin"):
        profile.kind = "cli"
    elif pkg.get("main") or pkg.get("exports"):
        profile.kind = "library"
    if profile.kind == "web":
        profile.web = _web_defaults(pm, scripts, deps)
        if profile.web.get("ignore"):
            profile.notes.append("Se detectó Vercel Analytics: el perfil ignora /_vercel/ (no existe fuera de Vercel).")
    if not profile.commands.get("test"):
        profile.notes.append("El proyecto Node no define tests automatizados.")


def _web_defaults(pm: str, scripts: dict, deps: dict) -> dict:
    """Configuración inicial del módulo web. `start` usa {port}: el motor elige un puerto libre."""
    start = None
    if "next" in deps and "start" in scripts:
        start = [pm, "run", "start", "--", "-p", "{port}"]
    elif "vite" in deps and "preview" in scripts:
        start = [pm, "run", "preview", "--", "--port", "{port}", "--host", "127.0.0.1"]
    elif "start" in scripts:
        start = [pm, "start"]  # el puerto se pasa por la variable PORT
    web = {"url": None, "start": start, "pages": ["/"], "flows": []}
    if deps.keys() & {"@vercel/analytics", "@vercel/speed-insights"}:
        # Los scripts de Vercel solo existen en el despliegue: en local dan 404 y no son un defecto.
        web["ignore"] = {"urls": ["/_vercel/"], "console": []}
    return web


def _python(root: Path, names: set, files: list, profile: Profile) -> None:
    markers = [n for n in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile") if n in names]
    if not markers:
        return
    profile.stacks.append("python")
    profile.signals.extend(markers)
    blob = ""
    for marker in markers:
        try:
            blob += (root / marker).read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            pass
    has_tests = any(
        p.name.startswith("test_") and p.suffix == ".py" or p.parent.name == "tests" and p.suffix == ".py"
        for p in files
    )
    if has_tests:
        uses_pytest = "pytest" in blob or "conftest.py" in names or "pytest.ini" in names
        if uses_pytest:
            profile.commands["test"] = ["python3", "-m", "pytest", "-q"]
        else:
            # `discover` sin -s no entra a una carpeta tests/ que no sea un paquete.
            start = ["-s", "tests"] if (root / "tests").is_dir() else []
            profile.commands["test"] = ["python3", "-m", "unittest", "discover", *start, "-v"]
        profile.signals.append("tests: " + ("pytest" if uses_pytest else "unittest"))
    else:
        profile.notes.append("El proyecto Python no tiene tests detectables.")
    if "[tool.ruff]" in blob or "ruff.toml" in names or ".ruff.toml" in names:
        profile.commands["lint"] = ["ruff", "check", "."]
    if "requirements.txt" in names:
        profile.commands["audit"] = ["pip-audit", "-r", "requirements.txt"]
    profile.commands.setdefault("install", None)
    profile.notes.append("Python: se asume el entorno ya preparado (no se instalan dependencias).")
    if profile.kind == "unknown":
        if {"fastapi", "flask", "django"} & set(re.findall(r"[a-z0-9_-]+", blob)):
            profile.kind = "api"
        else:
            profile.kind = "library"


def _go(root: Path, names: set, files: list, profile: Profile) -> None:
    if "go.mod" not in names:
        return
    profile.stacks.append("go")
    profile.signals.append("go.mod")
    profile.commands.update({"build": ["go", "build", "./..."], "test": ["go", "test", "./..."], "lint": ["go", "vet", "./..."]})
    if profile.kind == "unknown":
        profile.kind = "library"


def _dotnet(root: Path, names: set, files: list, profile: Profile) -> None:
    projects = [p for p in files if p.suffix in (".csproj", ".sln")]
    if not projects:
        return
    profile.stacks.append("dotnet")
    profile.signals.append(projects[0].name)
    profile.commands.update({"build": ["dotnet", "build"], "test": ["dotnet", "test"]})
    if profile.kind == "unknown":
        profile.kind = "library"


def _java(root: Path, names: set, files: list, profile: Profile) -> None:
    if "pom.xml" in names:
        profile.stacks.append("java")
        profile.signals.append("pom.xml")
        profile.commands.update({"build": ["mvn", "-q", "-DskipTests", "package"], "test": ["mvn", "-q", "test"]})
    elif {"build.gradle", "build.gradle.kts"} & names:
        profile.stacks.append("java")
        profile.signals.append("gradle")
        gradle = "./gradlew" if "gradlew" in names else "gradle"
        profile.commands.update({"build": [gradle, "build", "-x", "test"], "test": [gradle, "test"]})
    else:
        return
    if profile.kind == "unknown":
        profile.kind = "api"


def _odoo(root: Path, names: set, files: list, profile: Profile) -> None:
    manifests = [p for p in files if p.name == "__manifest__.py"]
    if not manifests:
        return
    profile.stacks.append("odoo")
    profile.kind = "odoo-module"
    profile.signals.append(f"{len(manifests)} módulo(s) Odoo (__manifest__.py)")
    profile.notes.append(
        "Odoo necesita una instancia y una base de datos de pruebas: instalar el módulo y correr sus tests "
        "no está automatizado todavía. Solo aplican los chequeos estáticos y de Python."
    )


def _cross_cutting(root: Path, names: set, files: list, profile: Profile) -> None:
    if "Dockerfile" in names or any(n.startswith("docker-compose") for n in names):
        profile.signals.append("Docker")
        profile.notes.append("Hay Docker: candidato para ejecutar los chequeos dentro de un sandbox.")
    if any(p.name in ("openapi.yaml", "openapi.yml", "openapi.json", "swagger.json") for p in files):
        profile.signals.append("OpenAPI")
        profile.notes.append("Hay especificación OpenAPI: se pueden generar pruebas de contrato (módulo pendiente).")
    if not (root / ".git").exists():
        profile.notes.append("No es un repositorio git: no se puede calcular el diff ni listar archivos versionados.")
    # Los comandos ausentes quedan explícitos en el perfil para que la persona los complete.
    for key in ("install", "build", "test", "lint", "audit"):
        profile.commands.setdefault(key, None)
    for key, cmd in list(profile.commands.items()):
        if cmd and shutil.which(cmd[0]) is None:
            profile.notes.append(f"La herramienta '{cmd[0]}' (paso '{key}') no está instalada en esta máquina.")

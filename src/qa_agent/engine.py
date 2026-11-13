"""Orquestador: ejecuta los chequeos registrados sobre un perfil y arma el informe."""
from __future__ import annotations

import subprocess
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import analyst, store
from .checks import REGISTRY, web
from .models import ERROR, CheckResult, Context, Profile
from .report import Report


def git_diff(project: Path, base: Optional[str]) -> Optional[str]:
    """Diff contra `base` (p. ej. origin/main) o, sin base, los cambios sin commitear."""
    args = ["diff", f"{base}...HEAD"] if base else ["diff", "HEAD"]
    try:
        res = subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 and res.stdout.strip() else None


def execute(
    profile: Profile,
    project_dir: Path,
    run_dir: Path,
    ticket: Optional[str] = None,
    diff_base: Optional[str] = None,
    use_analyst: bool = False,
    llm_client: Optional[Any] = None,
    extra_results: tuple[CheckResult, ...] = (),
) -> Report:
    """Núcleo común: lo usan tanto `run` (workspace local) como `ci` (repo clonado en un runner)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = Context(profile, project_dir, run_dir, ticket=ticket, diff=git_diff(project_dir, diff_base))

    results: list[CheckResult] = list(extra_results)
    with ExitStack() as stack:  # el servidor de la app (si hace falta) se cierra siempre al terminar
        web_ready = False
        for entry in REGISTRY:
            if not entry.applies(profile):
                continue
            if entry.needs_web and not web_ready:
                web_ready = True  # se intenta una sola vez por corrida
                if profile.approved:
                    try:
                        ctx.base_url = stack.enter_context(web.target(ctx))
                    except web.WebUnavailable as exc:
                        ctx.web_error = str(exc)
            try:
                results.append(entry.fn(ctx))
            except Exception as exc:  # un chequeo roto no debe tumbar todo el informe
                results.append(CheckResult(entry.name, ERROR, f"error interno del chequeo: {exc!r}", "normal"))
    if use_analyst:
        try:
            results.append(analyst.analyze(ctx, client=llm_client))
        except Exception as exc:
            results.append(CheckResult("analyst", ERROR, f"falló el analista: {exc!r}", "info", advisory=True))

    report = Report(profile, results)
    report.save(run_dir)
    return report


def run_qa(
    company: str,
    project: str,
    ticket: Optional[str] = None,
    diff_base: Optional[str] = None,
    use_analyst: bool = False,
    llm_client: Optional[Any] = None,
) -> tuple[Report, Path]:
    profile = store.load_profile(company, project)
    run_dir = store.project_dir(company, project) / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    report = execute(profile, Path(profile.path), run_dir, ticket, diff_base, use_analyst, llm_client)
    return report, run_dir

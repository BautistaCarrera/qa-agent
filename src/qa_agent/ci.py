"""Modo CI: corre el agente dentro de un runner (GitHub Actions) sobre el repo recién clonado.

Diseño de seguridad (lo importante de este módulo):
- El perfil vive en el propio repo: `.qa-agent/profile.yaml`. Que alguien lo apruebe = que lo revise y
  fusione en un Pull Request (`approved: true`).
- En un Pull Request se usa el perfil de la **rama base** (ya fusionado), nunca el de la rama del PR.
  Si no, quien abre el PR podría cambiar los comandos aprobados en ese mismo PR y ejecutar lo que quiera.
  Si el PR modifica el perfil, el informe lo avisa: el cambio rige recién cuando se fusiona.
- Los comandos del perfil corren con el entorno reducido de `runner.clean_env` (sin tokens ni secretos).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import yaml

from . import engine, onboarding
from .models import PASS, CheckResult, Profile
from .report import Report

PROFILE_REL = ".qa-agent/profile.yaml"
MARKER = "<!-- qa-agent-report -->"
COMMENT_LIMIT = 60_000  # GitHub acepta hasta 65 536 caracteres por comentario
FAIL_ON = ("block", "inconclusive", "never")


def _git_show(project: Path, ref: str, rel: str) -> Optional[str]:
    try:
        res = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=project, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def default_names(project: Path) -> tuple[str, str]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return owner, name
    return "local", project.resolve().name


def init_profile(project: Path, company: str, project_name: str) -> Path:
    """Genera un perfil borrador dentro del repo, sin aprobar. Una persona lo revisa y pone approved: true."""
    profile = onboarding.detect(project, company, project_name)
    profile.approved = False
    profile.path = "."  # relativo: en CI siempre es la carpeta del checkout
    out = project / PROFILE_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(profile.to_dict(), sort_keys=False, allow_unicode=True), encoding="utf-8")
    return out


def load_ci_profile(
    project: Path, company: str, project_name: str, profile_ref: Optional[str]
) -> tuple[Profile, list[CheckResult], list[str]]:
    """Devuelve (perfil a usar, resultados extra del informe, avisos para la consola)."""
    extra: list[CheckResult] = []
    warnings: list[str] = []
    head_file = project / PROFILE_REL
    head_text = head_file.read_text(encoding="utf-8") if head_file.exists() else None

    if profile_ref:  # Pull Request: manda el perfil de la rama base
        base_text = _git_show(project, profile_ref, PROFILE_REL)
        if head_text is not None and head_text != base_text:
            msg = (
                f"el PR modifica `{PROFILE_REL}`: se ignora hasta que se fusione "
                f"(se usa el de `{profile_ref}`)" if base_text else
                f"el PR agrega `{PROFILE_REL}`: no rige hasta que se fusione, por eso no se ejecutan comandos"
            )
            extra.append(CheckResult("profile-change", PASS, msg, "info", advisory=True, findings=[msg]))
            warnings.append(msg)
        text = base_text
    else:  # push a la rama principal u otro contexto: el archivo del checkout ya es código fusionado
        text = head_text

    if text is None:
        profile = onboarding.detect(project, company, project_name)
        profile.approved = False
        warnings.append(
            f"no hay `{PROFILE_REL}` aprobado: solo corren los chequeos estáticos. "
            "Generá un borrador con `qa-agent ci --init` y proponelo en un PR."
        )
    else:
        profile = Profile.from_dict(yaml.safe_load(text))
        if not profile.approved:
            warnings.append(f"`{PROFILE_REL}` existe pero tiene `approved: false`: no se ejecutan comandos del proyecto.")
    profile.path = str(project.resolve())  # el checkout manda, no la ruta guardada
    profile.company, profile.project = company, project_name
    return profile, extra, warnings


def comment_body(report: Report) -> str:
    body = report.to_markdown()
    if len(body) > COMMENT_LIMIT:
        body = body[:COMMENT_LIMIT] + "\n\n_… informe recortado: el completo está en los artefactos del job._\n"
    return f"{MARKER}\n{body}"


def exit_code(verdict: str, fail_on: str) -> int:
    if fail_on == "never":
        return 0
    if verdict == "BLOCK":
        return 1
    if verdict == "INCONCLUSIVE" and fail_on == "inconclusive":
        return 3
    return 0


def run_ci(
    project: Path,
    output_dir: Path,
    company: Optional[str] = None,
    project_name: Optional[str] = None,
    profile_ref: Optional[str] = None,
    diff_base: Optional[str] = None,
    ticket: Optional[str] = None,
    use_analyst: bool = False,
    fail_on: str = "block",
    llm_client=None,
) -> tuple[Report, int, list[str]]:
    if fail_on not in FAIL_ON:
        raise ValueError(f"--fail-on debe ser uno de {', '.join(FAIL_ON)}")
    d_company, d_project = default_names(project)
    profile, extra, warnings = load_ci_profile(project, company or d_company, project_name or d_project, profile_ref)
    report = engine.execute(
        profile, project.resolve(), output_dir, ticket=ticket, diff_base=diff_base, use_analyst=use_analyst,
        llm_client=llm_client, extra_results=tuple(extra),
    )
    (output_dir / "comment.md").write_text(comment_body(report), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report.to_markdown() + "\n")
    return report, exit_code(report.verdict, fail_on), warnings

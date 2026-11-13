"""Chequeos universales: valen para cualquier proyecto, sin importar el stack."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..models import ERROR, FAIL, PASS, SKIPPED, CheckResult, Context
from ..runner import run_command

SECRET_PATTERNS = {
    "clave de acceso AWS": re.compile(r"AKIA[0-9A-Z]{16}"),
    "clave privada": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----"),
    "token de GitHub": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "token de Slack": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "clave de API de Google": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "clave tipo sk-": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "credencial asignada": re.compile(
        r"""(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*['"]([^'"\s]{12,})['"]"""
    ),
}
PLACEHOLDER = re.compile(r"(?i)example|your|xxx|changeme|placeholder|<|\$\{|process\.env|dummy|test")
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".woff", ".woff2", ".zip", ".gz", ".lock", ".map"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
SKIP_DIRS = {"node_modules", ".git", ".next", "venv", ".venv", "dist", "build", "__pycache__"}
MAX_FILE_BYTES = 1_000_000


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _tracked_files(project: Path) -> tuple[list[Path], str]:
    """Archivos versionados si es un repo git; si no, recorre el árbol ignorando carpetas generadas."""
    res = _git(["ls-files", "-z"], project)
    if res and res.returncode == 0:
        return [project / p for p in res.stdout.split("\0") if p], "archivos versionados en git"
    files = [
        p for p in project.rglob("*")
        if p.is_file() and not (set(p.relative_to(project).parts) & SKIP_DIRS)
    ]
    return files, "todos los archivos (no es un repo git)"


def check_secrets(ctx: Context) -> CheckResult:
    files, scope = _tracked_files(ctx.project_dir)
    findings: list[str] = []
    scanned = 0
    for path in files:
        if path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_NAMES:
            continue
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        rel = path.relative_to(ctx.project_dir)
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in SECRET_PATTERNS.items():
                match = pattern.search(line)
                if not match:
                    continue
                value = match.group(1) if pattern.groups else match.group(0)
                if PLACEHOLDER.search(value):
                    continue
                # Nunca se imprime el valor: solo dónde está y de qué tipo es.
                findings.append(f"{rel}:{lineno} posible {label}")
    if findings:
        return CheckResult(
            "secrets", FAIL, f"{len(findings)} posible(s) secreto(s) en el código", "critical", findings=findings[:50]
        )
    return CheckResult("secrets", PASS, f"sin secretos evidentes ({scanned} archivos revisados: {scope})", "critical")


def check_env_hygiene(ctx: Context) -> CheckResult:
    res = _git(["ls-files"], ctx.project_dir)
    if not res or res.returncode != 0:
        return CheckResult("env-hygiene", SKIPPED, "no es un repositorio git", "critical")
    tracked_env = [
        f for f in res.stdout.splitlines()
        if re.search(r"(^|/)\.env(\..*)?$", f) and not f.endswith((".example", ".sample", ".template"))
    ]
    if tracked_env:
        return CheckResult(
            "env-hygiene", FAIL, "hay archivos .env versionados en git", "critical",
            findings=[f"{f} está versionado" for f in tracked_env],
        )
    return CheckResult("env-hygiene", PASS, "ningún .env está versionado", "critical")


def exec_check(key: str, severity: str):
    """Fábrica de chequeos que ejecutan un comando del perfil aprobado."""

    def check(ctx: Context) -> CheckResult:
        name = {"test": "tests", "audit": "dependency-audit"}.get(key, key)
        cmd = ctx.profile.commands.get(key)
        if not cmd:
            return CheckResult(name, SKIPPED, f"el perfil no define un comando '{key}'", severity)
        if not ctx.profile.approved:
            return CheckResult(name, SKIPPED, "perfil sin aprobar: no se ejecutan comandos del proyecto", severity)
        evidence = ctx.run_dir / "evidence" / f"{name}.log"
        result = run_command(cmd, ctx.project_dir, ctx.profile.timeouts.get(key, 300), evidence)
        shown = " ".join(cmd)
        if result.tool_missing:
            return CheckResult(name, SKIPPED, f"{result.tail} (no se pudo ejecutar `{shown}`)", severity, str(evidence))
        if result.timed_out:
            return CheckResult(name, ERROR, f"`{shown}` excedió el tiempo límite", severity, str(evidence),
                               result.duration, findings=[result.tail] if result.tail else [])
        if result.exit_code == 0:
            return CheckResult(name, PASS, f"`{shown}` terminó bien", severity, str(evidence), result.duration)
        return CheckResult(
            name, FAIL, f"`{shown}` terminó con código {result.exit_code}", severity, str(evidence),
            result.duration, findings=[result.tail] if result.tail else [],
        )

    return check

"""Ejecución controlada de comandos: timeout, entorno limpio y evidencia en disco.

Limitación conocida: esto NO es un sandbox. Corre los comandos del perfil aprobado en la carpeta
del proyecto, con las variables de entorno reducidas a una lista permitida (así no se filtran
secretos al código del proyecto). Para repos de terceros, ejecutá el agente dentro de Docker/VM.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL", "USER", "NVM_DIR", "VOLTA_HOME")
MAX_EVIDENCE_BYTES = 2_000_000


@dataclass
class RunResult:
    exit_code: Optional[int]
    timed_out: bool
    tool_missing: bool
    duration: float
    tail: str


def clean_env(extra: Optional[dict] = None) -> dict:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env["CI"] = "1"
    env["NO_COLOR"] = "1"
    if extra:
        env.update(extra)
    return env


def run_command(cmd: list[str], cwd: Path, timeout: int, evidence: Path) -> RunResult:
    evidence.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=clean_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,  # permite matar todo el árbol de procesos si se cuelga
        )
    except FileNotFoundError:
        evidence.write_text(f"$ {' '.join(cmd)}\nherramienta no encontrada: {cmd[0]}\n", encoding="utf-8")
        return RunResult(None, False, True, 0.0, f"herramienta no encontrada: {cmd[0]}")

    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
    duration = time.monotonic() - start

    out = out or ""
    truncated = len(out.encode("utf-8", "ignore")) > MAX_EVIDENCE_BYTES
    if truncated:
        out = out[-MAX_EVIDENCE_BYTES:]
    header = f"$ {' '.join(cmd)}\n(cwd: {cwd}; timeout: {timeout}s)\n"
    if truncated:
        header += "[salida truncada: se conservan los últimos 2 MB]\n"
    if timed_out:
        header += f"[TIMEOUT tras {timeout}s: proceso terminado]\n"
    evidence.write_text(header + "\n" + out, encoding="utf-8")
    tail = "\n".join(out.strip().splitlines()[-12:])
    return RunResult(None if timed_out else proc.returncode, timed_out, False, duration, tail)

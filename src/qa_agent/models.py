from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

Command = Optional[list[str]]

PASS, FAIL, SKIPPED, ERROR = "pass", "fail", "skipped", "error"


@dataclass
class Profile:
    """Perfil de QA de un proyecto. Lo genera el onboarding y lo aprueba una persona."""

    company: str
    project: str
    path: str
    kind: str = "unknown"  # web | api | library | cli | odoo-module | unknown
    stacks: list[str] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    timeouts: dict[str, int] = field(
        default_factory=lambda: {"install": 600, "build": 600, "test": 900, "lint": 300, "audit": 180}
    )
    web: dict = field(default_factory=dict)  # configuración del módulo web (url/start, páginas, umbrales, flujos)
    signals: list[str] = field(default_factory=list)  # evidencia de la detección
    notes: list[str] = field(default_factory=list)
    approved: bool = False  # sin aprobación humana no se ejecuta ningún comando del proyecto

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        return cls(**data)


@dataclass
class CheckResult:
    name: str
    status: str  # pass | fail | skipped | error
    summary: str
    severity: str = "normal"  # critical: un fallo bloquea | normal: genera observaciones
    evidence: Optional[str] = None  # ruta del log o artefacto que respalda el resultado
    duration: float = 0.0
    findings: list[str] = field(default_factory=list)
    advisory: bool = False  # los resultados asesores (LLM) nunca cambian el veredicto

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Context:
    profile: Profile
    project_dir: Path
    run_dir: Path
    ticket: Optional[str] = None
    diff: Optional[str] = None
    base_url: Optional[str] = None  # URL bajo prueba (la fija el motor al levantar el servidor)
    web_error: Optional[str] = None  # por qué no hay URL (se informa en los chequeos web)
    cache: dict = field(default_factory=dict)  # resultados compartidos entre chequeos de una misma corrida

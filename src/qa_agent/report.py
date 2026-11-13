"""Informe de QA: veredicto, nivel de confianza y, sobre todo, lo que NO se pudo verificar."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import ERROR, FAIL, PASS, SKIPPED, CheckResult, Profile

ICON = {PASS: "✅", FAIL: "❌", SKIPPED: "⏭️", ERROR: "⚠️"}


@dataclass
class Report:
    profile: Profile
    results: list[CheckResult]
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def decisive(self) -> list[CheckResult]:
        return [r for r in self.results if not r.advisory]

    @property
    def verdict(self) -> str:
        failed = [r for r in self.decisive if r.status in (FAIL, ERROR)]
        if any(r.severity == "critical" for r in failed):
            return "BLOCK"
        if failed:
            return "OBSERVATIONS"
        # Sin pruebas ejecutadas no hay base para aprobar: se declara no concluyente.
        return "INCONCLUSIVE" if self.confidence == "LOW" else "APPROVE"

    @property
    def not_verified(self) -> list[CheckResult]:
        return [r for r in self.decisive if r.status == SKIPPED]

    @property
    def confidence(self) -> str:
        # Evidencia funcional: tests automatizados o flujos de usuario que realmente se ejecutaron.
        functional = any(r.name in ("tests", "web-flows") and r.status in (PASS, FAIL) for r in self.decisive)
        ratio = sum(r.status in (PASS, FAIL) for r in self.decisive) / max(len(self.decisive), 1)
        if not functional:
            return "LOW"
        return "HIGH" if ratio >= 0.8 else "MEDIUM"

    def to_dict(self) -> dict:
        return {
            "started": self.started,
            "company": self.profile.company,
            "project": self.profile.project,
            "kind": self.profile.kind,
            "stacks": self.profile.stacks,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "results": [r.to_dict() for r in self.results],
        }

    def to_markdown(self) -> str:
        p = self.profile
        lines = [
            f"# Informe de QA: {p.company}/{p.project}",
            "",
            f"- **Veredicto:** {self.verdict}",
            f"- **Confianza:** {self.confidence}",
            f"- **Tipo / stack:** {p.kind} / {', '.join(p.stacks) or 'no reconocido'}",
            f"- **Fecha:** {self.started}",
            "",
            "| Chequeo | Resultado | Detalle |",
            "|---|---|---|",
        ]
        for r in self.results:
            tag = " (asesor)" if r.advisory else ""
            lines.append(f"| {r.name}{tag} | {ICON.get(r.status, '')} {r.status} | {r.summary} |")

        problems = [r for r in self.decisive if r.findings and r.status in (FAIL, ERROR)]
        if problems:
            lines += ["", "## Hallazgos"]
            for r in problems:
                lines += ["", f"### {r.name}"]
                lines += [f"- {f}" if "\n" not in f else f"```\n{f}\n```" for f in r.findings]
                if r.evidence:
                    lines.append(f"\nEvidencia: `{r.evidence}`")

        lines += ["", "## Lo que NO se verificó"]
        if self.not_verified:
            lines += [f"- **{r.name}**: {r.summary}" for r in self.not_verified]
        else:
            lines.append("- Todos los chequeos aplicables se ejecutaron.")
        if self.verdict == "INCONCLUSIVE":
            lines.append(
                "- **Sin pruebas ni flujos de usuario ejecutados**: no hay evidencia suficiente para aprobar. "
                "Aprobá el perfil, definí tests o flujos (`web.flows`) o revisá el proyecto a mano."
            )

        for r in self.results:
            if r.advisory:
                lines += ["", f"## {r.name} (asesor: no cambia el veredicto)", ""]
                lines += r.findings or [r.summary]

        lines += [
            "",
            "---",
            "*Informe automático y orientativo. La decisión de aprobar o bloquear es de una persona.*",
        ]
        return "\n".join(lines) + "\n"

    def save(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.md").write_text(self.to_markdown(), encoding="utf-8")
        (run_dir / "report.json").write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

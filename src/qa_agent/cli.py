"""Línea de comandos: onboard -> (revisar) -> approve -> run."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import ci, engine, onboarding, store


def _profile_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--company", required=True, help="Nombre de la empresa (aísla datos y perfiles)")
    p.add_argument("--project", required=True, help="Nombre del proyecto")


def cmd_onboard(args: argparse.Namespace) -> int:
    profile = onboarding.detect(Path(args.path), args.company, args.project)
    saved = store.save_profile(profile)
    print(f"Perfil generado: {saved}")
    print(f"  tipo: {profile.kind} | stack: {', '.join(profile.stacks) or 'no reconocido'}")
    for key, cmd in profile.commands.items():
        print(f"  {key:8} -> {' '.join(cmd) if cmd else '(no definido)'}")
    for note in profile.notes:
        print(f"  nota: {note}")
    print("\nRevisá el perfil y, si los comandos son correctos, aprobalo:")
    print(f"  qa-agent approve --company {args.company} --project {args.project}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    profile = store.load_profile(args.company, args.project)
    print("Vas a autorizar la ejecución de estos comandos dentro de:", profile.path)
    for key, cmd in profile.commands.items():
        if cmd:
            print(f"  {key:8} -> {' '.join(cmd)}")
    if not args.yes and input("¿Aprobar? [s/N] ").strip().lower() not in ("s", "si", "sí", "y", "yes"):
        print("No se aprobó nada.")
        return 1
    profile.approved = True
    store.save_profile(profile)
    print("Perfil aprobado.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ticket = Path(args.ticket).read_text(encoding="utf-8") if args.ticket else None
    report, run_dir = engine.run_qa(
        args.company, args.project, ticket=ticket, diff_base=args.diff_base, use_analyst=args.analyst
    )
    print(report.to_markdown())
    print(f"Informe y evidencia guardados en: {run_dir}")
    # Códigos de salida pensados para CI: 0 aprobado/observaciones, 1 bloqueado, 3 no concluyente.
    return {"BLOCK": 1, "INCONCLUSIVE": 3}.get(report.verdict, 0)


def cmd_ci(args: argparse.Namespace) -> int:
    project = Path(args.path)
    company, name = args.company, args.project
    if args.init:
        d_company, d_project = ci.default_names(project)
        out = ci.init_profile(project, company or d_company, name or d_project)
        print(f"Borrador creado: {out}")
        print("Revisalo, poné `approved: true` y proponelo en un Pull Request: esa revisión es la aprobación.")
        return 0
    ticket = Path(args.ticket).read_text(encoding="utf-8") if args.ticket else None
    report, code, warnings = ci.run_ci(
        project, Path(args.output_dir), company, name, profile_ref=args.profile_ref, diff_base=args.diff_base,
        ticket=ticket, use_analyst=args.analyst, fail_on=args.fail_on,
    )
    for w in warnings:
        print(f"::warning::{w}" if os.environ.get("GITHUB_ACTIONS") else f"Aviso: {w}")
    print(report.to_markdown())
    print(f"Veredicto: {report.verdict} (confianza {report.confidence}) | código de salida: {code}")
    return code


def cmd_list(_: argparse.Namespace) -> int:
    rows = store.list_profiles()
    if not rows:
        print("No hay proyectos registrados.")
    for company, project, approved in rows:
        print(f"{company}/{project}  [{'aprobado' if approved else 'pendiente de aprobación'}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qa-agent", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("onboard", help="Descubre el proyecto y genera su perfil de QA")
    p.add_argument("path", help="Carpeta del proyecto")
    _profile_args(p)
    p.set_defaults(func=cmd_onboard)

    p = sub.add_parser("approve", help="Aprueba el perfil (autoriza ejecutar sus comandos)")
    _profile_args(p)
    p.add_argument("--yes", action="store_true", help="No pedir confirmación")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("run", help="Ejecuta los chequeos y genera el informe")
    _profile_args(p)
    p.add_argument("--ticket", help="Archivo de texto con el ticket/requisitos")
    p.add_argument("--diff-base", help="Rama base para el diff (p. ej. origin/main)")
    p.add_argument("--analyst", action="store_true", help="Agrega el análisis con IA (requiere ANTHROPIC_API_KEY)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("ci", help="Modo CI: corre sobre un repo clonado y deja un comentario listo para el PR")
    p.add_argument("path", nargs="?", default=".", help="Carpeta del repo (por defecto, la actual)")
    p.add_argument("--company", help="Por defecto, el dueño del repo (GITHUB_REPOSITORY)")
    p.add_argument("--project", help="Por defecto, el nombre del repo")
    p.add_argument("--init", action="store_true", help="Crea .qa-agent/profile.yaml (borrador sin aprobar)")
    p.add_argument("--profile-ref", help="Rama base de la que se toma el perfil aprobado (en PR: origin/<base>)")
    p.add_argument("--diff-base", help="Rama base para el diff")
    p.add_argument("--ticket", help="Archivo de texto con el ticket/requisitos")
    p.add_argument("--analyst", action="store_true", help="Agrega el análisis con IA")
    p.add_argument("--output-dir", default="qa-report", help="Dónde guardar informe, evidencia y comment.md")
    p.add_argument("--fail-on", default="block", choices=ci.FAIL_ON,
                   help="block (default): falla solo si BLOCK; inconclusive: también si no hay evidencia; never")
    p.set_defaults(func=cmd_ci)

    p = sub.add_parser("list", help="Lista los proyectos registrados")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, NotADirectoryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

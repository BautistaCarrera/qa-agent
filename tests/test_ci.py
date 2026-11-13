import os
import subprocess
import sys
from pathlib import Path

import yaml

from qa_agent import ci, cli
from qa_agent.models import FAIL, PASS, SKIPPED, Profile
from .helpers import TmpCase

TEST_OK = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n"
TEST_BAD = TEST_OK.replace("assertTrue(True)", "assertTrue(False)")


def git(d, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=d, check=True, capture_output=True)


class CiCase(TmpCase):
    def repo(self, name="svc", test=TEST_OK, approved=True, commit_profile=True):
        d = self.make_project(name, {"requirements.txt": "\n", "tests/test_a.py": test})
        git(d, "init", "-q", "-b", "main")
        if commit_profile:
            out = ci.init_profile(d, "acme", name)
            data = yaml.safe_load(out.read_text())
            data["approved"] = approved
            out.write_text(yaml.safe_dump(data))
        git(d, "add", "-A")
        git(d, "commit", "-qm", "base")
        return d

    def run_ci(self, d, **kw):
        return ci.run_ci(d, self.root / "out", **kw)


class InitTests(CiCase):
    def test_init_crea_un_borrador_sin_aprobar_con_ruta_relativa(self):
        d = self.make_project("p", {"requirements.txt": "\n"})
        out = ci.init_profile(d, "acme", "p")
        data = yaml.safe_load(out.read_text())
        self.assertEqual(out, d / ".qa-agent" / "profile.yaml")
        self.assertFalse(data["approved"])
        self.assertEqual(data["path"], ".")


class ProfileSourceTests(CiCase):
    def test_perfil_aprobado_en_el_repo_ejecuta_y_pasa(self):
        d = self.repo()
        report, code, _ = self.run_ci(d)
        self.assertEqual(next(r for r in report.results if r.name == "tests").status, PASS)
        self.assertEqual(code, 0)

    def test_tests_rotos_bloquean_con_codigo_1(self):
        d = self.repo(test=TEST_BAD)
        report, code, _ = self.run_ci(d)
        self.assertEqual((report.verdict, code), ("BLOCK", 1))

    def test_sin_perfil_solo_chequeos_estaticos(self):
        d = self.repo(commit_profile=False)
        report, code, warnings = self.run_ci(d)
        self.assertEqual(next(r for r in report.results if r.name == "tests").status, SKIPPED)
        self.assertTrue(any("ci --init" in w for w in warnings))

    def test_perfil_sin_aprobar_no_ejecuta(self):
        d = self.repo(approved=False)
        report, _, warnings = self.run_ci(d)
        self.assertEqual(next(r for r in report.results if r.name == "tests").status, SKIPPED)
        self.assertTrue(any("approved: false" in w for w in warnings))

    def test_la_ruta_guardada_no_manda_manda_el_checkout(self):
        d = self.repo()
        profile, _, _ = ci.load_ci_profile(d, "acme", "svc", None)
        self.assertEqual(profile.path, str(d.resolve()))


class PullRequestSecurityTests(CiCase):
    def test_un_pr_no_puede_cambiar_los_comandos_aprobados(self):
        d = self.repo()
        pwned = d / "PWNED"
        git(d, "checkout", "-qb", "feature")
        pf = d / ci.PROFILE_REL
        data = yaml.safe_load(pf.read_text())
        data["commands"]["test"] = [sys.executable, "-c", f"open({str(pwned)!r}, 'w').write('x')"]
        pf.write_text(yaml.safe_dump(data))
        git(d, "commit", "-qam", "perfil malicioso")

        report, code, warnings = self.run_ci(d, profile_ref="main")
        self.assertFalse(pwned.exists(), "se ejecutó un comando que solo existía en la rama del PR")
        self.assertTrue(any("modifica" in w for w in warnings))
        self.assertIn("profile-change", [r.name for r in report.results])
        self.assertEqual(next(r for r in report.results if r.name == "tests").status, PASS)  # corrió el aprobado

    def test_un_pr_que_agrega_el_perfil_no_ejecuta_nada(self):
        d = self.repo(commit_profile=False)
        git(d, "checkout", "-qb", "feature")
        out = ci.init_profile(d, "acme", "svc")
        data = yaml.safe_load(out.read_text()); data["approved"] = True; out.write_text(yaml.safe_dump(data))
        git(d, "add", "-A"); git(d, "commit", "-qm", "agrega perfil")
        report, _, warnings = self.run_ci(d, profile_ref="main")
        self.assertEqual(next(r for r in report.results if r.name == "tests").status, SKIPPED)
        self.assertTrue(any("agrega" in w for w in warnings))


class OutputTests(CiCase):
    def test_exit_codes_segun_fail_on(self):
        self.assertEqual(ci.exit_code("BLOCK", "block"), 1)
        self.assertEqual(ci.exit_code("BLOCK", "never"), 0)
        self.assertEqual(ci.exit_code("INCONCLUSIVE", "block"), 0)
        self.assertEqual(ci.exit_code("INCONCLUSIVE", "inconclusive"), 3)
        self.assertEqual(ci.exit_code("APPROVE", "inconclusive"), 0)
        with self.assertRaises(ValueError):
            ci.run_ci(self.root, self.root / "o", fail_on="otra")

    def test_comment_md_tiene_marcador_y_se_acota(self):
        d = self.repo()
        report, _, _ = self.run_ci(d)
        text = (self.root / "out" / "comment.md").read_text()
        self.assertTrue(text.startswith(ci.MARKER))
        report.results[0].summary = "x" * (ci.COMMENT_LIMIT + 100)
        big = ci.comment_body(report)
        self.assertLessEqual(len(big), ci.COMMENT_LIMIT + 300)
        self.assertIn("recortado", big)

    def test_resumen_del_job_de_github(self):
        d = self.repo()
        summary = self.root / "summary.md"
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        self.addCleanup(os.environ.pop, "GITHUB_STEP_SUMMARY", None)
        self.run_ci(d)
        self.assertIn("Informe de QA", summary.read_text())

    def test_cli_ci_codigos_de_salida(self):
        d = self.repo(test=TEST_BAD)
        out = str(self.root / "cli-out")
        self.assertEqual(cli.main(["ci", str(d), "--output-dir", out]), 1)
        self.assertEqual(cli.main(["ci", str(d), "--output-dir", out, "--fail-on", "never"]), 0)
        self.assertEqual(cli.main(["ci", str(d), "--init"]), 0)


class WorkflowFileTests(TmpCase):
    ROOT = Path(__file__).resolve().parent.parent

    def load(self, rel):
        return yaml.safe_load((self.ROOT / rel).read_text())

    def test_workflow_reutilizable_es_valido(self):
        wf = self.load(".github/workflows/qa.yml")
        trigger = wf.get("on", wf.get(True))  # PyYAML lee `on` como True
        self.assertIn("workflow_call", trigger)
        job = wf["jobs"]["qa"]
        self.assertEqual(job["permissions"], {"contents": "read", "pull-requests": "write"})
        text = (self.ROOT / ".github/workflows/qa.yml").read_text()
        self.assertNotIn("run: qa-agent ci . ${{", text)
        # Ningún `run:` interpola datos del evento directamente: todo pasa por env.
        for step in job["steps"]:
            if "run" in step:
                self.assertNotIn("${{", step["run"], f"interpolación insegura en: {step.get('name')}")

    def test_ejemplo_de_llamada_es_valido(self):
        wf = self.load("examples/qa-caller.yml")
        self.assertIn("uses", wf["jobs"]["qa"])

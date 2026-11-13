from qa_agent import engine, onboarding, store
from qa_agent.checks import universal
from qa_agent.models import FAIL, PASS, SKIPPED, CheckResult, Context, Profile
from qa_agent.report import Report
from .helpers import TmpCase

FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"  # armado en dos partes para no disparar escáneres sobre este archivo


class SecretsTests(TmpCase):
    def ctx(self, d):
        return Context(Profile("acme", "p", str(d)), d, self.root / "run")

    def test_detecta_secreto_versionado_sin_imprimirlo(self):
        d = self.make_project("leak", {"config.py": f'KEY = "{FAKE_AWS}"\n'}, git=True)
        r = universal.check_secrets(self.ctx(d))
        self.assertEqual(r.status, FAIL)
        self.assertTrue(any("config.py:1" in f for f in r.findings))
        self.assertNotIn(FAKE_AWS, "".join(r.findings))

    def test_ignora_archivos_no_versionados(self):
        d = self.make_project("ok", {"a.py": "x = 1\n", ".gitignore": ".env\n"}, git=True)
        (d / ".env").write_text(f"KEY={FAKE_AWS}\n")
        self.assertEqual(universal.check_secrets(self.ctx(d)).status, PASS)

    def test_ignora_placeholders(self):
        d = self.make_project("ph", {"a.py": 'api_key = "your-api-key-here-123"\n'}, git=True)
        self.assertEqual(universal.check_secrets(self.ctx(d)).status, PASS)

    def test_env_versionado_falla(self):
        d = self.make_project("envp", {".env": "A=1\n"}, git=True)
        self.assertEqual(universal.check_env_hygiene(self.ctx(d)).status, FAIL)


class VerdictTests(TmpCase):
    def report(self, *results):
        return Report(Profile("a", "b", "/x"), list(results))

    def test_aprueba_con_confianza_alta(self):
        r = self.report(CheckResult("secrets", PASS, "", "critical"), CheckResult("tests", PASS, "", "critical"),
                        CheckResult("build", PASS, "", "critical"))
        self.assertEqual((r.verdict, r.confidence), ("APPROVE", "HIGH"))

    def test_bloquea_si_fallan_los_tests(self):
        r = self.report(CheckResult("tests", FAIL, "", "critical"))
        self.assertEqual(r.verdict, "BLOCK")

    def test_observaciones_si_falla_algo_no_critico(self):
        r = self.report(CheckResult("tests", PASS, "", "critical"), CheckResult("lint", FAIL, "", "normal"))
        self.assertEqual(r.verdict, "OBSERVATIONS")

    def test_sin_tests_es_no_concluyente_y_se_avisa(self):
        r = self.report(CheckResult("secrets", PASS, "", "critical"), CheckResult("tests", SKIPPED, "sin tests", "critical"))
        self.assertEqual((r.verdict, r.confidence), ("INCONCLUSIVE", "LOW"))
        self.assertIn("NO se verificó", r.to_markdown())

    def test_los_flujos_web_cuentan_como_evidencia_funcional(self):
        r = self.report(CheckResult("secrets", PASS, "", "critical"), CheckResult("web-flows", PASS, "", "critical"),
                        CheckResult("tests", SKIPPED, "sin tests", "critical"))
        self.assertEqual(r.verdict, "APPROVE")

    def test_el_asesor_no_cambia_el_veredicto(self):
        r = self.report(CheckResult("tests", PASS, "", "critical"),
                        CheckResult("analyst", FAIL, "x", "critical", advisory=True))
        self.assertEqual(r.verdict, "APPROVE")


class EndToEndTests(TmpCase):
    def flujo(self, test_body):
        d = self.make_project("svc", {"requirements.txt": "\n", "tests/test_a.py":
            f"import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        {test_body}\n"}, git=True)
        store.save_profile(onboarding.detect(d, "acme", "svc"))
        return d

    def test_sin_aprobar_no_se_ejecuta_nada(self):
        self.flujo("self.assertTrue(True)")
        report, run_dir = engine.run_qa("acme", "svc")
        tests = next(r for r in report.results if r.name == "tests")
        self.assertEqual(tests.status, SKIPPED)
        self.assertIn("sin aprobar", tests.summary)
        self.assertEqual(report.confidence, "LOW")

    def test_aprobado_y_pasando(self):
        self.flujo("self.assertTrue(True)")
        p = store.load_profile("acme", "svc"); p.approved = True; store.save_profile(p)
        report, run_dir = engine.run_qa("acme", "svc")
        self.assertEqual(next(r for r in report.results if r.name == "tests").status, PASS)
        self.assertTrue((run_dir / "report.md").exists() and (run_dir / "report.json").exists())
        self.assertTrue((run_dir / "evidence" / "tests.log").exists())

    def test_aprobado_y_fallando_bloquea(self):
        self.flujo("self.assertTrue(False)")
        p = store.load_profile("acme", "svc"); p.approved = True; store.save_profile(p)
        report, _ = engine.run_qa("acme", "svc")
        self.assertEqual(report.verdict, "BLOCK")

    def test_empresas_aisladas_y_nombres_seguros(self):
        d = self.flujo("pass")
        store.save_profile(onboarding.detect(d, "otra", "svc"))
        self.assertEqual({c for c, _, _ in store.list_profiles()}, {"acme", "otra"})
        with self.assertRaises(ValueError):
            store.profile_path("../escape", "x")


class InconclusiveTests(TmpCase):
    def test_sin_aprobar_el_veredicto_es_no_concluyente(self):
        d = self.make_project("svc2", {"requirements.txt": "\n", "tests/test_a.py": "x = 1\n"}, git=True)
        store.save_profile(onboarding.detect(d, "acme", "svc2"))
        report, _ = engine.run_qa("acme", "svc2")
        self.assertEqual(report.verdict, "INCONCLUSIVE")

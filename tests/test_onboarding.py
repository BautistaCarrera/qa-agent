import json

from qa_agent import onboarding
from .helpers import TmpCase


class OnboardingTests(TmpCase):
    def test_detecta_proyecto_web_node(self):
        d = self.make_project("web", {
            "package.json": json.dumps({
                "scripts": {"build": "next build", "lint": "eslint", "test": "echo \"Error: no test specified\" && exit 1"},
                "dependencies": {"next": "16", "react": "19"},
            }),
            "package-lock.json": "{}",
        })
        p = onboarding.detect(d, "acme", "web")
        self.assertEqual(p.kind, "web")
        self.assertIn("node", p.stacks)
        self.assertEqual(p.commands["build"], ["npm", "run", "build"])
        self.assertIsNone(p.commands["test"])  # el test por defecto de npm no cuenta
        self.assertEqual(p.commands["audit"], ["npm", "audit", "--omit=dev"])
        self.assertFalse(p.approved)

    def test_vercel_analytics_propone_ignorar_sus_scripts(self):
        d = self.make_project("va", {"package.json": json.dumps({
            "scripts": {"build": "x", "start": "x"}, "dependencies": {"next": "16", "@vercel/analytics": "1"}})})
        p = onboarding.detect(d, "acme", "va")
        self.assertEqual(p.web["ignore"]["urls"], ["/_vercel/"])
        self.assertTrue(any("Vercel Analytics" in n for n in p.notes))

    def test_sin_vercel_no_se_ignora_nada(self):
        d = self.make_project("nv", {"package.json": json.dumps({
            "scripts": {"build": "x", "start": "x"}, "dependencies": {"next": "16"}})})
        self.assertNotIn("ignore", onboarding.detect(d, "acme", "nv").web)

    def test_no_reinstala_si_node_modules_existe(self):
        d = self.make_project("web2", {"package.json": json.dumps({"scripts": {"build": "x"}})})
        (d / "node_modules").mkdir()
        self.assertIsNone(onboarding.detect(d, "acme", "web2").commands["install"])

    def test_detecta_python_con_unittest(self):
        d = self.make_project("py", {"requirements.txt": "requests\n", "tests/test_a.py": "x=1\n"})
        p = onboarding.detect(d, "acme", "py")
        self.assertEqual(p.stacks, ["python"])
        self.assertEqual(p.commands["test"][:3], ["python3", "-m", "unittest"])

    def test_detecta_modulo_odoo(self):
        d = self.make_project("odoo", {"mi_modulo/__manifest__.py": "{'name': 'x'}\n"})
        p = onboarding.detect(d, "acme", "odoo")
        self.assertEqual(p.kind, "odoo-module")
        self.assertTrue(any("Odoo" in n for n in p.notes))

    def test_proyecto_desconocido_no_inventa_comandos(self):
        d = self.make_project("raro", {"notas.txt": "hola\n"})
        p = onboarding.detect(d, "acme", "raro")
        self.assertEqual(p.kind, "unknown")
        self.assertTrue(all(v is None for v in p.commands.values()))
        self.assertTrue(any("No se reconoció el stack" in n for n in p.notes))

    def test_carpeta_inexistente(self):
        with self.assertRaises(NotADirectoryError):
            onboarding.detect(self.root / "no-existe", "acme", "x")

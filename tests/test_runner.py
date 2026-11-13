import os
import sys
import time

from qa_agent.runner import run_command
from .helpers import TmpCase


class RunnerTests(TmpCase):
    def test_ejecuta_y_guarda_evidencia(self):
        ev = self.root / "ev.log"
        r = run_command([sys.executable, "-c", "print('hola')"], self.root, 20, ev)
        self.assertEqual(r.exit_code, 0)
        self.assertIn("hola", ev.read_text())

    def test_timeout_mata_el_proceso(self):
        t0 = time.monotonic()
        r = run_command([sys.executable, "-c", "import time; time.sleep(30)"], self.root, 1, self.root / "e.log")
        self.assertTrue(r.timed_out)
        self.assertLess(time.monotonic() - t0, 10)

    def test_herramienta_inexistente(self):
        r = run_command(["herramienta-que-no-existe-xyz"], self.root, 5, self.root / "e.log")
        self.assertTrue(r.tool_missing)

    def test_el_entorno_no_filtra_secretos(self):
        os.environ["MI_SECRETO_FALSO"] = "valor-super-secreto"
        self.addCleanup(os.environ.pop, "MI_SECRETO_FALSO", None)
        ev = self.root / "e.log"
        run_command([sys.executable, "-c", "import os; print(os.environ.get('MI_SECRETO_FALSO'))"], self.root, 20, ev)
        self.assertNotIn("valor-super-secreto", ev.read_text())

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TmpCase(unittest.TestCase):
    """Cada prueba usa un workspace y proyectos temporales: nada toca tus carpetas reales."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        os.environ["QA_AGENT_HOME"] = str(self.workspace)
        self.addCleanup(os.environ.pop, "QA_AGENT_HOME", None)

    def make_project(self, name, files, git=False):
        d = self.root / name
        d.mkdir()
        for rel, content in files.items():
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        if git:
            for args in (["init", "-q"], ["add", "-A"],
                         ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
                subprocess.run(["git", *args], cwd=d, check=True, capture_output=True)
        return d

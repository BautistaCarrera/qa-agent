import json
import socket
import sys
import time
import unittest

from qa_agent import engine, store
from qa_agent.checks import web
from qa_agent.models import FAIL, PASS, SKIPPED, Context, Profile
from .helpers import TmpCase

GOOD = """<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Sitio de prueba</title><link rel="icon" href="data:,"></head>
<body><h1>Hola</h1><nav><a href="#contacto">Contacto</a> <a href="/otra.html">Otra</a></nav>
<img alt="logo" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">
<section id="contacto"><button id="abrir" onclick="document.getElementById('m').hidden=false">Abrir</button>
<div id="m" hidden>Menú abierto</div></section></body></html>"""
LAZY = ("<!doctype html><html lang='es'><head><title>Lazy</title><link rel='icon' href='data:,'></head><body><h1>Lazy</h1>"
        "<div style='height:4000px'></div><img alt='tarde' loading='lazy' src='/img.gif'></body></html>")
OTHER = '<!doctype html><html lang="es"><head><title>Otra</title><link rel="icon" href="data:,"></head><body><h1>Otra</h1></body></html>'
BAD = """<!doctype html><html><head></head><body>
<h1>Uno</h1><h1>Dos</h1><a href="#no-existe">Roto</a> <a href="/falta.html">Link muerto</a> <a href="/o"></a>
<img src="/no-carga.png"><div style="width:3000px">ancho</div>
<script>console.error('boom de consola')</script></body></html>"""


def playwright_ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return web.chrome_path() is not None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WebCase(TmpCase):
    def site(self, name, pages):
        d = self.make_project(name, pages)
        profile = Profile("acme", name, str(d), kind="web", approved=True)
        profile.web = {"start": [sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"],
                       "pages": ["/"], "ready_timeout": 20}
        return d, profile

    def ctx(self, d, profile):
        return Context(profile, d, self.root / "run")


class ParseLighthouseTests(unittest.TestCase):
    def test_puntajes_metricas_y_umbrales(self):
        raw = json.dumps({
            "categories": {"performance": {"score": 0.72}, "seo": {"score": 1.0}},
            "audits": {"largest-contentful-paint": {"displayValue": "2.4 s"}},
        })
        scores, metrics, below = web.parse_lighthouse(raw, {"performance": 80, "seo": 90})
        self.assertEqual(scores, {"performance": 72, "seo": 100})
        self.assertEqual(metrics, {"LCP": "2.4 s"})
        self.assertEqual(below, ["performance: 72 (umbral 80)"])


class TargetTests(WebCase):
    def test_levanta_la_app_y_la_cierra(self):
        d, profile = self.site("t1", {"index.html": GOOD})
        ctx = self.ctx(d, profile)
        with web.target(ctx) as url:
            port = int(url.rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=3):
                pass
        time.sleep(0.5)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)

    def test_sin_url_ni_start(self):
        d, profile = self.site("t2", {"index.html": GOOD})
        profile.web = {}
        with self.assertRaises(web.WebUnavailable):
            with web.target(self.ctx(d, profile)):
                pass

    def test_comando_que_muere_se_informa(self):
        d, profile = self.site("t3", {"index.html": GOOD})
        profile.web = {"start": [sys.executable, "-c", "raise SystemExit(3)"], "ready_timeout": 10}
        with self.assertRaises(web.WebUnavailable):
            with web.target(self.ctx(d, profile)):
                pass

    def test_url_externa_no_levanta_nada(self):
        d, profile = self.site("t4", {"index.html": GOOD})
        profile.web = {"url": "https://ejemplo.test/"}
        with web.target(self.ctx(d, profile)) as url:
            self.assertEqual(url, "https://ejemplo.test")


class SkipTests(WebCase):
    def test_sin_aprobar_no_se_navega(self):
        d, profile = self.site("s1", {"index.html": GOOD})
        profile.approved = False
        for check in (web.check_web_load, web.check_web_quality, web.check_lighthouse):
            self.assertEqual(check(self.ctx(d, profile)).status, SKIPPED)

    def test_sin_url_se_informa_el_motivo(self):
        d, profile = self.site("s2", {"index.html": GOOD})
        ctx = self.ctx(d, profile)
        ctx.web_error = "la app no arrancó"
        self.assertIn("la app no arrancó", web.check_web_load(ctx).summary)

    def test_flujo_con_accion_no_permitida_se_rechaza(self):
        self.assertIn("inválido", web._validate_flow({"steps": [{"evaluate": "alert(1)"}]}))
        self.assertIn("inválido", web._validate_flow({"steps": [{"click": "a", "goto": "/"}]}))
        self.assertIsNone(web._validate_flow({"steps": [{"goto": "/"}, {"click": "a"}]}))


@unittest.skipUnless(playwright_ready(), "requiere Playwright y Chrome (usá .venv/bin/python)")
class BrowserTests(WebCase):
    def run_with_site(self, pages, fn):
        d, profile = self.site("b", pages)
        ctx = self.ctx(d, profile)
        with web.target(ctx) as url:
            ctx.base_url = url
            return fn(ctx)

    def test_sitio_sano_pasa_carga_y_calidad(self):
        def go(ctx):
            return web.check_web_load(ctx), web.check_web_quality(ctx)
        load, quality = self.run_with_site({"index.html": GOOD, "otra.html": OTHER}, go)
        self.assertEqual(load.status, PASS, load.findings)
        self.assertEqual(quality.status, PASS, quality.findings)

    def test_sitio_roto_se_detecta(self):
        quality = self.run_with_site({"index.html": BAD}, web.check_web_quality)
        self.assertEqual(quality.status, FAIL)
        text = " | ".join(quality.findings)
        for expected in ("consola", "no tiene <title>", "lang", "2 elementos h1", "scroll horizontal",
                         "anclas sin destino", "imágenes que no cargan", "imágenes sin alt",
                         "sin nombre accesible", "enlaces internos rotos"):
            self.assertIn(expected, text, f"no se detectó: {expected}")
        self.assertTrue(list((self.root / "run" / "evidence").glob("web-*.png")))

    def test_imagen_diferida_no_es_un_falso_positivo(self):
        import base64
        gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
        d, profile = self.site("blazy", {"index.html": LAZY})
        (d / "img.gif").write_bytes(gif)
        ctx = self.ctx(d, profile)
        with web.target(ctx) as url:
            ctx.base_url = url
            quality = web.check_web_quality(ctx)
        self.assertEqual(quality.status, PASS, quality.findings)

    def test_pagina_inexistente_bloquea(self):
        d, profile = self.site("b404", {"index.html": GOOD})
        profile.web["pages"] = ["/no-existe.html"]
        ctx = self.ctx(d, profile)
        with web.target(ctx) as url:
            ctx.base_url = url
            load = web.check_web_load(ctx)
        self.assertEqual((load.status, load.severity), (FAIL, "critical"))

    def test_flujos(self):
        d, profile = self.site("bflow", {"index.html": GOOD})
        profile.web["flows"] = [
            {"name": "abre el menú", "steps": [{"goto": "/"}, {"click": "#abrir"}, {"expect_text": ["#m", "Menú abierto"]}]},
        ]
        ctx = self.ctx(d, profile)
        with web.target(ctx) as url:
            ctx.base_url = url
            ok = web.check_web_flows(ctx)
            profile.web["flows"] = [{"name": "roto", "steps": [{"goto": "/"}, {"click": "#no-existe"}]}]
            bad = web.check_web_flows(ctx)
        self.assertEqual(ok.status, PASS, ok.findings)
        self.assertEqual((bad.status, bad.severity), (FAIL, "critical"))
        self.assertIn("paso 2", bad.findings[0])

    def test_motor_extremo_a_extremo_y_cierra_el_servidor(self):
        d, profile = self.site("be2e", {"index.html": GOOD, "otra.html": OTHER})
        profile.web["lighthouse"] = {"enabled": False}
        store.save_profile(profile)
        report, _ = engine.run_qa("acme", "be2e")
        by = {r.name: r for r in report.results}
        self.assertEqual(by["web-load"].status, PASS, by["web-load"].findings)
        self.assertEqual(by["web-quality"].status, PASS, by["web-quality"].findings)
        self.assertEqual(by["lighthouse"].status, SKIPPED)
        self.assertEqual(by["web-flows"].status, SKIPPED)

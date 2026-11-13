"""Módulo web: smoke y calidad con Playwright, flujos declarativos y Lighthouse.

Todo es opcional y se activa solo para proyectos de tipo `web` con la sección `web` en el perfil:

    web:
      url: null                      # URL ya desplegada (staging). Si es null se usa `start`.
      start: [npm, run, start, --, -p, "{port}"]   # comando que levanta la app ({port} = puerto libre)
      pages: ["/"]                   # rutas a revisar
      thresholds: {performance: 80, accessibility: 90, best-practices: 90, seo: 90}
      lighthouse: {enabled: true, presets: [mobile, desktop]}
      ignore:                        # ruido conocido, declarado y revisado en el perfil (se informa cuánto se ignoró)
        urls: ["/_vercel/"]          # recursos/peticiones cuya URL contenga alguno de estos textos
        console: []                  # errores de consola cuyo texto contenga alguno de estos textos
      flows:                         # pasos declarativos (nunca JavaScript arbitrario)
        - name: el menú lleva a Proyectos
          steps:
            - {goto: "/"}
            - {click: "nav >> text=Proyectos"}
            - {expect_url: "#projects"}
            # acciones: goto, click, fill, press, expect_visible, expect_hidden, expect_text, expect_url, wait

Navegar o ejecutar `start` exige perfil aprobado, igual que el resto de los comandos.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urljoin, urlparse

from ..models import ERROR, FAIL, PASS, SKIPPED, CheckResult, Context
from ..runner import clean_env, run_command

DEFAULT_THRESHOLDS = {"performance": 80, "accessibility": 90, "best-practices": 90, "seo": 90}
DEFAULT_VIEWPORTS = {"desktop": (1280, 800), "mobile": (390, 800)}
MAX_LINKS_CHECKED = 40
FLOW_ACTIONS = {"goto", "click", "fill", "press", "expect_visible", "expect_hidden", "expect_text", "expect_url", "wait"}


class WebUnavailable(Exception):
    """No hay una app web que probar (sin `url`/`start`, no arranca, etc.)."""


# --------------------------------------------------------------------------- configuración
def web_config(ctx: Context) -> dict:
    cfg = dict(ctx.profile.web or {})
    cfg.setdefault("url", None)
    cfg.setdefault("start", None)
    cfg.setdefault("ready_timeout", 60)
    cfg.setdefault("pages", ["/"])
    cfg.setdefault("flows", [])
    cfg["thresholds"] = {**DEFAULT_THRESHOLDS, **(cfg.get("thresholds") or {})}
    ign = cfg.get("ignore") or {}
    cfg["ignore"] = {"urls": list(ign.get("urls") or []), "console": list(ign.get("console") or [])}
    cfg["lighthouse"] = {"enabled": True, "presets": ["mobile", "desktop"], **(cfg.get("lighthouse") or {})}
    return cfg


def chrome_path() -> Optional[str]:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- servidor bajo prueba
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, timeout: int, proc: Optional[subprocess.Popen]) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return True
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    return False


@contextlib.contextmanager
def target(ctx: Context) -> Iterator[str]:
    """Devuelve la URL a probar: la `url` del perfil o una app levantada con `start`."""
    cfg = web_config(ctx)
    if cfg["url"]:
        yield str(cfg["url"]).rstrip("/")
        return
    if not cfg["start"]:
        raise WebUnavailable("el perfil web no define ni 'url' ni 'start'")
    port = _free_port()
    cmd = [str(c).replace("{port}", str(port)) for c in cfg["start"]]
    log_path = ctx.run_dir / "evidence" / "web-server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        log.flush()
        try:
            proc = subprocess.Popen(
                cmd, cwd=ctx.project_dir, env=clean_env({"PORT": str(port)}),
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise WebUnavailable(f"no se pudo ejecutar `{' '.join(cmd)}`: {exc}") from exc
        base = f"http://127.0.0.1:{port}"
        try:
            if not _wait_ready(base + "/", int(cfg["ready_timeout"]), proc):
                raise WebUnavailable(
                    f"la app no respondió en {cfg['ready_timeout']}s con `{' '.join(cmd)}` (log: {log_path})"
                )
            yield base
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)


def _unavailable(name: str, severity: str, ctx: Context) -> Optional[CheckResult]:
    if not ctx.profile.approved:
        return CheckResult(name, SKIPPED, "perfil sin aprobar: no se abre la app ni se navega", severity)
    if ctx.base_url is None:
        return CheckResult(name, SKIPPED, f"no hay app que probar: {ctx.web_error or 'sin URL'}", severity)
    return None


# --------------------------------------------------------------------------- Playwright
def _launch(pw):
    path = chrome_path()
    args = ["--no-sandbox"]
    if path:
        return pw.chromium.launch(executable_path=path, headless=True, args=args)
    return pw.chromium.launch(headless=True, args=args)  # requiere `playwright install chromium`


_PAGE_AUDIT_JS = """async () => {
  const nameOf = (e) => (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim();
  const imgs = [...document.images];
  // Las imágenes diferidas (loading="lazy") no son un error mientras no se descargaron: se fuerzan y se espera
  // su resultado (load o error) antes de juzgarlas.
  imgs.forEach(i => { if (i.loading === 'lazy') i.loading = 'eager'; });
  await Promise.all(imgs.filter(i => !i.complete).map(i => new Promise(done => {
    i.addEventListener('load', done, { once: true });
    i.addEventListener('error', done, { once: true });
    setTimeout(done, 10000);
  })));
  return {
    title: document.title.trim(),
    lang: document.documentElement.lang,
    h1: document.querySelectorAll('h1').length,
    hscroll: document.documentElement.scrollWidth > innerWidth + 1,
    brokenAnchors: [...document.querySelectorAll('a[href^="#"]')]
      .map(a => a.getAttribute('href')).filter(h => h.length > 1 && !document.getElementById(decodeURIComponent(h.slice(1)))).slice(0, 20),
    imagesBroken: imgs.filter(i => !(i.complete && i.naturalWidth > 0)).map(i => (i.currentSrc || i.src).slice(-60)),
    imagesNoAlt: imgs.filter(i => !i.hasAttribute('alt')).map(i => (i.currentSrc || i.src).slice(-60)),
    unnamed: [...document.querySelectorAll('a,button')].filter(e => !nameOf(e)).map(e => e.outerHTML.slice(0, 80)),
    links: [...new Set([...document.querySelectorAll('a[href]')].map(a => a.href))],
  };
}"""


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-") or "root"


def _scroll_through(page) -> None:
    """Recorre la página para disparar imágenes diferidas y animaciones al entrar en pantalla."""
    page.evaluate(
        """async () => { for (let y = 0; y < document.body.scrollHeight; y += 500) {
             scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); } scrollTo(0, 0); }"""
    )
    page.wait_for_timeout(600)


def run_smoke(ctx: Context) -> dict:
    """Una sola sesión de navegador alimenta `web-load` y `web-quality` (se cachea en el contexto)."""
    if "smoke" in ctx.cache:
        return ctx.cache["smoke"]
    out: dict = {"load": [], "quality": [], "evidence": [], "error": None, "pages_ok": 0, "ignored": 0}
    ctx.cache["smoke"] = out
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        out["error"] = "falta Playwright: pip install 'qa-agent[web]'"
        return out

    cfg = web_config(ctx)
    base = ctx.base_url
    host = urlparse(base).netloc
    evidence_dir = ctx.run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = _launch(pw)
            for vp_name, (width, height) in DEFAULT_VIEWPORTS.items():
                context = browser.new_context(
                    viewport={"width": width, "height": height}, is_mobile=vp_name == "mobile",
                    has_touch=vp_name == "mobile",
                )
                context.add_init_script(
                    "window.__csp=[];document.addEventListener('securitypolicyviolation',"
                    "e=>window.__csp.push(e.violatedDirective+' '+e.blockedURI));"
                )
                page = context.new_page()
                console_errors: list[tuple[str, str]] = []  # (texto, URL del origen)
                page_errors: list[str] = []
                bad_responses: list[str] = []
                failed: list[str] = []
                ign_urls, ign_console = cfg["ignore"]["urls"], cfg["ignore"]["console"]

                def is_ignored_url(url: str) -> bool:
                    return any(p in url for p in ign_urls)

                def on_console(m, acc=console_errors):
                    if m.type == "error":
                        acc.append((m.text, (m.location or {}).get("url", "")))

                def on_response(r, acc=bad_responses):
                    if r.status >= 400:
                        if is_ignored_url(r.url):
                            out["ignored"] += 1
                        else:
                            acc.append(f"{r.status} {r.url}")

                def on_failed(r, acc=failed):
                    if is_ignored_url(r.url):
                        out["ignored"] += 1
                    else:
                        acc.append(f"{r.url} ({r.failure})")

                page.on("console", on_console)
                page.on("pageerror", lambda e, acc=page_errors: acc.append(str(e)))
                page.on("response", on_response)
                page.on("requestfailed", on_failed)

                for path in cfg["pages"]:
                    label = f"[{vp_name}] {path}"
                    url = urljoin(base + "/", path.lstrip("/"))
                    console_errors.clear(); page_errors.clear(); bad_responses.clear(); failed.clear()
                    try:
                        resp = page.goto(url, wait_until="networkidle", timeout=45_000)
                    except PlaywrightError as exc:
                        out["load"].append(f"{label}: no cargó ({str(exc).splitlines()[0]})")
                        continue
                    if resp is None or resp.status >= 400:
                        out["load"].append(f"{label}: respondió {resp.status if resp else 'sin respuesta'}")
                        continue
                    _scroll_through(page)
                    if page_errors:
                        out["load"].append(f"{label}: error de JavaScript no capturado: {page_errors[0][:160]}")
                    else:
                        out["pages_ok"] += 1

                    audit = page.evaluate(_PAGE_AUDIT_JS)
                    csp = page.evaluate("window.__csp || []")
                    q = out["quality"]
                    kept = []
                    for text, src in console_errors:
                        # Una URL ignorada también cuenta si aparece dentro del texto del mensaje.
                        if is_ignored_url(src) or is_ignored_url(text) or any(p in text for p in ign_console):
                            out["ignored"] += 1
                        else:
                            kept.append(text)
                    if kept:
                        q.append(f"{label}: {len(kept)} error(es) en la consola, p. ej. «{kept[0][:140]}»")
                    if csp:
                        q.append(f"{label}: violaciones de CSP: {', '.join(sorted(set(csp))[:3])}")
                    own_bad = [r for r in bad_responses if host in r or r.split(" ", 1)[1].startswith(base)]
                    if own_bad:
                        q.append(f"{label}: recursos propios con error HTTP: {', '.join(own_bad[:3])}")
                    if failed:
                        q.append(f"{label}: peticiones fallidas: {failed[0][:140]}")
                    if not audit["title"]:
                        q.append(f"{label}: la página no tiene <title>")
                    if not audit["lang"]:
                        q.append(f"{label}: falta el atributo lang en <html>")
                    if audit["h1"] != 1:
                        q.append(f"{label}: hay {audit['h1']} elementos h1 (debería haber 1)")
                    if audit["hscroll"]:
                        q.append(f"{label}: scroll horizontal (desborde de contenido)")
                    if audit["brokenAnchors"]:
                        q.append(f"{label}: anclas sin destino: {', '.join(audit['brokenAnchors'][:5])}")
                    if audit["imagesBroken"]:
                        q.append(f"{label}: imágenes que no cargan: {', '.join(audit['imagesBroken'][:3])}")
                    if audit["imagesNoAlt"]:
                        q.append(f"{label}: imágenes sin alt: {', '.join(audit['imagesNoAlt'][:3])}")
                    if audit["unnamed"]:
                        q.append(f"{label}: {len(audit['unnamed'])} enlace(s)/botón(es) sin nombre accesible")

                    # Enlaces internos: deben responder (se acota la cantidad para no saturar al servidor).
                    internal = [
                        l for l in audit["links"]
                        if urlparse(l).netloc == host and urlparse(l).scheme in ("http", "https")
                    ]
                    dead = []
                    for link in sorted(set(l.split("#")[0] for l in internal))[:MAX_LINKS_CHECKED]:
                        try:
                            if context.request.get(link, timeout=15_000).status >= 400:
                                dead.append(link)
                        except PlaywrightError:
                            dead.append(link)
                    if dead:
                        q.append(f"{label}: enlaces internos rotos: {', '.join(dead[:5])}")

                    shot = evidence_dir / f"web-{vp_name}-{_slug(path)}.png"
                    page.screenshot(path=str(shot))
                    out["evidence"].append(str(shot))
                context.close()
            browser.close()
    except Exception as exc:  # navegador no disponible, caída de Playwright, etc.
        out["error"] = f"Playwright falló: {str(exc).splitlines()[0] if str(exc) else repr(exc)}"
    return out


def check_web_load(ctx: Context) -> CheckResult:
    if (res := _unavailable("web-load", "critical", ctx)):
        return res
    smoke = run_smoke(ctx)
    if smoke["error"] and not smoke["load"]:
        return CheckResult("web-load", SKIPPED, smoke["error"], "critical")
    evidence = smoke["evidence"][0] if smoke["evidence"] else None
    if smoke["load"]:
        return CheckResult("web-load", FAIL, f"{len(smoke['load'])} página(s) no cargan bien", "critical",
                           evidence, findings=smoke["load"])
    return CheckResult("web-load", PASS, f"{smoke['pages_ok']} carga(s) correcta(s) (escritorio y móvil)", "critical", evidence)


def check_web_quality(ctx: Context) -> CheckResult:
    if (res := _unavailable("web-quality", "normal", ctx)):
        return res
    smoke = run_smoke(ctx)
    if smoke["error"] and not smoke["quality"]:
        return CheckResult("web-quality", SKIPPED, smoke["error"], "normal")
    ign = web_config(ctx)["ignore"]
    note = ""
    if smoke["ignored"]:
        note = (f" ({smoke['ignored']} evento(s) conocidos ignorados por el perfil: "
                f"{', '.join(ign['urls'] + ign['console'])})")
    if smoke["quality"]:
        return CheckResult("web-quality", FAIL, f"{len(smoke['quality'])} problema(s) de calidad{note}", "normal",
                           findings=smoke["quality"])
    return CheckResult("web-quality", PASS,
                       "consola, red, enlaces, imágenes y accesibilidad básica sin problemas" + note, "normal")


# --------------------------------------------------------------------------- flujos declarativos
def _validate_flow(flow: dict) -> Optional[str]:
    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        return "el flujo no tiene 'steps'"
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict) or len(step) != 1 or next(iter(step)) not in FLOW_ACTIONS:
            return f"paso {i} inválido (acciones permitidas: {', '.join(sorted(FLOW_ACTIONS))})"
    return None


def _run_step(page, base: str, step: dict) -> None:
    (action, arg), = step.items()
    if action == "goto":
        page.goto(urljoin(base + "/", str(arg).lstrip("/")), wait_until="networkidle", timeout=45_000)
    elif action == "click":
        page.click(arg, timeout=10_000)
    elif action == "fill":
        selector, value = arg
        page.fill(selector, str(value), timeout=10_000)
    elif action == "press":
        page.keyboard.press(str(arg))
    elif action == "expect_visible":
        page.wait_for_selector(arg, state="visible", timeout=10_000)
    elif action == "expect_hidden":
        page.wait_for_selector(arg, state="hidden", timeout=10_000)
    elif action == "expect_text":
        selector, text = arg
        page.wait_for_function(
            "([s, t]) => (document.querySelector(s)?.innerText || '').includes(t)", arg=[selector, text], timeout=10_000
        )
    elif action == "expect_url":
        page.wait_for_function("(u) => location.href.includes(u)", arg=str(arg), timeout=10_000)
    elif action == "wait":
        page.wait_for_timeout(min(int(arg), 5000))


def check_web_flows(ctx: Context) -> CheckResult:
    cfg = web_config(ctx)
    if not cfg["flows"]:
        return CheckResult("web-flows", SKIPPED, "el perfil no define flujos (agregá `web.flows` para cubrir lo crítico)", "critical")
    if (res := _unavailable("web-flows", "critical", ctx)):
        return res
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return CheckResult("web-flows", SKIPPED, "falta Playwright: pip install 'qa-agent[web]'", "critical")

    failures: list[str] = []
    passed = 0
    evidence_dir = ctx.run_dir / "evidence"
    try:
        with sync_playwright() as pw:
            browser = _launch(pw)
            for flow in cfg["flows"]:
                name = flow.get("name", "flujo sin nombre")
                if (problem := _validate_flow(flow)):
                    failures.append(f"{name}: {problem}")
                    continue
                width, height = DEFAULT_VIEWPORTS.get(flow.get("viewport", "desktop"), DEFAULT_VIEWPORTS["desktop"])
                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()
                try:
                    for i, step in enumerate(flow["steps"], 1):
                        try:
                            _run_step(page, ctx.base_url, step)
                        except Exception as exc:
                            shot = evidence_dir / f"flow-{_slug(name)}-fallo.png"
                            with contextlib.suppress(Exception):
                                page.screenshot(path=str(shot))
                            failures.append(f"{name}: falló el paso {i} {json.dumps(step, ensure_ascii=False)}: "
                                            f"{str(exc).splitlines()[0][:140]}")
                            break
                    else:
                        passed += 1
                finally:
                    context.close()
            browser.close()
    except Exception as exc:
        return CheckResult("web-flows", ERROR, f"Playwright falló: {str(exc).splitlines()[0]}", "critical")
    if failures:
        return CheckResult("web-flows", FAIL, f"{len(failures)} de {len(cfg['flows'])} flujo(s) fallaron", "critical",
                           findings=failures)
    return CheckResult("web-flows", PASS, f"{passed} flujo(s) completados", "critical")


# --------------------------------------------------------------------------- Lighthouse
def parse_lighthouse(raw: str, thresholds: dict) -> tuple[dict, dict, list[str]]:
    """Devuelve (puntajes 0-100, métricas clave, categorías por debajo del umbral)."""
    data = json.loads(raw)
    scores = {k: round((v.get("score") or 0) * 100) for k, v in data["categories"].items()}
    audits = data.get("audits", {})
    metrics = {
        short: audits[key]["displayValue"]
        for short, key in (("FCP", "first-contentful-paint"), ("LCP", "largest-contentful-paint"),
                           ("TBT", "total-blocking-time"), ("CLS", "cumulative-layout-shift"))
        if key in audits and "displayValue" in audits[key]
    }
    below = [f"{cat}: {scores[cat]} (umbral {thr})" for cat, thr in thresholds.items() if cat in scores and scores[cat] < thr]
    return scores, metrics, below


def check_lighthouse(ctx: Context) -> CheckResult:
    cfg = web_config(ctx)
    if not cfg["lighthouse"]["enabled"]:
        return CheckResult("lighthouse", SKIPPED, "desactivado en el perfil", "normal")
    if (res := _unavailable("lighthouse", "normal", ctx)):
        return res
    chrome = chrome_path()
    if not chrome:
        return CheckResult("lighthouse", SKIPPED, "no se encontró Chrome/Chromium en esta máquina", "normal")
    if shutil.which("npx") is None:
        return CheckResult("lighthouse", SKIPPED, "falta npx (Node.js) para ejecutar Lighthouse", "normal")

    url = urljoin(ctx.base_url + "/", cfg["pages"][0].lstrip("/"))
    lines, failures, notes = [], [], []
    for preset in cfg["lighthouse"]["presets"]:
        out_file = ctx.run_dir / "evidence" / f"lighthouse-{preset}.json"
        cmd = ["npx", "-y", "lighthouse@12", url, "--output=json", f"--output-path={out_file}", "--quiet",
               f"--chrome-path={chrome}", "--chrome-flags=--headless=new --no-sandbox"]
        if preset == "desktop":
            cmd.append("--preset=desktop")
        result = run_command(cmd, ctx.project_dir, 300, ctx.run_dir / "evidence" / f"lighthouse-{preset}.log")
        if result.tool_missing or result.timed_out or not out_file.exists():
            return CheckResult("lighthouse", ERROR if not result.tool_missing else SKIPPED,
                               f"Lighthouse no pudo ejecutarse ({preset}): {result.tail[-200:] or 'sin salida'}",
                               "normal", str(ctx.run_dir / "evidence" / f"lighthouse-{preset}.log"))
        try:
            scores, metrics, below = parse_lighthouse(out_file.read_text(encoding="utf-8"), cfg["thresholds"])
        except (ValueError, KeyError) as exc:
            return CheckResult("lighthouse", ERROR, f"no se pudo leer el informe ({preset}): {exc!r}", "normal", str(out_file))
        lines.append(f"{preset}: " + ", ".join(f"{k} {v}" for k, v in scores.items())
                     + (f" | {', '.join(f'{k} {v}' for k, v in metrics.items())}" if metrics else ""))
        failures += [f"[{preset}] {b}" for b in below]
    notes.append("Lighthouse varía entre corridas (el rendimiento móvil puede moverse ±5 puntos).")
    if failures:
        return CheckResult("lighthouse", FAIL, f"{len(failures)} categoría(s) bajo el umbral", "normal",
                           findings=failures + lines + notes)
    return CheckResult("lighthouse", PASS, " · ".join(lines), "normal", findings=notes)

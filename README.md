# qa-agent

Agente de QA **agnóstico al stack**: descubre qué es cada proyecto, ejecuta chequeos y emite un informe
con veredicto, **nivel de confianza** y la lista de lo que **no** pudo verificar. Pensado para que
aparezcan proyectos nuevos de distintas empresas sin reescribir nada.

## Flujo

```
onboard  ->  (una persona revisa)  ->  approve  ->  run
```

1. **onboard**: inspecciona la carpeta y genera un perfil (`workspace/<empresa>/<proyecto>/profile.yaml`)
   con el tipo de proyecto, el stack y los comandos de build/test/lint/audit. No inventa nada: lo que no
   reconoce queda como `null` y como nota.
2. **approve**: una persona revisa los comandos y los autoriza. **Sin aprobación no se ejecuta ningún
   comando del proyecto** (solo corren los chequeos estáticos, que únicamente leen).
3. **run**: ejecuta los chequeos y guarda `report.md`, `report.json` y la evidencia (logs) en
   `workspace/<empresa>/<proyecto>/runs/<fecha>/`.

```bash
export PYTHONPATH=src            # o: pip install -e .
python3 -m qa_agent onboard ~/proyectos/mi-app --company acme --project mi-app
python3 -m qa_agent approve --company acme --project mi-app
python3 -m qa_agent run     --company acme --project mi-app
python3 -m qa_agent run ... --ticket ticket.txt --diff-base origin/main --analyst   # análisis con IA
python3 -m qa_agent list
```

## Veredictos

| Veredicto | Significa | Código de salida |
|---|---|---|
| `APPROVE` | Nada falló y hay pruebas ejecutadas (confianza media/alta) | 0 |
| `OBSERVATIONS` | Falló algo no crítico (lint, auditoría de dependencias) | 0 |
| `BLOCK` | Falló algo crítico (secretos, build, tests) | 1 |
| `INCONCLUSIVE` | No hay evidencia suficiente (p. ej. sin tests ejecutados) | 3 |

El informe es **orientativo**: aprobar o bloquear lo decide una persona.

## Chequeos (capa universal)

secretos en archivos versionados · `.env` versionado · install · build · tests · lint · auditoría de
dependencias. Los comandos salen del perfil según el stack detectado: Node, Python, Go, .NET, Java y
módulos Odoo (estos últimos solo con chequeos estáticos por ahora).

## Modo CI (GitHub Actions)

Cada Pull Request recibe el informe como comentario (uno solo, que se actualiza en cada push).

```bash
qa-agent ci . --init        # crea .qa-agent/profile.yaml (borrador sin aprobar) para proponerlo en un PR
qa-agent ci . --fail-on block --profile-ref origin/main --diff-base origin/main
```

1. En el repo del proyecto: `qa-agent ci . --init`, revisá los comandos, poné `approved: true` y abrí un PR.
   **Esa revisión es la aprobación.**
2. Copiá `examples/qa-caller.yml` a `.github/workflows/qa.yml` del proyecto (cambiá `OWNER`).
3. Listo: el workflow reutilizable (`.github/workflows/qa.yml` de este repo) instala el agente, corre los
   chequeos, comenta el PR, escribe el resumen del job y sube la evidencia como artefacto.

**Seguridad.** En un PR rige el perfil de la **rama base**, nunca el de la rama del PR: si no, quien abre el
PR podría cambiar los comandos aprobados en ese mismo PR. Si el PR toca el perfil, el informe lo avisa y el
cambio rige al fusionarse. Los comandos corren con entorno reducido (sin tokens), el workflow pide permisos
mínimos (`contents: read`, `pull-requests: write`) y los datos del evento pasan por variables de entorno,
no dentro del script. Cada corrida ocurre en una VM descartable de GitHub. Aun así, `npm test` de un PR
ajeno ejecuta código de ese PR: es el riesgo normal de cualquier CI (usá `pull_request`, no `pull_request_target`).
En PRs desde forks el token es de solo lectura: el comentario puede fallar, el artefacto sigue disponible.

`--fail-on`: `block` (default) falla solo si hay BLOCK · `inconclusive` también si no hay evidencia · `never`.

> **Sin probar contra GitHub real:** los tests cubren el comando `ci`, la lógica de seguridad del perfil, el
> comentario y la validez del YAML. El workflow en sí (acciones, permisos, comentario en un PR real) no se
> ejecutó. Probalo primero en un repo de prueba.

## Módulo web (Playwright + Lighthouse)

Se activa solo en proyectos `web` (el onboarding detecta Next, React, Vue, Vite, etc. y propone el `start`).
Requiere `pip install -e '.[web]'` y Chrome instalado (se usa el del sistema; si no, `playwright install chromium`).
Navegar o levantar la app exige **perfil aprobado**.

| Chequeo | Qué hace | Si falla |
|---|---|---|
| `web-load` | Carga cada página en escritorio y móvil; sin 4xx/5xx ni errores de JS | bloquea |
| `web-quality` | Consola, CSP, peticiones fallidas, enlaces internos y anclas rotos, imágenes (incluye las diferidas) y `alt`, `<title>`, `lang`, un solo `h1`, scroll horizontal, controles sin nombre | observaciones |
| `web-flows` | Flujos de usuario declarativos de `web.flows` | bloquea |
| `lighthouse` | Rendimiento, accesibilidad, buenas prácticas y SEO contra umbrales (`web.thresholds`) | observaciones |

La app sale de `web.url` (staging/producción) o se levanta con `web.start` en un puerto libre y se cierra al terminar.
Los flujos son pasos declarativos (`goto`, `click`, `fill`, `press`, `expect_visible`, `expect_hidden`, `expect_text`,
`expect_url`, `wait`): nunca JavaScript arbitrario. Cada corrida guarda capturas y los JSON de Lighthouse como evidencia.
Lighthouse varía entre corridas (el rendimiento móvil se mueve unos ±5 puntos).

## Analista con IA (opcional, `--analyst`)

Propone casos de prueba y riesgos a partir del ticket y el diff, con el SDK oficial de Anthropic
(`pip install 'qa-agent[llm]'` y `ANTHROPIC_API_KEY`). Modelo por defecto: `claude-opus-5-5`, con
`fallbacks` del lado del servidor ante rechazos de seguridad. Reglas:

- Es **asesor**: nunca cambia el veredicto.
- El ticket y el diff se pasan como **datos no confiables** (anti inyección de prompts).
- Si recorta un diff largo, el informe lo dice.

> Esta parte tiene pruebas con un cliente simulado, pero **no se ejecutó contra la API real**.

## Seguridad y límites conocidos

- **No es un sandbox.** Los comandos aprobados corren en la carpeta del proyecto, con variables de
  entorno reducidas a una lista permitida (no se filtran tus claves). Para repos de terceros, corré el
  agente dentro de Docker o una VM.
- Los nombres de empresa/proyecto se validan (sin `../`). Cada empresa tiene su carpeta aislada.
- Pendiente: sandbox Docker (para correr en tu máquina; en CI ya hay VM descartable), pruebas de contrato
  desde OpenAPI, memoria de bugs por proyecto, disparador por ticket.

## Cómo agregar un chequeo

Escribí una función `(Context) -> CheckResult` en `src/qa_agent/checks/` y registrala en
`checks/__init__.py`. Un chequeo nuevo para un tipo de proyecto = un módulo nuevo, no un agente nuevo.

## Pruebas

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[web]'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .   # con Python sin Playwright, los 6 tests de navegador se omiten
```

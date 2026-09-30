<!-- mcp-name: io.github.polaris-smart/agent-mailbox -->
<p align="center"><img src="assets/brand/png/logo-readme.png" width="360" alt="agent-mailbox"></p>

# agent-mailbox

[![polaris-smart/agent-mailbox MCP server](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox/badges/score.svg)](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox)
[![npm](https://img.shields.io/npm/v/agent-mailbox)](https://www.npmjs.com/package/agent-mailbox)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Un buzón de verdad para tus agentes de IA — y tú eres el dueño.** Agentes en CLIs distintos (Claude Code, Codex, Gemini CLI, Hermes, WorkBuddy…) se escriben mensajes de forma asíncrona en una misma máquina, con garantía de entrega, llamadas de despertar y una bandeja de entrada humana de tres paneles donde cada conversación es visible. Cero dependencias, cero nube, cero claves API.

> **📊 Probado en producción**: más de 1.676 mensajes entre 5 agentes en 18 días de desarrollo multiagente diario — ~93 mensajes/día, cero pérdidas.

| Buzón de tres paneles | Asistente setup (autodescubrimiento) |
|---|---|
| <img src="docs/screenshots/mailbox-threepane.png" alt="buzón de tres paneles" width="100%"/> | <img src="docs/screenshots/setup-wizard.png" alt="asistente setup" width="100%"/> |

Otros documentos: [English](README.md) · [中文](README.zh-CN.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

---

## Instalación en 3 pasos

**Requisitos previos** — una sola vez: instala [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`, o `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` en Windows). Todo lo demás lo ejecuta `uvx`.

```bash
# 1 · consigue el CLI
uv tool install git+https://github.com/polaris-smart/agent-mailbox

# 2 · ejecuta el asistente setup — descubre tus agentes por ti
agent-mailbox setup          # abre el asistente local en tu navegador
agent-mailbox setup --yes    # headless / servidor: todo por defecto, sin navegador

# 3 · registra el servidor MCP en tu host de agente
claude mcp add agent-mailbox -- agent-mailbox        # o el JSON genérico de abajo
```

El asistente escanea cuatro capas automáticamente — **qué hay instalado** (CLIs en el PATH, /Applications, directorios de configuración) → **qué está conectado** (configs MCP + el registro del buzón) → **cómo despertar a cada uno** (URL schemes de las apps desde `Info.plist`, puertos a la escucha resueltos por *ruta del ejecutable*, comandos utilizables) → **y prueba cada canal** enviando una carta real. No escribes nada; lo que no puede identificar dice honestamente `unrecognized` en lugar de adivinar.

<details>
<summary>JSON genérico de host MCP (cualquier host)</summary>

```json
{
  "mcpServers": {
    "agent-mailbox": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/polaris-smart/agent-mailbox", "agent-mailbox"]
    }
  }
}
```

Consejo: define `AGENT_MAIL_ID=<id>` en el entorno de un agente y todas las herramientas quedan auto-direccionadas.
</details>

## Cómo funciona

![cómo viaja una carta](docs/diagrams/how-it-works.png)

Un buzón es un directorio de archivos JSON planos — un archivo por carta, legible con `cat`, greppable, tuyo:

```
~/.agent-mail/
  registry.json               agent_id → {kind, owner, description}
  inbox/HS/20260905-….json    un archivo por mensaje
  archive/HS/…
  tasks.json                  el tablero de tareas
  audit.log                   cada cambio de visibilidad, en append
```

Los agentes se comunican a través de un pequeño servidor MCP por stdio (14 herramientas). Sin proceso bróker, sin puertos, sin base de datos, sin red por defecto. Cualquier número de hosts MCP comparten una misma raíz de correo con seguridad (protegida con flock).

**Despertar a un agente dormido.** En el momento en que aterriza una carta, el buzón avisa al host del destinatario por su propia conexión MCP (sampling de MCP) — el LLM *propio* del host lee la carta y actúa, bajo una política de despertar forzada (identidad, tarea, lista dura de prohibiciones). **El buzón en sí no depende de ningún modelo y no guarda ninguna clave API** — la inteligencia se toma prestada del host donde el agente ya corre, y un interruptor por agente apaga el sampling en cualquier momento. Los agentes solo-CLI (codex…) usan el adaptador local-command. El sampling es un acelerador, nunca una garantía de entrega: si falla, la carta aterriza igual y la próxima comprobación entrega igual.

## El humano es el dueño

![modelo de permisos](docs/diagrams/permission-model.png)

- **owner (tú)** — lo ves todo: tu bandeja de entrada *más* todo el tráfico entre agentes, en el buzón web de tres paneles. Lees, respondes, recibes tareas ("necesito tu decisión") y accionas los interruptores de visibilidad — cada cambio queda en `audit.log`.
- **agent** — solo su propia bandeja. Las lecturas entre bandejas reciben un `permission denied` estructurado en la capa de herramientas, no un resultado vacío silencioso.
- **guest** — los remitentes externos o entre dispositivos necesitan un token de emparejamiento; **el correo de origen externo aterriza pero no despierta a nadie** hasta que tú lo confirmas (puerta anti prompt-injection).
- **Cartas selladas** — el contenido solo lo pueden leer las herramientas del agente destinatario; toda vista humana muestra solo metadatos. "El dueño lo ve todo" nunca debe convertirse en un canal de fugas.
- **Tres niveles de atención** — el remitente marca la carta `decision` / `report` / `archive`; por defecto solo "necesita tu decisión" te avisa.

El buzón de tres paneles (`agent-mailbox --web 8900`, `http.server` de stdlib, sigue sin dependencias) tiene carpetas, un panel de monitoreo con todo el tráfico de agentes, un registro de miembros con puntos de estado por canal, acciones por carta (responder / convertir en tarjeta de tarea / archivar), atajos de teclado (`j/k` mover · `e` archivar · `r` responder · `t` tarea · `/` buscar) y una acción de estado vacío ("escribe a tus agentes la primera carta") en lugar de una página en blanco.

## ¿Por qué no usar directamente MCP / Slack / archivos sueltos?

| Enfoque | Multi-CLI | Asíncrono | Despertar | Bandeja humana | Deps |
|---|---|---|---|---|---|
| **agent-mailbox** | ✅ cualquier host MCP | ✅ la bandeja persiste | ✅ sampling + daemon | ✅ tres paneles + tablero | **0** |
| Herramientas MCP crudas | ❌ sesiones por CLI | ❌ se pierde al reiniciar | ❌ | ❌ | — |
| Bot de Slack/Discord | ✅ | ✅ | ✅ | ❌ | tokens API, nube |
| Archivos compartidos + convenciones | ✅ | ⚠️ ad-hoc | ❌ manual | ❌ | tu propio código de bloqueo |

## CLI y herramientas

```bash
agent-mailbox setup [--yes]     # asistente en 3 pasos (descubrir → probar → listo)
agent-mailbox discover [--json] # solo imprime el informe de descubrimiento
agent-mailbox status [--json]   # servicio + salud de canal por miembro
agent-mailbox test <member>     # envía una carta de prueba y espera el acuse
agent-mailbox connect <name>    # conecta a un miembro (respalda antes de escribir)
agent-mailbox uninstall         # restaura cada config tocada (diff = 0)
agent-mailbox --web 8900        # buzón humano + tablero (localhost + token)
```

<details>
<summary><b>Las 14 herramientas MCP</b></summary>

| Herramienta | Notas |
|------|-------|
| `mailbox_register(agent_id, owner?, description?)` | reclama un buzón; idempotente |
| `mailbox_send(to, subject, body, priority?, attention?, sealed?, links?)` | `to` = id / lista / `"all"`; deduplicado por defecto |
| `mailbox_check(agent_id?, mark?)` | recupera pendientes (→ `acked`) |
| `mailbox_reply(msg_id, body)` | enruta de vuelta al remitente (exento de dedupe) |
| `mailbox_list(agent_id?, status?, thread?)` | lista con filtros |
| `mailbox_thread(thread)` | reproduce un hilo del más antiguo al más nuevo, entre agentes |
| `mailbox_done(msg_id)` | marca como atendido |
| `mailbox_broadcast(subject, body)` | a todos los agentes registrados |
| `mailbox_whoami()` | directorio de agentes + raíz de correo |
| `mailbox_wait(agent_id?, timeout_seconds?)` | long-poll para correo nuevo |
| `mailbox_confirm_external(msg_id)` | el owner confirma una carta de origen externo para su ejecución |
| `task_create(title, assignee, due?)` | tarjeta de tarea; el asignado recibe mensaje automático |
| `task_move(task_id, status, …)` | `todo→doing→review→done` (saltar pasos exige `force`) |
| `task_list(assignee?, status?)` | lista las tarjetas de tarea |

</details>

## Fiabilidad, en corto

Las garantías de entrega son el producto. Lo más destacado: **supresión de duplicados** (hash semántico, repetir la misma carta en 24 h devuelve `{"deduped": true}` sin efectos secundarios), **compensación de trabajos a medias** (handled-log en dos fases + `resume_plan`: process / replay / finalize / skip), **recuperación de acked caducados** integrada en el bucle de despertar, un **cortacircuitos de despertar** tras N rondas sin progreso, **protección anti auto-eco** e **hilos de primera clase** con aviso de hilo fantasma. El lado del despertar es fail-open por ley de hierro: si el despertar muere, el correo sigue llegando.

<details>
<summary><b>Detalles del sistema de despertar</b></summary>

- **Wake daemon** — `agent-mailbox wake install --agent ID` escribe unidades launchd `WatchPaths` (macOS) / systemd `PathChanged=` (Linux) sobre la bandeja; cada cambio dispara una ronda de drenaje. Adaptadores: `hermes` (webhook de gateway, firma auto-adaptable), `generic-webhook`, `claude-code` (campana + toast), local-command (solo argv, contenido por variables de entorno, killpg con timeout — para codex y compañía). Los POST fallidos reintentan 5×60 s y se reencolan; una entrada `wake` en `handled_log` hace que cada carta despierte como mucho una vez; la semántica de conteo v2 despierta con todos los pending + acked>600 s.
- **Política de sampling** — las secciones por agente de `wake.json` llevan identity / task / forbidden / `require_receipt` / `max_concurrent`; `wake install` preserva las claves desconocidas entre actualizaciones (tu política sobrevive). Interruptor por agente: `"sampling": {"enabled": false}`. MCP desaprobó el sampling el 2026-07-28 (SEP-2577) — la cadena de respaldo (wake-daemon / webhook / próxima comprobación) mantiene la ley de hierro de llegada del correo.
- **Webhook** — `~/.agent-mail/webhook.json` hace POST de cada carta que aterriza (firmada con HMAC, estilos github/generic/slack), anclado a objetivos loopback/privados, redirecciones rechazadas.
- **Router Jev (opcional, apagado)** — despertar por umbral de puntuación con resumen diario por lotes; cualquier fallo falla abierto.
- **Los lanzamientos se barren** — `scripts/verify_release.sh` revisa el sdist + wheel reales en busca de fugas de rutas/identidad antes de cada tag.

</details>

## Funciona con tu CLI de agente

Todo lo que hable MCP stdio puede albergar un buzón — o sea, todos.

| CLI de agente | Cómo registrarlo |
|-----------|-----------------|
| Claude Code | `claude mcp add agent-mailbox -- agent-mailbox` |
| Codex CLI | `~/.codex/config.toml` → `[mcp_servers.agent-mailbox]` |
| Gemini CLI / Qwen Code | `settings.json` → `"mcpServers": { … }` |
| OpenCode | `opencode.json` → `"mcp": { … }` |
| Hermes / WorkBuddy / cualquier host MCP | el mismo JSON genérico (arriba) |

## Para agentes de IA (pásale esta sección a tu agente)

Si eres un agente de IA leyendo esto porque tu usuario dijo "instala agent-mailbox": ejecuta `agent-mailbox setup --yes`, registra el servidor MCP según el JSON de arriba, elige un id, define `AGENT_MAIL_ID=<id>`, llama una vez a `mailbox_register` y verifica con `mailbox_whoami`. Ya eres direccionable por todos los agentes de esta máquina. Lee en inglés aunque tu usuario no lo haga — los comandos son idénticos en todas partes.

> **🆕 Lo más destacado del changelog v0.7.x** — la v0.7.5 añade el modelo de confianza (owner/agent/guest aplicado en la capa de herramientas), las cartas selladas, la puerta de ejecución para origen externo, el asistente /setup de 3 pasos con autodescubrimiento de cuatro capas, el buzón humano de tres paneles /mail y la página /visibility. La v0.7.4 endurece la cadena de publicación (conservación de claves desconocidas de wake.json, las cartas duplicadas ya no re-despiertan, barrido de artefactos). La v0.7.2 añade el interruptor de sampling por agente (SEP-2577). La v0.7.0 introdujo el sampling wake + el adaptador local-command. 14 herramientas MCP. Historia completa: [Roadmap](#roadmap)

## Notas de seguridad

- La raíz de correo vive en tu directorio personal; las cartas nunca salen de la máquina salvo que actives el webhook (anclado a loopback/privado por defecto).
- Los ids de agente se validan estrictamente — sin path traversal. El descubrimiento es de solo lectura; `connect` respalda cualquier config antes de escribir; `uninstall` restaura con una comprobación de diff a nivel de bytes.
- Sin claves API, sin credenciales de modelo, sin telemetría — el buzón no tiene ninguna.
- Los acuses firmados (ed25519) están en el roadmap.

## Desarrollo

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest          # 361 pruebas
ruff check src tests
```

## Actualizar

`uv tool upgrade agent-mailbox` (o vuelve a descargarlo como lo instalaste).

⚠️ **Reinicia tu sesión de agente (o reconecta el cliente MCP) tras actualizar** — las listas de herramientas MCP se enumeran al inicio de la sesión, así que las herramientas nuevas (14 ahora, antes 9) solo aparecen tras un reinicio.

## Roadmap

- **v0.7.5** (actual) — el modelo de confianza: los miembros llevan un kind (`owner` humano / `agent` / `guest`) con aplicación en la capa de herramientas (la lectura cruzada recibe un `permission denied` estructurado, nunca un resultado vacío silencioso); cartas selladas legibles solo por las herramientas del propio agente destinatario (todos los demás ven metadatos + `redacted: "sealed"`); la puerta de origen externo — el correo externo aterriza pero no despierta a nadie (webhook y sampling lo saltan ambos) hasta que un owner lo confirma vía `mailbox_confirm_external`; niveles de atención en las cartas; el asistente /setup de 3 pasos, el buzón humano de tres paneles /mail (carpetas / monitor / registro / acciones por carta) y la página /visibility conectadas a `config.json` + `audit.log`.
- **v0.7.4** — endurecimiento del despertar: `wake install` ya no borra en silencio claves desconocidas de `wake.json` (la sección de política de sampling por agente sobrevive a las actualizaciones — P0); las cartas duplicadas ya no re-despiertan (`wake_suppressed_dup`); los prompts de sampling wake llevan el conteo real de pendientes; `scripts/verify_release.sh` barre sdist + wheel contra criterios fijados antes de cada tag.
- **v0.7.3** — higiene del sdist, segunda ronda: el barrido de artefactos pos-publicación pilló `scripts/wake-zc.sh` (un envoltorio de ops específico de la máquina con rutas locales hardcodeadas) viajando en los sdists 0.7.0–0.7.2; ahora excluido vía `exclude` de hatchling — la wheel nunca lo llevó, la copia del repo permanece (el cableado launchd, intacto).
- **v0.7.2** — endurecimiento SEP-2577 + higiene de publicación: **interruptor de sampling por agente** (`"sampling": {"enabled": false}` en la sección por agente de `wake.json` — MCP desaprobó la capability de sampling el 2026-07-28; los valores malformados fallan ruidosamente en `sampling.log`, y el correo nunca depende del sampling); **higiene del sdist** (activos AOCI + fuga de rutas locales excluidos vía `exclude` de hatchling + `.gitignore`); `__version__` ahora sigue la versión de pyproject.
- **v0.7.0** — la actualización del despertar: **sampling wake** (`createMessage` iniciado por el servidor sobre la propia conexión MCP del host — inyección de política de despertar, bloqueo de ejecución por agente, timeout de 60 s, dedupe por mensaje, fail-open hacia el buzón), **adaptador de despertar local-command** (solo argv, contenido inyectado por entorno, killpg con timeout para agentes CLI bajo demanda como codex), anulación `wake run --adapter`. 13 herramientas MCP.
- **v0.6.2** — endurecimiento de seguridad + cierres de v0.5.x: **SECURITY.md** (reporte de vulnerabilidades vía GitHub Security Advisories, versiones soportadas, el modelo de confianza local declarado abiertamente); **identity binding** (`identity_binding` opcional en `config.json`: los ids de agente vinculados deben presentar `AGENT_MAIL_TOKEN` — sha256 + `hmac.compare_digest` en tiempo constante — o las llamadas fallan con `identity mismatch`; desactivado por defecto, los agentes no vinculados no cambian, la config malformada falla ruidosamente al arrancar); **`unread_count` en los payloads del webhook** (el conteo de pendientes del destinatario al momento de notificar, campo de primer nivel, adición pura); **semántica de claim para `mailbox_wait`** (`claim()` atómico bajo cerrojo: las cartas vuelven `acked` + `claimed_by`, un segundo waiter nunca re-consume un lote, los claims caducados mueren con la cosecha acked→pending).
- **v0.6.0** — el lote estrella: **wake daemon** (`agent-mailbox wake install` — launchd WatchPaths / systemd PathChanged disparan una ronda de drenaje; adaptadores hermes / generic-webhook / claude-code; los POST fallidos reintentan 5×60s y se reencolan en el siguiente disparo, las entradas `wake` en `handled_log` hacen que cada carta despierte como mucho una vez, semántica de conteo v2 = todos los pending + acked>600s; ley de hierro fail-open de punta a punta); **hilos de primera clase** (`thread_id` acuñado al enviar y heredado al responder, `mailbox_thread` reproduce entre agentes en orden temporal, filtro de lista `--thread`, relleno retroactivo por clave de asunto de cadenas Re:, aviso de hilo fantasma pasadas 5 cartas abiertas); **bypass de enrutado Jev** (plugin de puntuación opcional apagado por defecto: Noul puerta el despertar, las puntuaciones bajo el umbral se agrupan en un resumen diario, cualquier fallo cae en fail-open a despertar-con-cualquier-correo, decisiones registradas con puntuaciones, stdlib puro tras el extra `[jev]`). 13 herramientas MCP.
- **v0.5.0** — endurecimiento del ciclo de vida a raíz de los incidentes del 2026-09-13 (tarea `t-6`): **supresión de duplicados** (`semantic_hash` del lado de entrega, los repetidos no terminales con el mismo hash en una ventana de 24h devuelven `{"deduped": true, "existing_id"}` sin efectos secundarios; `dedupe: false` exime; hashing del contenido crudo de las vallas de código, alcance solo inbox, índice hash→inbox); **compensación de trabajos a medias** (API intent/outcome en dos fases de `record_handled` como único escritor de `handled_log` + tabla de cuatro filas `resume_plan`: process / replay / finalize / skip); **recuperación de acked caducados** lanzada antes como `reap_stale_acked` / `python -m agent_mailbox.reap` ahora cableada al bucle de despertar (primero cosechar, luego contar, fail-open) con el acoplamiento de la ley de hierro 1 exigido — `reap_ttl` (3600s) debe quedarse estrictamente por debajo de `dedup_ttl` (24h), las violaciones fallan ruidosamente; **cortacircuitos de despertar** — N rondas de drenaje consecutivas sin progreso latchean un archivo de breaker y dejan de lanzar turnos (el backoff estira el intervalo, el breaker corta la hemorragia).
- **v0.5.x (abierto)** — aún en seguimiento por las revisiones de t-6: `status filtering` (pedir vistas "pending o acked"), `wake routing` (filtro `to` en la suscripción del gateway; vive fuera de este repo). Enviado en v0.6.2: ~~`identity binding`~~, ~~`unread_count` en payloads del webhook~~, ~~semántica de claim de `mailbox_wait`~~.
- **v0.4.0** — lote de funcionalidades: estilo de firma del webhook configurable (`AGENT_MAIL_SIGNATURE_STYLE`: github por defecto / generic / slack); protección anti auto-eco (las notificaciones donde remitente == destinatario se descartan por defecto, auditadas como `echo_suppressed` en `sent.log`; `notify_self_echo` / `AGENT_MAIL_NOTIFY_SELF_ECHO` restaura la entrega con prefijo `[echo] ` en el asunto de la notificación mientras la carta conserva el suyo); nuevo comando de mantenimiento `cleanup --dry-run` (escanea residuos de prueba, lista sin borrar, `--yes` borra tras confirmación).
- **v0.3.1** — lote de parches: los asuntos de respuesta ya no acumulan `Re: Re:` (primera respuesta, re-respuestas y prefijos con mayúsculas mixtas se normalizan a un único `Re:`); los tokens del tablero web usan comparación en tiempo constante (`hmac.compare_digest`) y persisten entre reinicios (`~/.agent-mail/web_token`, modo 0600, la env `AGENT_MAIL_WEB_TOKEN` siempre gana); `sent.log` rota automáticamente una generación pasados 10 MB (a `sent.log.1`).
- **v0.3.0** — tablero de tareas + kanban web: `task_create` / `task_move` / `task_list` con una máquina de estados estricta todo→doing→review→done; crear o mover una tarjeta envía un mensaje automático al asignado, así el movimiento del tablero despierta agentes sin polling. `--web 8643` sirve una UI kanban sin dependencias protegida por token donde el arrastrar-y-soltar humano pasa por el mismo camino de despertar. Mensajes + tareas + despertar + tablero, aún cero dependencias.
- **Next** — channels (canales temáticos con registros de suscriptores, visibles para el owner); federación: transporte streamable HTTP para agentes en otras máquinas (amigable con Tailscale/LAN); acuses firmados (ed25519) para una entrega a prueba de manipulación.
- **v1.0.0** — puente entre organizaciones: los hilos locales alcanzan agentes en otras máquinas y organizaciones sobre la infraestructura de correo estándar, con el mismo ciclo de vida del buzón.

## Licencia

MIT

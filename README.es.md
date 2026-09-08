# agent-mailbox

[![polaris-smart/agent-mailbox MCP server](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox/badges/score.svg)](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox)

**Un buzón propio para cada agente de IA local.** Un servidor MCP por stdio. Cero demonios. Un archivo JSON por mensaje. Más un tablero de tareas integrado: las tarjetas despiertan a su responsable al moverse, y un kanban web sin dependencias para el humano.

📖 **Docs**: [English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

> 🆕 **v0.3.0 — Tablero de tareas**: los agentes comparten ahora una superficie de tareas sobre la misma raíz de correo. 3 herramientas MCP nuevas (12 en total), un tablero de arrastrar y soltar sin dependencias (`--web`), y cada movimiento avisa al responsable. ⚠️ **Nota de actualización**: reinicia tu sesión de agente para cargar las herramientas nuevas. → [Tablero de tareas](#tablero-de-tareas)

---

## El problema

Ejecutar varios agentes de IA en una misma máquina — Claude Code, Hermes, tus propios scripts — y no tienen forma de dejarse mensajes. Se quedan esperándose, o terminas copiando y pegando entre ventanas como un operador humano.

## La solución

Un buzón es un directorio de archivos JSON simples:

```
~/.agent-mail/
  registry.json                agent_id → {owner, description, created_at}
  inbox/HS/20260905-….json     un archivo por mensaje
  archive/HS/…
  tasks.json                   el tablero de tareas ({"next_id", "tasks": {id: tarjeta}})
```

Los agentes lo leen y escriben a través de un pequeño servidor MCP por stdio. Sin proceso intermediario, sin puertos, sin base de datos, sin red por defecto. Cualquier número de hosts MCP comparten una misma raíz de correo de forma segura (con bloqueo de archivos).

## Inicio rápido

**Requisitos previos** — solo una vez: instala [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh` en macOS/Linux, o `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` en Windows). `uvx` ejecuta todo lo demás; no hay nada más que instalar.

### 1 · Registra el servidor en tu host MCP

Claude Code:

```bash
claude mcp add agent-mailbox -- uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

Cualquier host MCP (JSON genérico):

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

Consejo: define `AGENT_MAIL_ID=HS` (o el id que quieras) en el entorno del agente y todas las herramientas quedan autodireccionadas — sin pasar `agent_id` en cada llamada.

### 2 · Los agentes se registran una vez

```json
{ "tool": "mailbox_register", "arguments": { "agent_id": "HS", "owner": "Hermes", "description": "PM & QA" } }
```

El registro es idempotente. Todo agente registrado es direccionable de inmediato por todos — incluido un id humano `boss` que puedes leer tú mismo.

### 3 · Enviar, revisar, responder

```json
{ "tool": "mailbox_send", "arguments": { "to": "HS", "subject": "deploy ready", "body": "v0.1.0 preparada, por favor verifica." } }
{ "tool": "mailbox_check", "arguments": {} }
{ "tool": "mailbox_reply", "arguments": { "msg_id": "20260905-…-hs", "body": "verificado, marcado done." } }
```

`mailbox_check` trae los mensajes pendientes y los marca `acked`. Ciclo de vida: `pending → acked → done`, y luego se archivan opcionalmente. Cada mensaje es un JSON que puedes `cat` — el jefe lee la bandeja directamente.

### 4 · Esperar en vez de sondear

`mailbox_wait` se bloquea (long-poll) hasta que llega un mensaje — llámalo como última acción del turno:

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

## Tablero de tareas

Las tarjetas viven en `<raíz de correo>/tasks.json` (JSON plano, el mismo bloqueo de archivos que el correo). La máquina de estados es estricta: `todo→doing→review→done`; los saltos no adyacentes se rechazan salvo `force=True`, y `done` es terminal. Crear o mover una tarjeta envía al responsable un mensaje normal del buzón (`[task#t-12 → review] …`) — el movimiento del tablero despierta al agente por la bandeja existente, sin sondeos ni webhooks. Los movimientos que uno se hace a sí mismo permanecen en silencio, y `notify=False` los desactiva.

**Tablero web (para el humano).** `agent-mailbox --web 8643` sirve un kanban sin dependencias (`http.server` de stdlib + una sola página HTML embebida, sin frameworks) en `127.0.0.1`. Cuatro columnas reflejan la máquina de estados; arrastra una tarjeta entre columnas adyacentes para moverla, o crea tarjetas desde el formulario, con una sola alternancia entre tema claro y oscuro. La autenticación es un token bearer — define `AGENT_MAIL_WEB_TOKEN` para uno fijo, o se genera e imprime uno nuevo en cada arranque (abre `http://127.0.0.1:8643/?token=…`). El tablero actúa como agente `boss`: cada tarjeta que crees o arrastres sigue avisando al responsable — cada movimiento le envía un mensaje. La página se refresca cada 5 segundos.

## Despertar a un agente dormido (una línea de configuración)

Si el agente receptor ni siquiera está en ejecución, `mailbox_send` puede hacer POST de cada mensaje nuevo a un webhook en el instante en que aterriza — sin demonios, sin sondeo, sin procesos extra:

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

Genera el secreto una vez: `openssl rand -hex 32`. Omítelo para POSTs sin firmar (suficiente para pruebas locales; exigir la verificación lo decide el receptor).

El manejador de webhooks del host recibe:

```json
{ "event": "agent_mailbox_new_message", "event_type": "agent_mailbox_new_message", "message": { "id": "…", "from": "ZC", "to": "HS", "subject": "…", "body": "…" } }
```

…despierta al agente, y el agente llama a `mailbox_check` al llegar. Esa es toda la integración.

- Firmado con `X-Hub-Signature-256: sha256=<hmac>` (esquema GitHub — aceptado por Hermes gateway y la mayoría de consumidores de webhooks).
- El destino queda fijado: solo http/https, direcciones loopback/privadas por defecto, redirecciones rechazadas, proxy del sistema omitido.
- Las variables de entorno `AGENT_MAIL_WEBHOOK_URL` / `AGENT_MAIL_WEBHOOK_SECRET` tienen prioridad sobre el archivo. Sin configurar → totalmente offline.

## Las herramientas

| Herramienta | Notas |
|-------------|-------|
| `mailbox_register(agent_id, owner?, description?)` | reclama un buzón; idempotente |
| `mailbox_send(to, subject, body, priority?)` | `to` = un id, una lista, o `"all"` |
| `mailbox_check(agent_id?, mark?)` | trae pendientes (→ `acked`) |
| `mailbox_reply(msg_id, body)` | enruta de vuelta al remitente original |
| `mailbox_list(agent_id?, status?)` | lista mensajes, filtro opcional por estado |
| `mailbox_done(msg_id)` | marca como atendido |
| `mailbox_broadcast(subject, body)` | a todos los agentes registrados |
| `mailbox_whoami()` | directorio de agentes + raíz de correo |
| `mailbox_wait(agent_id?, timeout_seconds?)` | long-poll de correo nuevo |
| `task_create(title, assignee, due?)` | crea una tarjeta de tarea (arranca en `todo`); avisa al responsable |
| `task_move(task_id, status, assignee?, note?, force?)` | avanza por `todo→doing→review→done` (los saltos requieren `force`); mover una tarjeta avisa a su responsable |
| `task_list(assignee?, status?)` | lista tarjetas de tarea, filtros opcionales |

## Opcional: notificaciones de escritorio para humanos

Un watcher complementario imprime cada mensaje nuevo como línea JSON y lanza notificaciones de escritorio (macOS / Linux / Windows). Nunca está en la ruta de despertar de agentes — los agentes no lo necesitan:

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss
```

| Plataforma | Instalar | Verificar |
|------------|----------|-----------|
| macOS (launchd) | `scripts/install-watch-macos.sh --notify boss` | `tail -f ~/.agent-mail/watch.log` |
| Linux (systemd user) | `scripts/install-watch-linux.sh …` | `journalctl --user -u agent-mailbox-watch -f` |
| Windows (schtasks) | `scripts\install-watch-windows.ps1` | `schtasks /Query /TN AgentMailboxWatch /V` |

## Diseño

- **Local-first** — archivos JSON simples bajo `~/.agent-mail/`. Sin SMTP, sin IMAP, sin dominio, sin relé en la nube, sin red por defecto.
- **Direccionamiento con un registro** — `mailbox_register("HS")` es todo lo que hace falta; todo agente registrado es direccionable por todos.
- **Cero dependencias externas** — solo `mcp`. El almacén es un archivo Python con escrituras atómicas protegidas por `flock`.
- **Legible por humanos** — cada mensaje es un JSON pequeño que puedes `cat`. El jefe lee la bandeja directamente.
- **Respeta identidades existentes** — define `AGENT_MAIL_ID` en el entorno de cada agente y sus herramientas quedan autodireccionadas.

## Notas de seguridad

- La raíz de correo vive en tu directorio personal; los mensajes nunca salen de la máquina salvo que actives el webhook, fijado por defecto a destinos loopback/privados.
- Los ids de agente se validan estrictamente (`[A-Za-z0-9_-]`, ≤64 caracteres) — sin path traversal.
- El almacén es orientado a anexión con escrituras atómicas y bloqueos; un escritor caído no corrompe el registro.
- Los payloads del webhook van firmados con HMAC; los verificadores deben usar comparación en tiempo constante.
- Los recibos firmados (ed25519) están en el roadmap.

## Desarrollo

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest
```

## Actualización

Actualiza con `uv tool upgrade agent-mailbox` (o vuelve a instalar según tu método original).

⚠️ **Tras actualizar, reinicia tu sesión de agente (o reconecta el cliente MCP)** — la lista de herramientas MCP se enumera al iniciar la sesión, así que las herramientas nuevas (12 ahora, antes 9) solo aparecen tras un reinicio. No hay que cambiar ninguna configuración; `tasks.json` se crea automáticamente al primer uso.

## Roadmap

- **v0.3.0** (actual) — tablero de tareas + kanban web: `task_create` / `task_move` / `task_list` con una estricta máquina de estados todo→doing→review→done; crear o mover una tarjeta avisa automáticamente al responsable, así el movimiento del tablero despierta agentes sin ningún sondeo. `--web 8643` sirve una interfaz kanban sin dependencias protegida por token donde el arrastre humano pasa por la misma ruta de despertar. Mensajes + tareas + despertar + tablero, cero dependencias.
- **v0.4.0** — quizá: integraciones kanban más profundas (Kaneo como referencia/competidor). En discusión.
- **Siguiente** — federación: transporte HTTP streamable para agentes en otras máquinas (amigable con Tailscale/LAN); recibos firmados (ed25519) para entrega a prueba de manipulación.
- **v1.0.0** — puente entre organizaciones: los hilos locales alcanzan agentes en otras máquinas y organizaciones sobre infraestructura de email estándar, con el mismo ciclo de vida del buzón.

## Licencia

MIT

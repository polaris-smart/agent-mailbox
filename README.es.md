# agent-mailbox

**Un buzón propio para cada agente de IA local.** Un servidor MCP por stdio. Cero demonios. Un archivo JSON por mensaje.

📖 **Docs**: [English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

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
```

Los agentes lo leen y escriben a través de un pequeño servidor MCP por stdio. Sin proceso intermediario, sin puertos, sin base de datos, sin red por defecto. Cualquier número de hosts MCP comparten una misma raíz de correo de forma segura (con bloqueo de archivos).

## Inicio rápido

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

## Despertar a un agente dormido (una línea de configuración)

Si el agente receptor ni siquiera está en ejecución, `mailbox_send` puede hacer POST de cada mensaje nuevo a un webhook en el instante en que aterriza — sin demonios, sin sondeo, sin procesos extra:

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

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

## Roadmap

- **v0.1.0** (actual) — buzones para agentes en la misma máquina vía stdio MCP. Infraestructura cero. Long-poll `mailbox_wait`, webhook de despertar integrado en `mailbox_send`, watcher opcional.
- **v0.2.0** — federación: transporte HTTP streamable para agentes en otras máquinas (amigable con Tailscale/LAN).
- **v0.3.0** — recibos firmados (ed25519).
- **v1.0.0** — puente entre organizaciones: los hilos locales alcanzan agentes en otras máquinas y organizaciones sobre infraestructura de email estándar, con el mismo ciclo de vida del buzón.

## Licencia

MIT

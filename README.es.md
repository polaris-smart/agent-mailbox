# agent-mailbox

**Un buzón propio para cada agente de IA local.**

Un solo servidor MCP. Regístrate una vez y envía mensajes a cualquier agente de esta máquina. Sin cron. Sin demonios de sondeo. Sin archivos markdown compartidos. Sin nube.

📖 **Docs**: [English](README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md) · [Español](README.es.md) — [Diagrama de arquitectura](docs/architecture.html)

![Arquitectura de agent-mailbox](docs/architecture.png)

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox          # transporte stdio, listo para cualquier host MCP
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox --http 8642   # o expónlo por HTTP para agentes remotos
```

---

## El problema

Tu agente de código, tu agente de operaciones, tu agente de revisión — todos ejecutándose en la misma máquina, todos perfectamente capaces — no pueden comunicarse entre sí. Así que **tú** terminas siendo el mensajero: copiando conclusiones de una terminal, pegando instrucciones en otra, retransmitiendo actualizaciones de estado a mano.

Las soluciones basadas en archivos (un "registro" markdown compartido, una carpeta `dropped-notes/`) acaban pudriéndose en una transcripción ilegible. Las soluciones de cron-y-escaneo queman tokens en sondeos vacíos. Los relés en la nube ponen los datos de tu flujo de trabajo detrás de la API de otra persona.

## La solución

Un buzón que es **simplemente otra herramienta**:

| Herramienta | Qué hace |
|---|---|
| `mailbox_register` | Reclama tu buzón. Idempotente. |
| `mailbox_send` | Entrega a un agente, una lista, o `"all"` para difusión. |
| `mailbox_check` | Obtiene mensajes pendientes — se autoconfirman al leerse. |
| `mailbox_reply` | Responde dentro de un hilo, enrutado automáticamente al remitente. |
| `mailbox_list` | Explora por estado (`pending` / `acked` / `done`). |
| `mailbox_done` | Marca como gestionado; los mensajes done se archivan automáticamente. |
| `mailbox_broadcast` | Una llamada, todos los agentes registrados. |
| `mailbox_whoami` | Quién está registrado, dónde está la raíz del correo. |

Los mensajes son JSON plano con un ciclo de vida mínimo: `pending → acked → done`. Un mensaje que llega mientras el destinatario está desconectado simplemente espera — el correo, como debe ser.

## Inicio rápido

**Hermes** (`~/.hermes/config.yaml`):

```yaml
mcp:
  servers:
    agent-mailbox:
      command: uvx
      args: ["git+https://github.com/polaris-smart/agent-mailbox"]
```

**Claude Code** (`~/.claude/settings.json`):

```json
{ "mcpServers": { "agent-mailbox": { "command": "uvx", "args": ["--from", "git+https://github.com/polaris-smart/agent-mailbox", "agent-mailbox"] } } }
```

**Cualquier cliente MCP** (stdio):

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

**Agentes remotos** (p. ej., un agente en otro servidor):

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox --http 8642   # en el host de correo
```

```json
{ "mcpServers": { "agent-mailbox": { "url": "http://your-host:8642/mcp" } } }
```

## Esperar correo (sin sondeo)

Los agentes no necesitan sondear. `mailbox_wait` se bloquea (long-poll) hasta que llega un mensaje — llámalo como última acción del turno y el siguiente mensaje despertará a tu agente de inmediato:

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

Para humanos y paneles, un watcher complementario imprime cada mensaje nuevo como línea JSON y puede disparar notificaciones de macOS para los agentes elegidos:

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss      # centro de notificaciones de macOS
agent-mailbox-watch --once                     # escaneo único (compatible con cron)
```

## Diseño

- **Local-first** — archivos JSON planos bajo `~/.agent-mail/`. Sin SMTP, sin IMAP, sin dominio, sin relé en la nube, sin red (salvo que actives el transporte HTTP a propósito).
- **Cero dependencias** — solo la biblioteca estándar de Python. Una base de código de ~500 líneas que se entiende de una lectura.
- **MCP nativo** — no es un plugin de CLI más: cualquier host MCP puede registrarse.
- **Identidad por registro** — `mailbox_register` una vez, identidad persistente; no desaparece al terminar la sesión.

Los buzones locales resuelven la coordinación en la misma máquina y en LAN de confianza. El ciclo de vida de mensajes (`pending → acked → done`) está diseñado para continuar intacto cuando los hilos de un agente necesiten llegar a otras máquinas y organizaciones sobre infraestructura de correo real.

## Notas de seguridad

- La raíz del correo vive en tu directorio personal; los mensajes nunca salen de la máquina salvo que actives el transporte HTTP en una red confiable.
- Los IDs de agente se validan estrictamente (`[A-Za-z0-9_-]`, ≤64 caracteres) — sin path traversal.
- El almacén está orientado a añadir, con escrituras atómicas y bloqueos de archivo; un escritor que falle no corrompe el registro.
- Para prueba de manipulación, los recibos firmados (ed25519) están en la hoja de ruta.

## Hoja de ruta

- **v0.1.0** (actual) — buzones de agente en la misma máquina sobre stdio MCP. Infraestructura cero. Incluye `mailbox_wait` de long-poll y un watcher complementario — sin demonios de sondeo.
- **v0.2.0** — federación: transporte HTTP streamable para agentes en otras máquinas (Tailscale/LAN friendly).
- **v0.3.0** — recibos firmados (ed25519) para entrega a prueba de manipulación.
- **v1.0.0** — puente entre organizaciones: los hilos locales llegan a agentes en otras máquinas y organizaciones sobre infraestructura de correo estándar, con el mismo ciclo de vida del buzón.

Proyecto hermano: [dsh-devices](https://github.com/polaris-smart/dsh-devices) gestiona tus dispositivos; agent-mailbox gestiona la conversación entre los agentes que hay en ellos.

## Desarrollo

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
pip install -e ".[dev]"
pytest
```

## Licencia

MIT — ver [LICENSE](LICENSE).

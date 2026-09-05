# agent-mailbox

**Um mailbox próprio para cada agente de IA local.** Um servidor MCP por stdio. Zero daemons. Um arquivo JSON por mensagem.

📖 **Docs**: [English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

---

## O problema

Rodar vários agentes de IA na mesma máquina — Claude Code, Hermes, seus próprios scripts — e eles não têm como deixar mensagens entre si. Ficam esperando uns pelos outros, ou você acaba copiando e colando entre janelas como um operador humano.

## A solução

Um mailbox é um diretório de arquivos JSON simples:

```
~/.agent-mail/
  registry.json                agent_id → {owner, description, created_at}
  inbox/HS/20260905-….json     um arquivo por mensagem
  archive/HS/…
```

Os agentes leem e escrevem através de um pequeno servidor MCP por stdio. Sem processo intermediário, sem portas, sem banco de dados, sem rede por padrão. Quantos hosts MCP você quiser compartilham a mesma raiz de correio com segurança (bloqueio de arquivo).

## Início rápido

### 1 · Registre o servidor no seu host MCP

Claude Code:

```bash
claude mcp add agent-mailbox -- uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

Qualquer host MCP (JSON genérico):

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

Dica: defina `AGENT_MAIL_ID=HS` (ou o id que preferir) no ambiente do agente e todas as ferramentas ficam autodirecionadas — sem passar `agent_id` a cada chamada.

### 2 · Os agentes se registram uma vez

```json
{ "tool": "mailbox_register", "arguments": { "agent_id": "HS", "owner": "Hermes", "description": "PM & QA" } }
```

O registro é idempotente. Todo agente registrado é imediatamente endereçável por todos — incluindo um id humano `boss` que você mesmo pode ler.

### 3 · Enviar, verificar, responder

```json
{ "tool": "mailbox_send", "arguments": { "to": "HS", "subject": "deploy ready", "body": "v0.1.0 preparada, por favor verifique." } }
{ "tool": "mailbox_check", "arguments": {} }
{ "tool": "mailbox_reply", "arguments": { "msg_id": "20260905-…-hs", "body": "verificado, marcado done." } }
```

`mailbox_check` traz as mensagens pendentes e as marca `acked`. Ciclo de vida: `pending → acked → done`, depois arquivadas opcionalmente. Cada mensagem é um JSON que você pode `cat` — o chefe lê a caixa de entrada diretamente.

### 4 · Esperar em vez de sondar

`mailbox_wait` bloqueia (long-poll) até chegar uma mensagem — chame como última ação do turno:

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

## Acordar um agente dormindo (uma linha de configuração)

Se o agente receptor nem está em execução, o próprio `mailbox_send` pode fazer POST de cada mensagem nova para um webhook no instante em que ela chega — sem daemon, sem sondagem, sem processo extra:

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

O manipulador de webhooks do host recebe:

```json
{ "event": "agent_mailbox_new_message", "event_type": "agent_mailbox_new_message", "message": { "id": "…", "from": "ZC", "to": "HS", "subject": "…", "body": "…" } }
```

…acorda o agente, e o agente chama `mailbox_check` ao chegar. Essa é toda a integração.

- Assinado com `X-Hub-Signature-256: sha256=<hmac>` (esquema GitHub — aceito pelo Hermes gateway e pela maioria dos consumidores de webhooks).
- O destino é fixado: apenas http/https, endereços loopback/privados por padrão, redirecionamentos recusados, proxy do sistema ignorado.
- As variáveis de ambiente `AGENT_MAIL_WEBHOOK_URL` / `AGENT_MAIL_WEBHOOK_SECRET` têm prioridade sobre o arquivo. Sem configurar → totalmente offline.

## As ferramentas

| Ferramenta | Notas |
|------------|-------|
| `mailbox_register(agent_id, owner?, description?)` | reivindica um mailbox; idempotente |
| `mailbox_send(to, subject, body, priority?)` | `to` = um id, uma lista, ou `"all"` |
| `mailbox_check(agent_id?, mark?)` | traz pendentes (→ `acked`) |
| `mailbox_reply(msg_id, body)` | roteia de volta ao remetente original |
| `mailbox_list(agent_id?, status?)` | lista mensagens, filtro opcional por status |
| `mailbox_done(msg_id)` | marca como tratado |
| `mailbox_broadcast(subject, body)` | para todos os agentes registrados |
| `mailbox_whoami()` | diretório de agentes + raiz de correio |
| `mailbox_wait(agent_id?, timeout_seconds?)` | long-poll de novas mensagens |

## Opcional: notificações de desktop para humanos

Um watcher complementar imprime cada mensagem nova como linha JSON e dispara notificações de desktop (macOS / Linux / Windows). Ele nunca está no caminho de despertar de agentes — os agentes não precisam dele:

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss
```

| Plataforma | Instalar | Verificar |
|------------|----------|-----------|
| macOS (launchd) | `scripts/install-watch-macos.sh --notify boss` | `tail -f ~/.agent-mail/watch.log` |
| Linux (systemd user) | `scripts/install-watch-linux.sh …` | `journalctl --user -u agent-mailbox-watch -f` |
| Windows (schtasks) | `scripts\install-watch-windows.ps1` | `schtasks /Query /TN AgentMailboxWatch /V` |

## Design

- **Local-first** — arquivos JSON simples sob `~/.agent-mail/`. Sem SMTP, sem IMAP, sem domínio, sem retransmissão em nuvem, sem rede por padrão.
- **Endereçamento com um registro** — `mailbox_register("HS")` é tudo o que é preciso; todo agente registrado é endereçável por todos.
- **Zero dependências externas** — apenas `mcp`. O armazenamento é um arquivo Python com escritas atômicas protegidas por `flock`.
- **Legível por humanos** — cada mensagem é um JSON pequeno que você pode `cat`. O chefe lê a caixa de entrada diretamente.
- **Respeita identidades existentes** — defina `AGENT_MAIL_ID` no ambiente de cada agente e suas ferramentas ficam autodirecionadas.

## Notas de segurança

- A raiz de correio vive no seu diretório pessoal; as mensagens nunca saem da máquina a menos que você ative o webhook, fixado por padrão a destinos loopback/privados.
- Os ids de agente são estritamente validados (`[A-Za-z0-9_-]`, ≤64 caracteres) — sem path traversal.
- O armazenamento é orientado a anexação com escritas atômicas e bloqueios; um escritor que falhar não corrompe o registro.
- Os payloads do webhook vão assinados com HMAC; verificadores devem usar comparação em tempo constante.
- Recebos assinados (ed25519) estão no roadmap.

## Desenvolvimento

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest
```

## Roadmap

- **v0.1.0** (atual) — mailboxes para agentes na mesma máquina via stdio MCP. Infraestrutura zero. Long-poll `mailbox_wait`, webhook de despertar integrado ao `mailbox_send`, watcher opcional.
- **v0.2.0** — federação: transporte HTTP streamable para agentes em outras máquinas (amigável a Tailscale/LAN).
- **v0.3.0** — recibos assinados (ed25519).
- **v1.0.0** — ponte entre organizações: threads locais alcançam agentes em outras máquinas e organizações sobre infraestrutura de e-mail padrão, com o mesmo ciclo de vida do mailbox.

## Licença

MIT

# agent-mailbox

[![polaris-smart/agent-mailbox MCP server](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox/badges/score.svg)](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox)

**Um mailbox próprio para cada agente de IA local.** Um servidor MCP por stdio. Zero daemons. Um arquivo JSON por mensagem. Mais um quadro de tarefas integrado: os cartões despertam o responsável ao se mover, e um kanban web sem dependências para o humano.

📖 **Docs**: [English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

> 🆕 **v0.3.0 — Quadro de tarefas**: os agentes compartilham agora uma superfície de tarefas na mesma raiz de correio. 3 ferramentas MCP novas (12 no total), um quadro de arrastar e soltar sem dependências (`--web`), e cada movimento avisa o responsável. ⚠️ **Nota de atualização**: reinicie sua sessão de agente para carregar as ferramentas novas. → [Quadro de tarefas](#quadro-de-tarefas)

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
  tasks.json                   o quadro de tarefas ({"next_id", "tasks": {id: cartão}})
```

Os agentes leem e escrevem através de um pequeno servidor MCP por stdio. Sem processo intermediário, sem portas, sem banco de dados, sem rede por padrão. Quantos hosts MCP você quiser compartilham a mesma raiz de correio com segurança (bloqueio de arquivo).

## Início rápido

**Pré-requisitos** — apenas uma vez: instale o [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh` no macOS/Linux, ou `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` no Windows). O `uvx` roda todo o resto; nada mais a instalar.

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

## Quadro de tarefas

Os cartões vivem em `<raiz de correio>/tasks.json` (JSON puro, o mesmo bloqueio de arquivo do correio). A máquina de estados é estrita: `todo→doing→review→done`; movimentos não adjacentes são recusados a menos que `force=True`, e `done` é terminal. Criar ou mover um cartão envia ao responsável uma mensagem normal do mailbox (`[task#t-12 → review] …`) — o movimento do quadro desperta o agente pela caixa de entrada existente, sem sondagem, sem webhooks. Movimentos para si mesmo ficam em silêncio, e `notify=False` desliga o aviso.

**Quadro web (para o humano).** `agent-mailbox --web 8643` serve um kanban sem dependências (`http.server` da stdlib + uma única página HTML embutida, sem framework) em `127.0.0.1`. Quatro colunas espelham a máquina de estados; arraste um cartão entre colunas adjacentes para movê-lo, ou crie cartões pelo formulário, com uma alternância entre tema claro e escuro. A autenticação é um token bearer — defina `AGENT_MAIL_WEB_TOKEN` para um fixo, ou um novo token é gerado e impresso a cada boot (abra `http://127.0.0.1:8643/?token=…`). O quadro age como agente `boss`: todo cartão que você cria ou arrasta continua avisando o responsável — cada movimento acorda o agente certo. A página se atualiza a cada 5 segundos.

## Acordar um agente dormindo (uma linha de configuração)

Se o agente receptor nem está em execução, o próprio `mailbox_send` pode fazer POST de cada mensagem nova para um webhook no instante em que ela chega — sem daemon, sem sondagem, sem processo extra:

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

Gere o segredo uma vez: `openssl rand -hex 32`. Omita para POSTs não assinados (suficiente para testes locais; exigir a verificação é decisão do receptor).

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
| `task_create(title, assignee, due?)` | cria um cartão de tarefa (começa em `todo`); avisa o responsável |
| `task_move(task_id, status, assignee?, note?, force?)` | avança por `todo→doing→review→done` (saltos exigem `force`); mover um cartão avisa o responsável |
| `task_list(assignee?, status?)` | lista cartões de tarefa, filtros opcionais |

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

## Atualização

Atualize com `uv tool upgrade agent-mailbox` (ou reinstale conforme seu método original).

⚠️ **Depois de atualizar, reinicie sua sessão de agente (ou reconecte o cliente MCP)** — a lista de ferramentas MCP é enumerada no início da sessão, então as ferramentas novas (12 agora, antes 9) só aparecem após um reinício. Nenhuma mudança de configuração é necessária; `tasks.json` é criado automaticamente no primeiro uso.

## Roadmap

- **v0.3.0** (atual) — quadro de tarefas + kanban web: `task_create` / `task_move` / `task_list` com uma máquina de estados estrita todo→doing→review→done; criar ou mover um cartão avisa automaticamente o responsável, então o movimento do quadro desperta agentes sem qualquer sondagem. `--web 8643` serve uma interface kanban sem dependências protegida por token onde o arraste humano passa pelo mesmo caminho de despertar. Mensagens + tarefas + despertar + quadro, ainda zero dependências.
- **v0.4.0** — talvez: integrações kanban mais profundas (Kaneo como referência/concorrente). Em discussão.
- **Próximo** — federação: transporte HTTP streamable para agentes em outras máquinas (amigável a Tailscale/LAN); recibos assinados (ed25519) para entrega à prova de adulteração.
- **v1.0.0** — ponte entre organizações: threads locais alcançam agentes em outras máquinas e organizações sobre infraestrutura de e-mail padrão, com o mesmo ciclo de vida do mailbox.

## Licença

MIT

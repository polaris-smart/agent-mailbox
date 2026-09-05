# agent-mailbox

**Собственный почтовый ящик для каждого локального ИИ-агента.** Один MCP-сервер stdio. Ноль демонов. Один JSON-файл на сообщение.

📖 **Докум.**: [English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

---

## Проблема

Запускаете несколько ИИ-агентов на одной машине — Claude Code, Hermes, собственные скрипты — и им негде оставлять друг другу сообщения. Они ждут друг друга, а вы в итоге копируете и вставляете между их окнами, как человеческий коммутатор.

## Решение

Почтовый ящик — это каталог простых JSON-файлов:

```
~/.agent-mail/
  registry.json                agent_id → {owner, description, created_at}
  inbox/HS/20260905-….json     один файл на сообщение
  archive/HS/…
```

Агенты читают и пишут через небольшой MCP-сервер stdio. Ни процесса-брокера, ни портов, ни базы данных, ни сети по умолчанию. Любое число MCP-хостов безопасно разделяют один почтовый корень (файловая блокировка).

## Быстрый старт

**Предварительные требования** — один раз: установите [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh` на macOS/Linux или `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` на Windows). Всё остальное запускает `uvx`; больше ничего ставить не нужно.

### 1 · Зарегистрируйте сервер в вашем MCP-хосте

Claude Code:

```bash
claude mcp add agent-mailbox -- uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

Любой MCP-хост (общий JSON):

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

Совет: задайте `AGENT_MAIL_ID=HS` (или любой id) в окружении агента — все инструменты станут самоадресованными, не нужно передавать `agent_id` в каждом вызове.

### 2 · Агенты регистрируются один раз

```json
{ "tool": "mailbox_register", "arguments": { "agent_id": "HS", "owner": "Hermes", "description": "PM & QA" } }
```

Регистрация идемпотентна. Каждый зарегистрированный агент немедленно адресуем всеми — включая человеческий id `boss`, который вы можете читать сами.

### 3 · Отправить, проверить, ответить

```json
{ "tool": "mailbox_send", "arguments": { "to": "HS", "subject": "deploy ready", "body": "v0.1.0 готова, проверьте." } }
{ "tool": "mailbox_check", "arguments": {} }
{ "tool": "mailbox_reply", "arguments": { "msg_id": "20260905-…-hs", "body": "проверено, отмечено done." } }
```

`mailbox_check` забирает ожидающие сообщения и помечает их `acked`. Жизненный цикл: `pending → acked → done`, затем архивация по желанию. Каждое сообщение — JSON-файл, который можно `cat` — босс читает входящие напрямую.

### 4 · Ждать, а не опрашивать

`mailbox_wait` блокируется (long-poll), пока не придёт сообщение — вызывайте его последним действием хода:

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

## Разбудить спящего агента (одна строка конфига)

Если агент-получатель даже не запущен, сам `mailbox_send` может отправлять каждое новое сообщение на webhook в момент записи — без демонов, без опроса, без лишних процессов:

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

Секрет сгенерируйте один раз: `openssl rand -hex 32`. Можно опустить — тогда POST без подписи (для локальных тестов достаточно; требовать проверку решает получатель).

Обработчик вебхуков хоста получает:

```json
{ "event": "agent_mailbox_new_message", "event_type": "agent_mailbox_new_message", "message": { "id": "…", "from": "ZC", "to": "HS", "subject": "…", "body": "…" } }
```

…будит агента, и агент по прибытии вызывает `mailbox_check`. Вот и вся интеграция.

- Подпись `X-Hub-Signature-256: sha256=<hmac>` (схема GitHub — принимается Hermes gateway и большинством потребителей вебхуков).
- Цель фиксируется: только http/https, по умолчанию loopback/приватные адреса, перенаправления отклонены, системный прокси обходится.
- Переменные окружения `AGENT_MAIL_WEBHOOK_URL` / `AGENT_MAIL_WEBHOOK_SECRET` имеют приоритет над файлом. Не задано → полностью офлайн.

## Инструменты

| Инструмент | Заметки |
|------------|---------|
| `mailbox_register(agent_id, owner?, description?)` | занимает ящик; идемпотентно |
| `mailbox_send(to, subject, body, priority?)` | `to` = один id, список или `"all"` |
| `mailbox_check(agent_id?, mark?)` | забирает ожидающие (→ `acked`) |
| `mailbox_reply(msg_id, body)` | маршрутизирует обратно исходному отправителю |
| `mailbox_list(agent_id?, status?)` | список сообщений, фильтр по статусу |
| `mailbox_done(msg_id)` | отметить обработанным |
| `mailbox_broadcast(subject, body)` | всем зарегистрированным агентам |
| `mailbox_whoami()` | справочник агентов + корень почты |
| `mailbox_wait(agent_id?, timeout_seconds?)` | long-poll новых сообщений |

## Опционально: уведомления на рабочий стол для людей

Компаньон-наблюдатель печатает каждое новое сообщение строкой JSON и посылает уведомления на рабочий стол (macOS / Linux / Windows). Он никогда не участвует в пробуждении агентов — агентам он не нужен:

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss
```

| Платформа | Установка | Проверка |
|-----------|-----------|----------|
| macOS (launchd) | `scripts/install-watch-macos.sh --notify boss` | `tail -f ~/.agent-mail/watch.log` |
| Linux (systemd user) | `scripts/install-watch-linux.sh …` | `journalctl --user -u agent-mailbox-watch -f` |
| Windows (schtasks) | `scripts\install-watch-windows.ps1` | `schtasks /Query /TN AgentMailboxWatch /V` |

## Дизайн

- **Local-first** — простые JSON-файлы в `~/.agent-mail/`. Без SMTP, без IMAP, без домена, без облачного релея, без сети по умолчанию.
- **Адресация одной регистрацией** — `mailbox_register("HS")` — и всё; каждый зарегистрированный агент сразу адресуем всеми.
- **Ноль внешних зависимостей** — только `mcp`. Хранилище — один Python-файл с атомарной записью под `flock`.
- **Читаемо человеком** — каждое сообщение — маленький JSON, который можно `cat`. Босс читает входящие напрямую.
- **Уважает существующие идентичности** — задайте `AGENT_MAIL_ID` в окружении агента, и его инструменты станут самоадресованными.

## Заметки по безопасности

- Корень почты лежит в вашем домашнем каталоге; сообщения никогда не покидают машину, если вы явно не включите webhook, по умолчанию привязанный к loopback/приватным целям.
- id агентов строго валидируются (`[A-Za-z0-9_-]`, ≤64 символов) — без path traversal.
- Хранилище добавляющее, с атомарной записью и блокировками; упавший писатель не портит реестр.
- Полезная нагрузка вебхуков подписана HMAC; проверяющим следует использовать сравнение с постоянным временем.
- Подписанные квитанции (ed25519) — в дорожной карте.

## Разработка

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest
```

## Дорожная карта

- **v0.1.0** (текущая) — почтовые ящики агентов на одной машине через stdio MCP. Ноль инфраструктуры. Long-poll `mailbox_wait`, встроенный webhook-пробуждение в `mailbox_send`, опциональный наблюдатель.
- **v0.2.0** — федерация: streamable HTTP транспорт для агентов на других машинах (дружелюбно к Tailscale/LAN).
- **v0.3.0** — подписанные квитанции (ed25519).
- **v1.0.0** — межорганизационный мост: локальные цепочки достигают агентов на других машинах и в организациях через стандартную почтовую инфраструктуру, с тем же жизненным циклом ящика.

## Лицензия

MIT

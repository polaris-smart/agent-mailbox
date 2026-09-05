# agent-mailbox

**ローカルのすべての AI エージェントに、専用のメールボックスを。** stdio MCP サーバーはひとつ。デーモン不要。メッセージは 1 通 = JSON ファイル 1 つ。

📖 **Docs**: [English](README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md) · [Español](README.es.md)

---

## 課題

1 台のマシンで複数の AI エージェント（Claude Code、Hermes、自作スクリプト）を動かしても、互いにメッセージを残す手段がありません。エージェント同士が待ち合ったり、あなたがウィンドウ間のコピー＆ペースト係になったりしがちです。

## 解決策

メールボックスの実体は、ただの JSON ファイルのディレクトリです：

```
~/.agent-mail/
  registry.json                agent_id → {owner, description, created_at}
  inbox/HS/20260905-….json     メッセージ 1 通 = ファイル 1 つ
  archive/HS/…
```

エージェントは小型の stdio MCP サーバーを通して読み書きします。ブローカープロセスなし、ポート開放なし、データベースなし、デフォルトでネットワーク無し。複数の MCP ホストプロセスがファイルロック付きでひとつのメールルートを安全に共有します。

## クイックスタート

### 1 · MCP ホストにサーバーを登録

Claude Code：

```bash
claude mcp add agent-mailbox -- uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

汎用 MCP ホスト（JSON）：

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

ヒント：エージェントの環境に `AGENT_MAIL_ID=HS` を一度設定すれば、全ツールが自分宛てとして動作し、毎回 `agent_id` を渡す必要がなくなります。

### 2 · エージェントは一度だけ登録

```json
{ "tool": "mailbox_register", "arguments": { "agent_id": "HS", "owner": "Hermes", "description": "PM & QA" } }
```

登録は冪等。登録済みのエージェントは全員から即座にアドレス指定可能 — 人間が直接読める `boss` 用メールボックスも含まれます。

### 3 · 送信・受信・返信

```json
{ "tool": "mailbox_send", "arguments": { "to": "HS", "subject": "deploy ready", "body": "v0.1.0 をステージングしました。検証をお願いします。" } }
{ "tool": "mailbox_check", "arguments": {} }
{ "tool": "mailbox_reply", "arguments": { "msg_id": "20260905-…-hs", "body": "検証 OK、done にしました。" } }
```

`mailbox_check` は未読メッセージを取得して `acked` にします。ライフサイクル：`pending → acked → done`、その後アーカイブ可能。メッセージは `cat` できる小さな JSON ファイル — ボスは受信箱を直接読めます。

### 4 · ポーリングではなく待機

`mailbox_wait` はメッセージが届くまでロングポーリングでブロックします — ターンの最後のアクションとして呼びます：

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

## 寝ているエージェントの起こし方（設定 1 行）

受信側のエージェントが起動していない場合でも、`mailbox_send` は新着メッセージを着信瞬間に webhook へ POST できます — デーモン不要、ポーリング不要、追加プロセス不要：

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

ホストの webhook ハンドラが受信：

```json
{ "event": "agent_mailbox_new_message", "event_type": "agent_mailbox_new_message", "message": { "id": "…", "from": "ZC", "to": "HS", "subject": "…", "body": "…" } }
```

…エージェントを起こし、到着したエージェントが `mailbox_check` を呼ぶ。統合はこれで全部です。

- 署名 `X-Hub-Signature-256: sha256=<hmac>`（GitHub 方式 — Hermes gateway をはじめ多くの webhook 消費者がそのまま受け付けます）。
- 宛先は固定：http/https のみ、デフォルトはループバック/プライベートアドレスのみ、リダイレクト拒否、システムプロキシ経由しない。
- 環境変数 `AGENT_MAIL_WEBHOOK_URL` / `AGENT_MAIL_WEBHOOK_SECRET` はファイルより優先。未設定 = 完全オフライン。

## ツール一覧

| ツール | メモ |
|--------|------|
| `mailbox_register(agent_id, owner?, description?)` | メールボックス取得。冪等 |
| `mailbox_send(to, subject, body, priority?)` | `to` = 1 つの id、リスト、または `"all"` |
| `mailbox_check(agent_id?, mark?)` | 未読取得（→ `acked`） |
| `mailbox_reply(msg_id, body)` | 元の送信者へ自動ルーティング |
| `mailbox_list(agent_id?, status?)` | 一覧。ステータス絞り込み可 |
| `mailbox_done(msg_id)` | 処理済みマーク |
| `mailbox_broadcast(subject, body)` | 登録済み全エージェントへ |
| `mailbox_whoami()` | エージェント一覧 + メールルート |
| `mailbox_wait(agent_id?, timeout_seconds?)` | 新着をロングポーリング |

## オプション：人間向けデスクトップ通知

補助のウォッチャーは新着メッセージを JSON 行として出力し、デスクトップ通知（macOS / Linux / Windows）を出します。エージェントの起床経路には決して載っていません — エージェントには不要です：

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss
```

| プラットフォーム | インストール | 検証 |
|------------------|--------------|------|
| macOS (launchd) | `scripts/install-watch-macos.sh --notify boss` | `tail -f ~/.agent-mail/watch.log` |
| Linux (systemd user) | `scripts/install-watch-linux.sh …` | `journalctl --user -u agent-mailbox-watch -f` |
| Windows (schtasks) | `scripts\install-watch-windows.ps1` | `schtasks /Query /TN AgentMailboxWatch /V` |

## 設計

- **ローカルファースト** — `~/.agent-mail/` の素の JSON ファイル。SMTP も IMAP もドメインもクラウド中継もなし、デフォルトでネットワーク無し。
- **登録一回のアドレッシング** — `mailbox_register("HS")` だけで全員から到達可能。
- **外部依存ゼロ** — 依存は `mcp` のみ。ストアは `flock` でアトミックに保護された単一 Python ファイル。
- **人間が読める** — メッセージは `cat` できる小さな JSON。ボスが受信箱を直接読めます。
- **既存のアイデンティティを尊重** — `AGENT_MAIL_ID` を一度設定すれば全ツールが自己宛て。

## セキュリティノート

- メールルートはホームディレクトリ内。webhook（デフォルトでループバック/プライベートに固定）を明示的に有効にしない限り、メッセージがマシンの外に出ることはありません。
- agent id は厳格に検証（`[A-Za-z0-9_-]`、≤64 文字）— パストラバーサルなし。
- ストアは追記指向のアトミック書き込み + ファイルロック。クラッシュしてもレジストリは破損しません。
- webhook ペイロードは HMAC 署名付き。検証側は定数時間比較を使ってください。
- 改ざん検出用の署名済みレシート（ed25519）は roadmap 上。

## 開発

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest
```

## Roadmap

- **v0.1.0**（現行）— 同一マシンのエージェントメールボックス、stdio MCP。インフラゼロ。ロングポーリング `mailbox_wait`、`mailbox_send` 内蔵 webhook 起こし、オプションのウォッチャー。
- **v0.2.0** — フェデレーション：他マシンのエージェント向け streamable HTTP トランスポート（Tailscale/LAN 向け）。
- **v0.3.0** — 署名済みレシート（ed25519）。
- **v1.0.0** — 組織間ブリッジ：標準メールインフラ経由で他マシン・他組織のエージェントへ、同じメールボックスライフサイクルのまま。

## ライセンス

MIT

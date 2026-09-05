# agent-mailbox

**ローカルのすべての AI エージェントに、専用のメールボックスを。**

MCP サーバーはひとつ。一度登録すれば、このマシン上のどのエージェントともメッセージをやり取りできます。cron 不要。ポーリングデーモン不要。共有 Markdown ファイル不要。クラウド不要。

📖 **Docs**: [English](README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md) · [Español](README.es.md) — [アーキテクチャ図](docs/architecture.html) · [English](docs/architecture-en.html)

![agent-mailbox アーキテクチャ](docs/architecture.png)

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox          # stdio transport、あらゆる MCP ホストで即使用可能
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox --http 8642   # または HTTP で公開し、リモートエージェントに対応
```

---

## 問題

コーディングエージェント、運用エージェント、レビューエージェント —— すべて同じマシンで動いているのに、互いに会話できません。そこで**あなた**が伝書鳩になります：あるターミナルから結論をコピーし、別のターミナルに指示を貼り付け、ステータス更新を手動で中継する。

ファイルベースの代 workarounds（共有 Markdown「ログ」や `dropped-notes/` フォルダ）は、いつか読めない記録の山に腐ります。cron スキャン式の回避策は、空ポーリングでトークンを無駄に燃やします。クラウドリレーは、ワークフローデータを他人の API の向こう側に置いてしまいます。

## 解決策

**それ自体がただのツール**であるメールボックス：

| ツール | 機能 |
|---|---|
| `mailbox_register` | メールボックスを取得。冪等。 |
| `mailbox_send` | 1 人、複数人、または `"all"` で全員に配信。 |
| `mailbox_check` | 未読メッセージを取得 —— 読むと自動 ack。 |
| `mailbox_reply` | スレッド内で返信、送信者へ自動ルーティング。 |
| `mailbox_list` | ステータス別に閲覧（`pending` / `acked` / `done`）。 |
| `mailbox_done` | 処理済みをマーク；done メッセージは自動アーカイブ。 |
| `mailbox_broadcast` | 1 回の呼び出しで、登録済みの全エージェントへ。 |
| `mailbox_whoami` | 誰が登録済みか、メールルートはどこか。 |

メッセージはプレーンな JSON で、ライフサイクルは極めてシンプル：`pending → acked → done`。受信者がオフラインの間、メッセージは静かに待ちます —— メールはあるべき姿で。

## クイックスタート

**Hermes** (`~/.hermes/config.yaml`)：

```yaml
mcp:
  servers:
    agent-mailbox:
      command: uvx
      args: ["git+https://github.com/polaris-smart/agent-mailbox"]
```

**Claude Code** (`~/.claude/settings.json`)：

```json
{ "mcpServers": { "agent-mailbox": { "command": "uvx", "args": ["--from", "git+https://github.com/polaris-smart/agent-mailbox", "agent-mailbox"] } } }
```

**任意の MCP クライアント** (stdio)：

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

**リモートエージェント**（別サーバー上のエージェントなど）：

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox --http 8642   # メールホスト上で
```

```json
{ "mcpServers": { "agent-mailbox": { "url": "http://your-host:8642/mcp" } } }
```

## メールを待つ（ポーリングなし）

エージェントはポーリングする必要がありません。`mailbox_wait` はメッセージが到着するまでブロック（ロングポール）します —— ターンの最後のアクションとして呼べば、次のメッセージが即座にエージェントを起こします：

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

人間とダッシュボードのために、専用ウォッチャーが新しいメッセージを JSON 行として出力し、指定エージェントに macOS 通知を送ることもできます：

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss      # macOS 通知センター
agent-mailbox-watch --once                     # 単一スキャン（cron 向け）
```

## 設計

- **ローカルファースト** —— `~/.agent-mail/` 以下のプレーンな JSON ファイル。SMTP も IMAP もドメインもクラウドリレーも不要、ネットワーク接続もなし（HTTP transport を意図的に有効化しない限り）。
- **ゼロ依存** —— Python 標準ライブラリのみ。約 500 行のコードベース、一度読めばわかります。
- **MCP ネイティブ** —— 単なる別の CLI プラグインではありません：あらゆる MCP ホストが登録可能。
- **登録制のアイデンティティ** —— `mailbox_register` 一度で、永続的なアイデンティティ。セッション終了で消えません。

ローカルメールボックスは同一マシンおよび信頼された LAN 内の調整を解決します。メッセージライフサイクル（`pending → acked → done`）は、エージェントのスレッドが実際のメールインフラ経由で他のマシンや組織に届く必要が生じても、そのまま引き継げるように設計されています。

## セキュリティに関する注意

- メールルートはホームディレクトリにあります。信頼されたネットワークで HTTP transport を意図的に有効にしない限り、メッセージがマシンの外に出ることはありません。
- エージェント ID は厳格に検証されます（`[A-Za-z0-9_-]`、64 文字以下）—— パストラバーサルなし。
- ストアは追記指向、アトミック書き込み＋ファイルロック。ライターがクラッシュしてもレジストリは破損しません。
- 改ざん耐性のあるレシート（ed25519 署名）はロードマップ上にあります。

## ロードマップ

- **v0.1.0**（現在）—— stdio MCP による同一マシン上のエージェントメールボックス。インフラゼロ。ロングポール `mailbox_wait` と専用ウォッチャー付き —— ポーリングデーモンは不要。
- **v0.2.0** —— フェデレーション：他のマシンのエージェント向け streamable HTTP transport（Tailscale/LAN フレンドリー）。
- **v0.3.0** —— 署名付きレシート（ed25519）による改ざん耐性のある配信。
- **v1.0.0** —— 組織間ブリッジ：標準メールインフラ経由で、同じメールボックスライフサイクルのままローカルスレッドを他マシン・他組織へ届ける。

姉妹プロジェクト：[dsh-devices](https://github.com/polaris-smart/dsh-devices) はデバイスを管理し、agent-mailbox はその上のエージェント間の会話を管理します。

## 開発

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
pip install -e ".[dev]"
pytest
```

## ライセンス

MIT — [LICENSE](LICENSE) を参照。

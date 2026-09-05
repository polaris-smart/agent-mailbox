# agent-mailbox

**Une boîte aux lettres propre à chaque agent IA local.** Un serveur MCP stdio. Zéro démon. Un fichier JSON par message.

📖 **Docs** : [English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

---

## Le problème

Faire tourner plusieurs agents IA sur une même machine — Claude Code, Hermes, vos propres scripts — et ils n'ont aucun moyen de se laisser des messages. Ils s'attendent mutuellement, ou vous finissez par copier-coller entre leurs fenêtres comme un standardiste humain.

## La solution

Une boîte aux lettres est un répertoire de fichiers JSON simples :

```
~/.agent-mail/
  registry.json                agent_id → {owner, description, created_at}
  inbox/HS/20260905-….json     un fichier par message
  archive/HS/…
```

Les agents la lisent et l'écrivent via un petit serveur MCP stdio. Aucun processus broker, aucun port, aucune base de données, aucun réseau par défaut. Autant de processus hôtes MCP que vous voulez partagent une même racine de courrier en toute sécurité (verrou de fichier).

## Démarrage rapide

### 1 · Enregistrez le serveur auprès de votre hôte MCP

Claude Code :

```bash
claude mcp add agent-mailbox -- uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

Tout hôte MCP (JSON générique) :

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

Astuce : définissez `AGENT_MAIL_ID=HS` (ou l'id de votre choix) dans l'environnement de l'agent et tous les outils s'auto-adressent — plus besoin de passer `agent_id` à chaque appel.

### 2 · Les agents s'enregistrent une fois

```json
{ "tool": "mailbox_register", "arguments": { "agent_id": "HS", "owner": "Hermes", "description": "PM & QA" } }
```

L'enregistrement est idempotent. Tout agent enregistré est immédiatement adressable par tous — y compris un id humain `boss` que vous pouvez lire vous-même.

### 3 · Envoyer, consulter, répondre

```json
{ "tool": "mailbox_send", "arguments": { "to": "HS", "subject": "deploy ready", "body": "v0.1.0 est prête, merci de vérifier." } }
{ "tool": "mailbox_check", "arguments": {} }
{ "tool": "mailbox_reply", "arguments": { "msg_id": "20260905-…-hs", "body": "vérifié, marqué done." } }
```

`mailbox_check` récupère les messages en attente et les marque `acked`. Cycle de vie : `pending → acked → done`, puis archivage optionnel. Chaque message est un JSON que vous pouvez `cat` — le patron lit la boîte de réception directement.

### 4 · Attendre plutôt que sonder

`mailbox_wait` bloque (long-poll) jusqu'à l'arrivée d'un message — appelez-le en dernière action du tour :

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

## Réveiller un agent endormi (une ligne de config)

Si l'agent destinataire n'est même pas en cours d'exécution, `mailbox_send` peut lui-même POSTer chaque nouveau message vers un webhook dès qu'il atterrit — pas de démon, pas de sondage, pas de processus supplémentaire :

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

Le gestionnaire de webhooks de l'hôte reçoit :

```json
{ "event": "agent_mailbox_new_message", "event_type": "agent_mailbox_new_message", "message": { "id": "…", "from": "ZC", "to": "HS", "subject": "…", "body": "…" } }
```

…réveille l'agent, et l'agent appelle `mailbox_check` en arrivant. Toute l'intégration est là.

- Signé `X-Hub-Signature-256: sha256=<hmac>` (schéma GitHub — accepté par Hermes gateway et la plupart des consommateurs de webhooks).
- La cible est épinglée : http/https uniquement, adresses loopback/privées par défaut, redirections refusées, proxy système contourné.
- Les variables d'environnement `AGENT_MAIL_WEBHOOK_URL` / `AGENT_MAIL_WEBHOOK_SECRET` priment sur le fichier. Non défini → totalement hors ligne.

## Les outils

| Outil | Remarques |
|-------|-----------|
| `mailbox_register(agent_id, owner?, description?)` | revendique une boîte ; idempotent |
| `mailbox_send(to, subject, body, priority?)` | `to` = un id, une liste, ou `"all"` |
| `mailbox_check(agent_id?, mark?)` | récupère les pendants (→ `acked`) |
| `mailbox_reply(msg_id, body)` | re-route vers l'expéditeur d'origine |
| `mailbox_list(agent_id?, status?)` | liste les messages, filtre de statut optionnel |
| `mailbox_done(msg_id)` | marque comme traité |
| `mailbox_broadcast(subject, body)` | à tous les agents enregistrés |
| `mailbox_whoami()` | annuaire des agents + racine du courrier |
| `mailbox_wait(agent_id?, timeout_seconds?)` | long-poll du courrier nouveau |

## Optionnel : notifications de bureau pour les humains

Un watcher compagnon imprime chaque nouveau message en ligne JSON et déclenche des notifications de bureau (macOS / Linux / Windows). Il n'est jamais dans le chemin de réveil des agents — les agents n'en ont pas besoin :

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss
```

| Plateforme | Installer | Vérifier |
|------------|-----------|----------|
| macOS (launchd) | `scripts/install-watch-macos.sh --notify boss` | `tail -f ~/.agent-mail/watch.log` |
| Linux (systemd user) | `scripts/install-watch-linux.sh …` | `journalctl --user -u agent-mailbox-watch -f` |
| Windows (schtasks) | `scripts\install-watch-windows.ps1` | `schtasks /Query /TN AgentMailboxWatch /V` |

## Conception

- **Local-first** — fichiers JSON simples sous `~/.agent-mail/`. Pas de SMTP, pas d'IMAP, pas de domaine, pas de relais cloud, pas de réseau par défaut.
- **Adressage en un enregistrement** — `mailbox_register("HS")` suffit ; tout agent enregistré est immédiatement adressable par tous.
- **Zéro dépendance externe** — uniquement `mcp`. Le magasin est un fichier Python avec écritures atomiques protégées par `flock`.
- **Lisible par les humains** — chaque message est un petit JSON que vous pouvez `cat`. Le patron lit la boîte directement.
- **Respecte les identités existantes** — définissez `AGENT_MAIL_ID` dans l'environnement de chaque agent et ses outils s'auto-adressent.

## Notes de sécurité

- La racine de courrier vit dans votre répertoire personnel ; les messages ne quittent jamais la machine sauf si vous activez le webhook, épinglé par défaut aux cibles loopback/privées.
- Les ids d'agents sont strictement validés (`[A-Za-z0-9_-]`, ≤64 caractères) — pas de path traversal.
- Le magasin est orienté ajout avec écritures atomiques et verrous ; un rédacteur qui plante ne corrompt pas le registre.
- Les payloads webhook sont signés HMAC ; les vérificateurs doivent utiliser une comparaison en temps constant.
- Les reçus signés (ed25519) sont sur la feuille de route.

## Développement

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest
```

## Feuille de route

- **v0.1.0** (actuelle) — boîtes aux lettres pour agents sur une même machine via stdio MCP. Zéro infrastructure. Long-poll `mailbox_wait`, webhook de réveil intégré à `mailbox_send`, watcher optionnel.
- **v0.2.0** — fédération : transport HTTP streamable pour les agents sur d'autres machines (Tailscale/LAN friendly).
- **v0.3.0** — reçus signés (ed25519).
- **v1.0.0** — pont inter-organisations : les fils locaux joignent des agents sur d'autres machines et organisations via l'infrastructure e-mail standard, avec le même cycle de vie.

## Licence

MIT

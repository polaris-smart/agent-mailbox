# agent-mailbox

[![polaris-smart/agent-mailbox MCP server](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox/badges/score.svg)](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox)

**Une boîte aux lettres propre à chaque agent IA local.** Un serveur MCP stdio. Zéro démon. Un fichier JSON par message. Plus un tableau de tâches intégré : les cartes réveillent leur responsable dès qu'elles bougent, et un kanban web sans dépendance pour l'humain.

📖 **Docs** : [English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

> 🆕 **v0.3.0 — Tableau de tâches** : les agents partagent désormais une surface de tâches sur la même racine de courrier. 3 nouveaux outils MCP (12 au total), un tableau en glisser-déposer sans dépendance (`--web`), et chaque mouvement prévient le responsable. ⚠️ **Note de mise à niveau** : redémarrez votre session d'agent pour charger les nouveaux outils. → [Tableau de tâches](#tableau-de-tâches)

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
  tasks.json                   le tableau de tâches ({"next_id", "tasks": {id: carte}})
```

Les agents la lisent et l'écrivent via un petit serveur MCP stdio. Aucun processus broker, aucun port, aucune base de données, aucun réseau par défaut. Autant de processus hôtes MCP que vous voulez partagent une même racine de courrier en toute sécurité (verrou de fichier).

## Démarrage rapide

**Prérequis** — une seule fois : installez [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh` sur macOS/Linux, ou `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` sur Windows). `uvx` exécute tout le reste ; rien d'autre à installer.

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

## Tableau de tâches

Les cartes vivent dans `<racine du courrier>/tasks.json` (JSON brut, le même verrou de fichier que le courrier). La machine à états est stricte : `todo→doing→review→done`, les sauts non adjacents sont refusés sauf `force=True`, et `done` est terminal. Créer ou déplacer une carte envoie au responsable un message ordinaire du courrier (`[task#t-12 → review] …`) — le mouvement du tableau réveille l'agent par la boîte existante, sans sondage ni webhook. Les déplacements que l'on se fait à soi-même restent silencieux, et `notify=False` les désactive.

**Tableau web (pour l'humain).** `agent-mailbox --web 8643` sert un kanban sans dépendance (`http.server` de la stdlib + une seule page HTML intégrée, pas de framework) sur `127.0.0.1`. Quatre colonnes reflètent la machine à états ; faites glisser une carte entre colonnes adjacentes pour la déplacer, ou créez des cartes via le formulaire, avec une bascule entre thème clair et sombre. L'authentification est un token bearer — définissez `AGENT_MAIL_WEB_TOKEN` pour un token fixe, sinon un nouveau token est généré et affiché à chaque démarrage (ouvrez `http://127.0.0.1:8643/?token=…`). Le tableau agit comme agent `boss` : chaque carte que vous créez ou faites glisser prévient toujours le responsable — chaque mouvement lui envoie un message. La page se rafraîchit toutes les 5 secondes.

## Réveiller un agent endormi (une ligne de config)

Si l'agent destinataire n'est même pas en cours d'exécution, `mailbox_send` peut lui-même POSTer chaque nouveau message vers un webhook dès qu'il atterrit — pas de démon, pas de sondage, pas de processus supplémentaire :

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

Générez le secret une fois : `openssl rand -hex 32`. Omettez-le pour des POST non signés (suffisant en test local ; c'est au récepteur d'exiger la vérification).

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
| `task_create(title, assignee, due?)` | crée une carte de tâche (démarre en `todo`) ; prévient le responsable |
| `task_move(task_id, status, assignee?, note?, force?)` | avance le long de `todo→doing→review→done` (les sauts exigent `force`) ; déplacer une carte prévient son responsable |
| `task_list(assignee?, status?)` | liste les cartes de tâche, filtres optionnels |

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

## Mise à niveau

Mettez à niveau avec `uv tool upgrade agent-mailbox` (ou réinstallez selon votre méthode d'origine).

⚠️ **Après la mise à niveau, redémarrez votre session d'agent (ou reconnectez le client MCP)** — la liste des outils MCP est énumérée au démarrage de la session ; les nouveaux outils (12 désormais, contre 9 avant) n'apparaissent qu'après un redémarrage. Aucun changement de configuration ; `tasks.json` est créé automatiquement au premier usage.

## Feuille de route

- **v0.3.1** (actuelle) — lot de correctifs : les objets de réponse n'accumulent plus de `Re: Re:` (première réponse, ré-réponses et préfixes à casse mixte normalisés en un seul `Re:`) ; les tokens du tableau web utilisent une comparaison à temps constant (`hmac.compare_digest`) et persistent entre les redémarrages (`~/.agent-mail/web_token`, mode 0600, la variable d'environnement `AGENT_MAIL_WEB_TOKEN` a toujours priorité) ; `sent.log` effectue une rotation automatique d'une génération au-delà de 10 Mo (vers `sent.log.1`).
- **v0.3.0** — tableau de tâches + kanban web : `task_create` / `task_move` / `task_list` avec une machine à états stricte todo→doing→review→done ; créer ou déplacer une carte prévient automatiquement le responsable, le mouvement du tableau réveille donc les agents sans aucun sondage. `--web 8643` sert une interface kanban sans dépendance protégée par token où le glisser-déposer humain passe par le même chemin de réveil. Messages + tâches + réveil + tableau, toujours zéro dépendance.
- **v0.4.0** — peut-être : intégrations kanban plus poussées (Kaneo comme référence/concurrent). En discussion.
- **Ensuite** — fédération : transport HTTP streamable pour les agents sur d'autres machines (Tailscale/LAN friendly) ; reçus signés (ed25519) pour une livraison infalsifiable.
- **v1.0.0** — pont inter-organisations : les fils locaux joignent des agents sur d'autres machines et organisations via l'infrastructure e-mail standard, avec le même cycle de vie.

## Licence

MIT

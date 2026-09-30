<!-- mcp-name: io.github.polaris-smart/agent-mailbox -->
<p align="center"><img src="assets/brand/png/logo-readme.png" width="360" alt="agent-mailbox"></p>

# agent-mailbox

[![polaris-smart/agent-mailbox MCP server](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox/badges/score.svg)](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox)
[![npm](https://img.shields.io/npm/v/agent-mailbox)](https://www.npmjs.com/package/agent-mailbox)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Une vraie boîte aux lettres pour vos agents IA — et vous en êtes le propriétaire.** Des agents sur des CLIs différents (Claude Code, Codex, Gemini CLI, Hermes, WorkBuddy…) s'écrivent de façon asynchrone sur une même machine, avec garantie de livraison, réveils et une boîte de réception humaine à trois volets où chaque conversation est visible. Zéro dépendance, zéro cloud, zéro clé API.

> **📊 Éprouvé en production** : plus de 1 676 messages entre 5 agents en 18 jours de développement multi-agents au quotidien — ~93 messages/jour, zéro perte.

| Boîte à trois volets | Assistant setup (autodécouverte) |
|---|---|
| <img src="docs/screenshots/mailbox-threepane.png" alt="boîte à trois volets" width="100%"/> | <img src="docs/screenshots/setup-wizard.png" alt="assistant setup" width="100%"/> |

Autres documents : [English](README.md) · [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Русский](README.ru.md)

---

## Installation en 3 étapes

**Prérequis** — une seule fois : installez [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`, ou `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` sous Windows). Tout le reste tourne via `uvx`.

```bash
# 1 · récupérez le CLI
uv tool install git+https://github.com/polaris-smart/agent-mailbox

# 2 · lancez l'assistant setup — il découvre vos agents pour vous
agent-mailbox setup          # ouvre l'assistant local dans votre navigateur
agent-mailbox setup --yes    # headless / serveur : valeurs par défaut, sans navigateur

# 3 · enregistrez le serveur MCP auprès de votre hôte d'agent
claude mcp add agent-mailbox -- agent-mailbox        # ou le JSON générique ci-dessous
```

L'assistant scanne automatiquement quatre couches — **ce qui est installé** (CLIs du PATH, /Applications, répertoires de config) → **ce qui est connecté** (configs MCP + annuaire du mailbox) → **comment réveiller chacun** (URL schemes des apps tirés du `Info.plist`, ports en écoute résolus par *chemin de l'exécutable*, commandes utilisables) → **et teste chaque canal** en envoyant une vraie lettre. Vous ne tapez rien ; ce qu'il ne sait pas identifier répond honnêtement `unrecognized`, au lieu de deviner.

<details>
<summary>JSON générique d'hôte MCP (tout hôte)</summary>

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

Astuce : définissez `AGENT_MAIL_ID=<id>` dans l'environnement d'un agent et tous les outils deviennent auto-adressés.
</details>

## Comment ça marche

![le voyage d'une lettre](docs/diagrams/how-it-works.png)

Un mailbox est un répertoire de fichiers JSON bruts — un fichier par lettre, lisible au `cat`, greppable, à vous :

```
~/.agent-mail/
  registry.json               agent_id → {kind, owner, description}
  inbox/HS/20260905-….json    un fichier par message
  archive/HS/…
  tasks.json                  le tableau de tâches
  audit.log                   chaque changement de visibilité, en append
```

Les agents dialoguent via un petit serveur MCP stdio (14 outils). Pas de processus broker, pas de ports, pas de base de données, pas de réseau par défaut. Autant d'hôtes MCP que vous voulez partagent une même racine de courrier en toute sécurité (protégée par flock).

**Réveiller un agent endormi.** Dès qu'une lettre atterrit, le mailbox ping l'hôte du destinataire via sa propre connexion MCP (sampling MCP) — le LLM *propre* de l'hôte lit la lettre et agit, sous une politique de réveil forcée (identité, tâche, liste d'interdictions stricte). **Le mailbox lui-même ne dépend d'aucun modèle et ne détient aucune clé API** — l'intelligence est empruntée à l'hôte sur lequel l'agent tourne déjà, et un kill switch par agent coupe le sampling à tout moment. Les agents CLI-seuls (codex…) passent par l'adaptateur local-command. Le sampling est un accélérateur, jamais une garantie de livraison : s'il échoue, la lettre atterrit quand même et la prochaine vérification livre quand même.

## L'humain est le propriétaire

![modèle de permissions](docs/diagrams/permission-model.png)

- **owner (vous)** — vous voyez tout : votre boîte de réception *plus* tout le trafic entre agents, dans la boîte web à trois volets. Vous lisez, répondez, recevez des tâches (« besoin de ta décision ») et actionnez les interrupteurs de visibilité — chaque changement atterrit dans `audit.log`.
- **agent** — uniquement sa propre boîte. Les lectures inter-boîtes reçoivent un `permission denied` structuré à la couche d'outils, jamais un résultat vide silencieux.
- **guest** — les expéditeurs inter-appareils/externes ont besoin d'un token d'appairage ; **le courrier d'origine externe atterrit mais ne réveille rien** tant que vous ne le confirmez pas (porte anti prompt-injection).
- **Lettres scellées** — le contenu n'est lisible que par les outils de l'agent destinataire ; toute vue humaine ne montre que les métadonnées. « Le propriétaire voit tout » ne doit jamais devenir un canal de fuite.
- **Trois niveaux d'attention** — l'expéditeur marque la lettre `decision` / `report` / `archive` ; par défaut, seul « besoin de ta décision » vous ping.

La boîte à trois volets (`agent-mailbox --web 8900`, `http.server` de la stdlib, toujours zéro dépendance) offre des dossiers, un volet de monitoring pour tout le trafic des agents, un annuaire des membres avec pastilles d'état par canal, des actions par lettre (répondre / transformer en carte de tâche / archiver), des raccourcis clavier (`j/k` déplacer · `e` archiver · `r` répondre · `t` tâche · `/` rechercher) et, sur écran vide, une action (« écrivez la première lettre à vos agents ») au lieu d'une page blanche.

## Pourquoi pas directement MCP / Slack / fichiers bruts ?

| Approche | Multi-CLI | Asynchrone | Réveil | Boîte humaine | Deps |
|---|---|---|---|---|---|
| **agent-mailbox** | ✅ tout hôte MCP | ✅ la boîte persiste | ✅ sampling + daemon | ✅ trois volets + tableau | **0** |
| Outils MCP bruts | ❌ sessions par CLI | ❌ perdu au redémarrage | ❌ | ❌ | — |
| Bot Slack/Discord | ✅ | ✅ | ✅ | ❌ | tokens API, cloud |
| Fichiers partagés + conventions | ✅ | ⚠️ ad-hoc | ❌ manuel | ❌ | votre propre code de verrouillage |

## CLI et outils

```bash
agent-mailbox setup [--yes]     # assistant en 3 étapes (découvrir → tester → prêt)
agent-mailbox discover [--json] # affiche uniquement le rapport de découverte
agent-mailbox status [--json]   # service + santé de canal par membre
agent-mailbox test <member>     # envoie une lettre de test et attend l'accusé
agent-mailbox connect <name>    # raccorde un membre (sauvegarde avant écriture)
agent-mailbox uninstall         # restaure chaque config touchée (diff = 0)
agent-mailbox --web 8900        # boîte humaine + tableau (localhost + token)
```

<details>
<summary><b>Les 14 outils MCP</b></summary>

| Outil | Remarques |
|------|-------|
| `mailbox_register(agent_id, owner?, description?)` | revendique un mailbox ; idempotent |
| `mailbox_send(to, subject, body, priority?, attention?, sealed?, links?)` | `to` = id / liste / `"all"` ; dédupliqué par défaut |
| `mailbox_check(agent_id?, mark?)` | récupère les pending (→ `acked`) |
| `mailbox_reply(msg_id, body)` | re-route vers l'expéditeur (exempté de dédup) |
| `mailbox_list(agent_id?, status?, thread?)` | liste avec filtres |
| `mailbox_thread(thread)` | rejoue un thread du plus ancien au plus récent, entre agents |
| `mailbox_done(msg_id)` | marque comme traité |
| `mailbox_broadcast(subject, body)` | à tous les agents enregistrés |
| `mailbox_whoami()` | annuaire des agents + racine de courrier |
| `mailbox_wait(agent_id?, timeout_seconds?)` | long-poll pour du courrier neuf |
| `mailbox_confirm_external(msg_id)` | l'owner confirme l'exécution d'une lettre d'origine externe |
| `task_create(title, assignee, due?)` | carte de tâche ; le désigné reçoit un message automatique |
| `task_move(task_id, status, …)` | `todo→doing→review→done` (les sauts exigent `force`) |
| `task_list(assignee?, status?)` | liste les cartes de tâche |

</details>

## La fiabilité, en bref

Les garanties de livraison sont le produit. À retenir : **suppression des doublons** (hash sémantique, répéter la même lettre sous 24 h renvoie `{"deduped": true}` sans effet de bord), **compensation des tâches à moitié faites** (handled-log en deux phases + `resume_plan` : process / replay / finalize / skip), **récupération des acked périmés** câblée dans la boucle de réveil, un **disjoncteur de réveil** après N rounds sans progrès, **protection anti auto-écho** et **threads de première classe** avec alerte thread-fantôme. Le côté réveil est fail-open par loi de fer : si le réveil meurt, le courrier arrive quand même.

<details>
<summary><b>Détails du système de réveil</b></summary>

- **Wake daemon** — `agent-mailbox wake install --agent ID` écrit des unités launchd `WatchPaths` (macOS) / systemd `PathChanged=` (Linux) sur la boîte de réception ; chaque changement déclenche un round de drain. Adaptateurs : `hermes` (webhook de gateway, signature auto-adaptée), `generic-webhook`, `claude-code` (cloche + toast), local-command (argv uniquement, contenu par env, killpg avec timeout — pour codex & cie). Les POST échoués réessaient 5×60 s puis repartent en file ; une entrée `wake` dans `handled_log` fait que chaque lettre réveille au plus une fois ; la sémantique de comptage v2 réveille sur tous les pending + acked>600 s.
- **Politique de sampling** — les sections par agent de `wake.json` portent identity / task / forbidden / `require_receipt` / `max_concurrent` ; `wake install` préserve les clés inconnues entre mises à niveau (votre politique survit). Kill switch par agent : `"sampling": {"enabled": false}`. MCP a déprécié le sampling le 2026-07-28 (SEP-2577) — la chaîne de secours (wake-daemon / webhook / prochaine vérification) tient la loi de fer d'arrivée du courrier.
- **Webhook** — `~/.agent-mail/webhook.json` POST chaque lettre qui atterrit (signée HMAC, styles github/generic/slack), épinglé à des cibles loopback/privées, redirections refusées.
- **Routeur Jev (optionnel, éteint)** — réveil à seuil de score avec digest quotidien groupé ; tout échec échoue ouvert.
- **Les releases sont balayées** — `scripts/verify_release.sh` vérifie le sdist + wheel réels contre les fuites de chemins/identité avant chaque tag.

</details>

## Fonctionne avec votre CLI d'agent

Tout ce qui parle MCP stdio peut héberger un mailbox — c'est-à-dire tous.

| CLI d'agent | Comment l'enregistrer |
|-----------|-----------------|
| Claude Code | `claude mcp add agent-mailbox -- agent-mailbox` |
| Codex CLI | `~/.codex/config.toml` → `[mcp_servers.agent-mailbox]` |
| Gemini CLI / Qwen Code | `settings.json` → `"mcpServers": { … }` |
| OpenCode | `opencode.json` → `"mcp": { … }` |
| Hermes / WorkBuddy / tout hôte MCP | le même JSON générique (ci-dessus) |

## Pour les agents IA (confiez cette section à votre agent)

Si vous êtes un agent IA qui lit ceci parce que votre utilisateur a dit « installe agent-mailbox » : lancez `agent-mailbox setup --yes`, enregistrez le serveur MCP selon le JSON ci-dessus, choisissez un id, définissez `AGENT_MAIL_ID=<id>`, appelez une fois `mailbox_register`, vérifiez avec `mailbox_whoami`. Tous les agents de cette machine peuvent dès lors vous adresser du courrier. Lisez l'anglais même si votre utilisateur ne le fait pas — les commandes sont identiques partout.

> **🆕 Points forts du changelog v0.7.x** — la v0.7.5 apporte le modèle de confiance (owner/agent/guest appliqué à la couche d'outils), les lettres scellées, la porte d'exécution pour origine externe, l'assistant /setup en 3 étapes avec autodécouverte en quatre couches, la boîte humaine à trois volets /mail et la page /visibility. La v0.7.4 durcit la chaîne de release (préservation des clés inconnues de wake.json, les lettres dupliquées ne réveillent plus, balayage des artefacts). La v0.7.2 ajoute le kill switch de sampling par agent (SEP-2577). La v0.7.0 a introduit le sampling wake + l'adaptateur local-command. 14 outils MCP. Historique complet : [Roadmap](#roadmap)

## Notes de sécurité

- La racine de courrier vit dans votre répertoire home ; les lettres ne quittent jamais la machine sauf si vous activez le webhook (épinglé à loopback/privé par défaut).
- Les ids d'agent sont validés strictement — pas de path traversal. La découverte est en lecture seule ; `connect` sauvegarde toute config avant d'écrire ; `uninstall` restaure avec une vérification de diff octet par octet.
- Pas de clé API, pas d'identifiants de modèle, pas de télémétrie — le mailbox n'en détient aucun.
- Les reçus signés (ed25519) sont sur la roadmap.

## Développement

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest          # 361 tests
ruff check src tests
```

## Mise à niveau

`uv tool upgrade agent-mailbox` (ou re-téléchargez comme vous l'aviez installé).

⚠️ **Redémarrez votre session d'agent (ou reconnectez le client MCP) après la mise à niveau** — les listes d'outils MCP sont énumérées au démarrage de la session, donc les nouveaux outils (14 désormais, contre 9 avant) n'apparaissent qu'après un redémarrage.

## Roadmap

- **v0.7.5** (actuelle) — le modèle de confiance : les membres portent un kind (`owner` humain / `agent` / `guest`) avec application à la couche d'outils (la lecture croisée reçoit un `permission denied` structuré, jamais un résultat vide silencieux) ; lettres scellées lisibles uniquement via les outils propres à l'agent destinataire (tous les autres voient métadonnées + `redacted: "sealed"`) ; la porte d'origine externe — le courrier externe atterrit mais ne réveille rien (webhook et sampling le sautent tous les deux) jusqu'à ce qu'un owner confirme via `mailbox_confirm_external` ; niveaux d'attention sur les lettres ; l'assistant /setup en 3 étapes, la boîte humaine à trois volets /mail (dossiers / monitoring / annuaire / actions par lettre) et la page /visibility branchées sur `config.json` + `audit.log`.
- **v0.7.4** — durcissement du réveil : `wake install` n'efface plus silencieusement les clés inconnues de `wake.json` (la section de politique de sampling par agent survit aux mises à niveau — P0) ; les lettres dupliquées ne réveillent plus (`wake_suppressed_dup`) ; les prompts de sampling wake portent le vrai compte de pending ; `scripts/verify_release.sh` balaie sdist + wheel contre des critères épinglés avant chaque tag.
- **v0.7.3** — hygiène du sdist, deuxième passe : le balayage des artefacts post-release a attrapé `scripts/wake-zc.sh` (un wrapper d'ops spécifique à la machine avec des chemins locaux codés en dur) embarqué dans les sdists 0.7.0–0.7.2 ; désormais exclu via `exclude` de hatchling — la wheel ne l'a jamais embarqué, la copie du dépôt reste (le câblage launchd intact).
- **v0.7.2** — durcissement SEP-2577 + hygiène de release : **kill switch de sampling par agent** (`"sampling": {"enabled": false}` dans la section par agent de `wake.json` — MCP a déprécié la capability sampling le 2026-07-28 ; les valeurs malformées échouent bruyamment dans `sampling.log`, et le courrier ne dépend jamais du sampling) ; **hygiène du sdist** (assets AOCI + fuites de chemins locaux exclus via `exclude` de hatchling + `.gitignore`) ; `__version__` suit désormais la version de pyproject.
- **v0.7.0** — la mise à niveau du réveil : **sampling wake** (`createMessage` piloté par le serveur sur la propre connexion MCP de l'hôte — injection de politique de réveil, verrou d'exécution par agent, timeout de 60 s, dédup par message, fail-open vers le mailbox), **adaptateur de réveil local-command** (argv uniquement, contenu injecté par env, killpg avec timeout pour les agents CLI à la demande comme codex), surcharge `wake run --adapter`. 13 outils MCP.
- **v0.6.2** — durcissement sécurité + clôtures v0.5.x : **SECURITY.md** (signalement de vulnérabilités via GitHub Security Advisories, versions supportées, le modèle de confiance locale déclaré au grand jour) ; **identity binding** (`identity_binding` optionnel dans `config.json` : les ids d'agent liés doivent présenter `AGENT_MAIL_TOKEN` — sha256 + `hmac.compare_digest` à temps constant — sinon les appels échouent avec `identity mismatch` ; désactivé par défaut, les agents non liés inchangés, une config malformée échoue bruyamment au démarrage) ; **`unread_count` dans les payloads webhook** (compte de pending du destinataire au moment de la notification, champ de premier niveau, pure addition) ; **sémantique de claim pour `mailbox_wait`** (`claim()` atomique sous verrou : les lettres reviennent `acked` + `claimed_by`, un second waiter ne re-consomme jamais un lot, les claims périmés meurent avec la moisson acked→pending).
- **v0.6.0** — le lot vedette : **wake daemon** (`agent-mailbox wake install` — launchd WatchPaths / systemd PathChanged déclenchent un round de drain ; adaptateurs hermes / generic-webhook / claude-code ; les POST échoués réessaient 5×60s puis repartent en file au prochain déclencheur, les entrées `wake` de `handled_log` font que chaque lettre réveille au plus une fois, sémantique de comptage v2 = tous les pending + acked>600s ; loi de fer fail-open de bout en bout) ; **threads de première classe** (`thread_id` frappé à l'envoi et hérité à la réponse, `mailbox_thread` rejoue entre agents par ordre temporel, filtre de liste `--thread`, rattrapage par clé de sujet des chaînes Re:, alerte thread fantôme au-delà de 5 lettres ouvertes) ; **bypass de routage Jev** (plugin de score optionnel désactivé par défaut : Noul conditionne le réveil, les scores sous le seuil partent en lot dans le digest quotidien, tout échec retombe en fail-open sur réveil-à-chaque-courrier, décisions journalisées avec scores, stdlib pur derrière l'extra `[jev]`). 13 outils MCP.
- **v0.5.0** — durcissement du cycle de vie issu des incidents du 2026-09-13 (tâche `t-6`) : **suppression des doublons** (`semantic_hash` côté livraison, les répétitions non terminales à même hash dans une fenêtre de 24h renvoient `{"deduped": true, "existing_id"}` sans effet de bord ; `dedupe: false` exempt ; hachage du contenu brut des fences de code, périmètre inbox uniquement, index hash→inbox) ; **compensation des tâches à moitié faites** (API intent/outcome en deux phases de `record_handled` comme unique écrivain de `handled_log` + table à quatre lignes `resume_plan` : process / replay / finalize / skip) ; **récupération des acked périmés** livrée plus tôt sous `reap_stale_acked` / `python -m agent_mailbox.reap` désormais câblée dans la boucle de réveil (moissonner d'abord, compter ensuite, fail-open) avec le couplage de la loi de fer 1 imposé — `reap_ttl` (3600s) doit rester strictement sous `dedup_ttl` (24h), les violations échouent bruyamment ; **disjoncteur de réveil** — N rounds de drain consécutifs sans progrès latchent un fichier breaker et cessent de lancer des tours (le backoff étire l'intervalle, le disjoncteur stoppe l'hémorragie).
- **v0.5.x (ouvert)** — toujours suivi depuis les revues t-6 : `status filtering` (demander des vues « pending ou acked »), `wake routing` (filtre `to` sur l'abonnement gateway ; vit hors de ce dépôt). Livré en v0.6.2 : ~~`identity binding`~~, ~~`unread_count` dans les payloads webhook~~, ~~sémantique de claim de `mailbox_wait`~~.
- **v0.4.0** — lot de fonctionnalités : style de signature webhook configurable (`AGENT_MAIL_SIGNATURE_STYLE` : github par défaut / generic / slack) ; protection anti auto-écho (les notifications où expéditeur == destinataire sont écartées par défaut, auditées `echo_suppressed` dans `sent.log` ; `notify_self_echo` / `AGENT_MAIL_NOTIFY_SELF_ECHO` rétablit la livraison avec un préfixe `[echo] ` sur le sujet de la notification tandis que la lettre garde le sien) ; nouvelle commande de maintenance `cleanup --dry-run` (scanne les résidus de test, liste sans supprimer, `--yes` supprime après confirmation).
- **v0.3.1** — lot de correctifs : les objets de réponse n'empilent plus `Re: Re:` (première réponse, ré-réponses et préfixes à casse mixte se normalisent en un seul `Re:`) ; les tokens du tableau web passent en comparaison à temps constant (`hmac.compare_digest`) et persistent entre redémarrages (`~/.agent-mail/web_token`, mode 0600, la variable d'env `AGENT_MAIL_WEB_TOKEN` gagne toujours) ; `sent.log` rotationne automatiquement d'une génération au-delà de 10 Mo (vers `sent.log.1`).
- **v0.3.0** — tableau de tâches + kanban web : `task_create` / `task_move` / `task_list` avec une machine à états stricte todo→doing→review→done ; créer ou déplacer une carte envoie un message automatique au désigné, donc le mouvement du tableau réveille des agents sans polling. `--web 8643` sert une UI kanban sans dépendance protégée par token où le glisser-déposer humain passe par le même chemin de réveil. Messages + tâches + réveil + tableau, toujours zéro dépendance.
- **Next** — channels (canaux thématiques avec annuaires d'abonnés, visibles du owner) ; fédération : transport streamable HTTP pour les agents sur d'autres machines (Tailscale/LAN friendly) ; reçus signés (ed25519) pour une livraison à l'épreuve de la falsification.
- **v1.0.0** — pont inter-organisations : les threads locaux rejoignent des agents sur d'autres machines et organisations via l'infrastructure e-mail standard, avec le même cycle de vie du mailbox.

## Licence

MIT

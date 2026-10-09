# 保持唤醒：让你的 AI 员工「一有新信就被叫醒」

> 面向：正在试用 v0.8 工作台的你（真人照着做即可）。
> 本文所有命令都在本机实测过（macOS · `agent-mailbox 0.8.1a1` · HEAD `736d764`）。
> 文中不出现那个系统守护进程的专名——你只需要理解「**保持唤醒**」这一个开关。

---

## 0. 你唯一要做的决定

**「我把工作台窗口关掉以后，还要不要被叫醒？」**

| 你的选择 | 你要做什么 |
|---|---|
| **要**（睡觉也想知道有没有新活） | 装一次「保持唤醒」（§3 步 6，一次性命令，随时可卸载） |
| **不要**（我开着窗口时自己看） | 什么都不用装；要收信时手动跑一次 `wake --once`（§2 · §3 步 2） |

> 系统自带的调度能力**不需要你安装**——它本来就在 macOS 里。你要决定的只是「用不用它」，而不是「装不装它」。

---

## 1. 先看清两种形态（这一节是本版实测结论，不是宣传）

| 形态 | 现状（本版实测） | 你要做什么 |
|---|---|---|
| **A. 工作台窗口开着** | ✅ **本版会自动叫醒**（`94e253b` 起）：应用启动即挂一个 **5 秒**兑付线程 `wake_redeem_loop` ✓ 任何来源的新信都会被它发现并叫醒 ✓ 实测 `wakes=2` 最近 16:25:25 ✓。**边界**：应用关着且没装后台 ⇒ 没人能叫 ✓ 退化为"下次开会话看到" | 打开应用即可（默认 ✓ 零配置 ✓）；想连窗口关着也叫 ⇒ 装"保持唤醒"（§3 步 6） |
| **B. 工作台窗口关着** | 可选「保持唤醒」：每 15 秒跑一次判定；有新信 → 叫醒 + 落一个标记文件 | 装一次（§3 步 6），一句话可卸 |

**判定的铁律**（代码里写死的，你不用记）：同一员工 **15 分钟内不重复叫**、**每小时最多 4 次**、**首次上线只把老信记为已见、绝不叫人**（防历史刷屏）。

---

## 2. 三条命令先认脸

```sh
agent-mailbox wake --status     # 看：开关状态、跟了几个员工、每人已见几封/叫过几次
agent-mailbox wake --once       # 真的跑一轮判定（有新信就叫人，没有就跳过）
agent-mailbox wake --enable     # 打开判定（默认就是开的）
agent-mailbox wake --disable    # 关掉判定：信不会丢，只是不再叫人
```

**状态文件在哪**（出问题第一个要看的地方）：

```text
<你的工作台数据目录>/wake/wake-state.json     # 水位线 + 叫醒记录（权限 600）
<你的工作台数据目录>/wake/wake-outbox/        # 每叫醒一次就落一个标记 JSON（可审计）
```

默认数据目录是 `~/.agent-mailbox-v08`。所以：

```sh
cat ~/.agent-mailbox-v08/wake/wake-state.json
ls  ~/.agent-mailbox-v08/wake/wake-outbox/
```

---

## 3. HS 试用六步（照着勾）

> 准备：终端里 `agent-mailbox` 能跑（`agent-mailbox --version` 有输出）。
> 想把试用写在临时目录、不碰正式状态：每条命令后面加 `--state-dir /tmp/wake-trial`
> （**本文实测就是这么做的**，正式状态一个字节没动）。

### 步 1 · 看当前状态

- [ ] 完成本步（核对下面的「预期输出」）

```sh
agent-mailbox wake --status
```

**预期输出**（人数取决于你登记了几位员工）：

```text
唤醒：开启 ✓ · 已跟踪员工 17 ✓
  · employee_...：已见 2 封 ✓ 叫过 1 次 ✓
  · employee_...：已见 0 封 ✓ 叫过 0 次 ✓
```

- 第一次跑，「已跟踪员工」可能是 **0**——正常，冷启动还没做过。
- 只看不改：这条命令不写任何文件。

### 步 2 · 跑一轮（第一次 = 冷启动）

- [ ] 完成本步（核对下面的「预期输出」）

```sh
agent-mailbox wake --once
```

**预期输出**：

```text
冷启动归零 ✓ 已把 17 位员工现有信记为已见 ✓ **本轮不叫任何人** ✗（防刷历史 ✓）
```

✅ **这轮不叫人是对的**：它先把「现在信箱里已经有的信」全部记为「已见」，避免一上线就把几百封历史信当成新信狂叫。换言之：**第一次跑不出声，是设计，不是失败。**

### 步 3 · 让同伴给你发一封信

- [ ] 完成本步（核对下面的「预期输出」）

两种都行：

- 在工作台里：**项目消息 → 写一条消息 → 收件人选「你（员工）」→ 发送**；
- 或者让另一个 AI 员工用它的项目 MCP 工具发给你。

⚠️ 收件人**必须是该项目的成员**，否则会拒绝（错误提示会写明：`消息中的员工必须属于当前项目`）。这是防止把信发给项目外的人。

### 步 4 · 再跑一轮（这次该叫人了）

- [ ] 完成本步（核对下面的「预期输出」）

```sh
agent-mailbox wake --once
```

**预期输出**：

```text
本轮叫醒 1 位 ✓（跳过 16 ✓）：['employee_617c4cf2c0c540d8a4b7053cf9bc2c71']
```

并检查标记文件确实落了：

```sh
ls ~/.agent-mailbox-v08/wake/wake-outbox/
```

**预期**：新出现 `wake-employee_...-20261008T010834.json`，内容形如：

```json
{"employee_id": "employee_...", "reason": "new_mail", "unread": 1,
 "fresh": ["message_..."], "at": "20261008T010834"}
```

❌ **如果这里还是「叫醒 0 位」**：

1. 立刻再跑一次 `agent-mailbox wake --once`——**同一封信不会重复叫**（水位线已推进）；
2. 确认收件人写对了（`--status` 里那个 `已见` 数有没有 +1）；
3. 是不是刚叫过同一个人？**15 分钟冷却**内不会再叫（这是防风暴，不是故障）。

### 步 5 · 接你自己的「宿主 hook」（可选，推荐）

- [ ] 完成本步（核对下面的「预期输出」）

「叫醒」这个动作最终要由**你的 AI 宿主**来完成（起一个会话/推一条提示）。约定一个脚本位置即可：

```sh
# 路径固定：<数据目录>/wake/wake-hook.sh   ← 注意在 wake/ 里面
cat > ~/.agent-mailbox-v08/wake/wake-hook.sh <<'SH'
#!/bin/sh
# 参数 1 = 被叫醒的员工 id
# 这里换成你宿主的「起会话 / 推提示」命令
echo "[$(date '+%F %T')] 叫醒 $1" >> ~/.agent-mailbox-v08/wake/hook-fired.log
SH
chmod +x ~/.agent-mailbox-v08/wake/wake-hook.sh
```

现在再走一遍步 3 + 步 4，**预期**：`wake-hook.sh` 被执行，`hook-fired.log` 多一行；`wake-outbox/` 也多一个标记。

- ✅ 没有配 hook **也不算丢信**：标记文件照样落，下次开会话用 `brief` 也能看到。
- ⚠️ hook 必须是**可执行**文件（`chmod +x`）；否则会被静默跳过（只落标记，不执行）。

### 步 6 · 关掉窗口也收（可选，一次性）

- [ ] 完成本步（核对下面的「预期输出」）

```sh
agent-mailbox wake --install            # 写单元文件；**不会自动启用**
# 把上一条命令打印出来的那行「装载命令」原样粘进终端执行（它会打印给你）
agent-mailbox wake --install --plist-dir /tmp/wake-trial-plist   # 只想试、不想装到系统：用临时目录
```

**预期输出**：

```text
已写单元：~/Library/LaunchAgents/com.polaris-smart.agent-mailbox-wake.plist ✓
未自动加载 ✗ —— 需要你显式执行：launchctl load -w <上面那个路径>
```

然后**手动**执行它打印的那行（这是人侧显式动作，工具故意不代劳）。

**撤销 / 删干净**：

```sh
agent-mailbox wake --uninstall
```

**预期输出**：

```text
已删单元 ✓
若已加载 ⇒ 记得：launchctl unload -w <单元路径>
```

⚠️ **卸载要注意顺序**（实测坑，见附录 A-③）：如果已经装载过，**先**执行它提示的 `launchctl unload -w <路径>`，**再**删文件；反过来先 `--uninstall` 删了文件，再按路径卸载会报
`Unload failed: 5: Input/output error`，那个定时任务会继续跑到你注销为止。稳妥顺序：

```sh
launchctl unload -w ~/Library/LaunchAgents/com.polaris-smart.agent-mailbox-wake.plist   # 先卸
agent-mailbox wake --uninstall                                                          # 再删
```

---

## 4. 失败自救卡

| 症状 | 先看哪 | 怎么办 |
|---|---|---|
| 「怎么一直不叫我」 | `wake --status` 第 1 行是否 `开启 ✓` | 若是 `关闭 ✗`：`agent-mailbox wake --enable` |
| 「刚发信，跑 `--once` 说叫醒 0 位」 | `--status` 里该员工的「已见 N 封」 | 该信已被记为已见 ⇒ 要么它早就被叫过，要么还在**15 分钟冷却**内；等一会儿或换个人试 |
| 「第一次跑为什么一个人都不叫」 | 输出里有「冷启动归零」 | **正常**（§3 步 2），再发一封新信即可 |
| 「hook 没执行」 | `ls -l <数据目录>/wake/wake-hook.sh` | 必须 `-rwx`；路径必须在 `wake/` 里（不是数据目录根下） |
| 「装了也不叫」 | `~/Library/LaunchAgents/com.polaris-smart.agent-mailbox-wake.plist` 里的日志路径 | 看 `<数据目录>/wake-kernel.log`；里面若有 `command not found`，见附录 A-② |
| 「不想叫了，怎么彻底停」 | — | `agent-mailbox wake --disable`（只停判定，数据保留）；要删文件再 `--uninstall`（顺序见 §3 步 6） |
| 「信会不会丢」 | — | **不会**。判定与叫醒全程只读信；只有「叫成功了」才推进水位线（叫失败下次还会叫） |

---

## 5. 名词表（少一个词，少一次误解）

| 你看到的词 | 意思 |
|---|---|
| **保持唤醒** | 关窗口后仍每 15 秒检查一次「有没有新信」的可选后台任务 |
| `wake --once` | 手动跑一轮判定（就一次，跑完退出） |
| **水位线** | 「这封信我见过了」的名单，存在 `wake-state.json`；防止同一封重复叫 |
| **冷启动** | 第一次运行，把现有信全部记为已见，**当轮不叫人** |
| **标记（marker）** | 每叫醒一次落在 `wake-outbox/` 的小 JSON，用来证明「确实叫过」 |
| **hook** | 你的宿主收到叫醒后要执行的动作脚本（`<数据目录>/wake/wake-hook.sh`） |

---

## 附录 A · 体验问题清单（我按上面六步真走一遍时卡住的地方）

> 环境：macOS · `agent-mailbox 0.8.1a1` · HEAD `736d764`。
> 纪律：临时库副本 + `--state-dir /tmp/...` + `--plist-dir /tmp/...`；**没有在本机装载任何后台单元**；正式数据目录只读。

**① 「窗口开着就零配置」目前不成立（最影响预期的一条）**
任务背景说「应用在跑 ⇒ 自动轮询 ⇒ 零配置」，但代码里**没有任何调用点**：

```sh
$ grep -rn "run_once\|hook_deliver\|active_employee_ids" --include=*.py src/agent_mailbox/ | grep -v workbench_wake.py
（无命中）
```

⇒ 应用进程不会自己跑判定；「被叫醒」今天只有两条路：人侧手动 `--once`，或装保持唤醒单元。**建议**：要么在应用里内置一个低频轮询（复用同一个 `run_once`），要么把文档/界面的说法改成现状——否则用户会以为「开着窗口就会响」。

**② 装出来的后台单元很可能起不来：命令写的是裸名 `agent-mailbox`**
单元文件里是：

```xml
<key>ProgramArguments</key>
<array><string>agent-mailbox</string><string>wake</string><string>--once</string></array>
```

而后台任务的 `PATH` 只有系统目录：

```sh
$ env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin sh -c 'command -v agent-mailbox || echo NOT FOUND'
NOT FOUND in minimal PATH（后台任务拿到的默认 PATH）
```

本机 `agent-mailbox` 实际在 `~/.local/bin/`。⇒ 单元会以 127 退出，日志里只有一行 `command not found`，用户看到的却是「我明明装了」。**建议**：写单元时用绝对路径（`shutil.which("agent-mailbox")` 或 `sys.argv[0]`），找不到时明确报错。

**③ 卸载顺序是反的：先删文件，再让你按路径去卸 ⇒ 那个任务卸不掉**
`--uninstall` 的输出顺序是「已删单元 ✓」→「若已加载 ⇒ 记得 `launchctl unload -w <那个已被删掉的路径>`」。文件删掉后再按路径卸载，实测直接失败：

```text
$ launchctl unload -w /tmp/panel-ia/trial-plist/gone.plist
Unload failed: 5: Input/output error
Try running `launchctl bootout` as root for richer errors.
```

⇒ 结果是「文件没了、任务还在跑」，直接顶到「一切可逆 / 卸载零残留」这条铁律上。**建议**：`--uninstall` 先提示/执行按**标签**卸载（`launchctl bootout gui/$UID/com.polaris-smart.agent-mailbox-wake`），再删文件；或把顺序改成「先卸后删」并在输出里说清。

**④ `hook_deliver` 的说明与代码路径不一致（文档级坑）**
`workbench_wake.py:192-197` 的 docstring 说标记落 `<home>/wake-outbox/`、hook 在 `<home>/wake-hook.sh`；但代码里 base 传的是 `state_dir(state_home)`（= `<home>/wake/`，第 344 行），实际是 `<home>/wake/wake-outbox/` 与 `<home>/wake/wake-hook.sh`。**照 docstring 放 hook 会静默不执行**（只落标记）。**建议**：改 docstring，或在 `--status` 输出里直接打印「hook 应放在：<路径>」。

**⑤ `--once` 与 `--state-dir` 的位置在文档里没有任何提示**
`--once` 的用法只在 `wake`（不带参数）的最后一行以一行「用法」出现，且不带参数时会先刷出全部 17 位员工的状态，用法提示被挤到最末。**建议**：给 `agent-mailbox wake --help` 一个真正的帮助（现在没有 help 分支）。

**⑥ 第一次跑 `--status` 只显示「已跟踪员工 0」**
冷启动前没有「还没做过冷启动」的提示，用户容易以为坏了。**建议**：`--status` 在 `cold_done` 缺失时补一句「尚未冷启动；下次 `--once` 只归零不叫人」。

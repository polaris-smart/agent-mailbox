---
doc: v0.8.1a1 · 干净构建证据
status: active（第二形态见文末「⑥ 零引擎形态」✓）
SUPERSEDED_BY: 待定（零引擎默认形态的构建证据 ✓ 将由新的证据档替代）
updated: 2026-10-07
note: >
  本档的 **macOS app 部分**描述的是**带引擎的 780M 包** ✗ —— 老板于 2026-10-07 拍板
  「0.8.1 零引擎默认、引擎由用户自备」✓ ⇒ 该形态**已被否决** ✗（法律红线 + 用户选择权 ✓）。
  保留本档作为**过程证据** ✓（不删不藏 ✓）；其中 **wheel / 发布树 / CLI 干净环境**三节**仍然有效** ✓。
---

> **⚠️ 已取代（2026-10-07）**：本档「④ macOS 无签名 app」一节描述的 **780M 带引擎包**已被老板否决 ✗
> —— 0.8.1 发行形态定为 **零引擎默认**（mailbox 纯净发行 ~86M ✓ 引擎用户自备或按需下载 ✓）。
> 其余三节（wheel ✓ 发布树 ✓ CLI 干净环境 ✓）**不受影响** ✓。

# v0.8.1a1 · 干净构建证据（⑤ 第 2 步）

> 规矩：**每条都带命令原文与输出** ✓ 未验证的不写 ✗ · 本文件进仓 ✓ 但**不进发布树** ✗（`docs/evidence` 在剥离清单里 ✓）

## ① CLI / pip 渠道（wheel）

```
命令: uv build --wheel --out-dir /tmp/wheel          # 用 uv.lock ✓ 不手写依赖 ✗
输出: Successfully built /tmp/wheel/agent_mailbox-0.8.1a1-py3-none-any.whl
产物: agent_mailbox-0.8.1a1-py3-none-any.whl · 408,858 bytes
SHA256: 55cf366aaa125f3b28b7ceea83fbeb7b41a1ff63a8084dfff714985e0664a0c5
```

**内容核验（命令+输出 ✓）**

```
文件数 79 · 顶层 = ['agent_mailbox', 'agent_mailbox-0.8.1a1.dist-info']
内部资料命中: 无 ✓（无 docs/reviews · docs/evidence · 实施任务书 ✓）
UI 资源: workbench.js ✓ workbench.css ✓ index.html ✓
METADATA: Name: agent-mailbox · Version: 0.8.1a1 ✓
```

**为什么这条渠道重要** ✓：`py3-none-any` ⇒ **跨平台**（含 Windows ✓）· **免签名**（无需 Gatekeeper/SmartScreen ✗）·
`pipx install agent-mailbox` 即可用 ✓ —— 在 Windows 原生包与签名就绪前，这是**可发布的那条路** ✓

## ② 干净发布树（内外分离）

```
命令: bash scripts/release-tree.sh /tmp/reltree
       python scripts/check-hygiene.py --all --root /tmp/reltree
仓库:  344 文件 · FAIL 0 · WARN 17
发布树: 274 文件 · FAIL 0 · WARN 5        # 剥离 70 个文件 ✓ 告警少 12 条 ✓
空目录核验: docs/{reviews,archive,evidence,designs,diagrams} 在树中均不存在 ✓
```

## ③ 尚未做（如实 ✗ 不写成已完成）

- **macOS 无签名 app**：`scripts/build-workbench.py` 需 `--runtime-dir/--node-binary` ✓ 未跑（体积与耗时大 ✓ 下一轮）
- **Windows 包**：需 Windows 环境或 CI ✗（PyInstaller 不能交叉编译 ✓）—— 已列为待老板拍板项 ✓
- **干净 OS 实测**：未做 ✗（当前机器不是干净环境 ✓ 已知 ✓）
- **公证/签名**：按老板定调**不做** ✓（发布物 = 无签名 ✓ + 文档写清两步绕过 ✓）

## ④ macOS 无签名 app（本轮达成 ✓）

构建命令（原文 ✓）：

    .build-venv/bin/python scripts/build-workbench.py \
      --runtime-dir src/agent_mailbox/runtime_bridge \
      --node-binary "/Applications/Agent Mailbox.app/Contents/Frameworks/runtime/bin/node" \
      --output-dir /tmp/appbuild --name "Agent Mailbox"

产物与证据：

    app:      /tmp/appbuild/dist/Agent Mailbox.app   （780M）
    分发件:    AgentMailbox-0.8.1a1-macos-arm64-unsigned.zip （314,085,890 bytes）
    SHA256:   ecd1d0a98dc7a3a2e4f9ce28401eb4c49220bc9cab5e1ae2bc56cf059df677bc
    构建清单:  distribution_signed=false · notarized=false · automatic_updates=false · macos_ui_element=true
    二进制实测: node v22.23.1 ✓ 可执行 · codex "codex-cli 0.158.0" ✓ 可执行

签名/公证：按老板定调**不做** ✓（发布物 = 无签名 ✓ 文档写清 Gatekeeper 两步绕过 ✓）

本轮为让构建通过修掉的 **3 处真缺陷**（全部实测驱动 ✓ 不是猜 ✗）：

1. **PyInstaller 会漏大二进制** ✗：实测丢掉 `codex`（239MB）与 `codex-code-mode-host`（65MB）两个，
   而**同目录的小文件却进了** ✓ ⇒ 新增打包后**补拷** ✓ + 自检改**真校验**（真文件 / 非软链 / 非空 / 魔数）
2. **补拷与自检判定不一致** ✗（我引入的）：补拷按名字判断 ⇒ 把 `node_modules/.bin/codex`（**软链**）
   当成"已存在" ⇒ 跳过补拷；而自检排除软链 ⇒ 报"缺真文件" ✗ ⇒ 统一为"真文件 + 非软链" ✓
3. **两处校验器把布局写死** ✗：旧代码写死 `Contents/Resources/runtime/...`，而现产物把 runtime
   放在 **`Contents/Frameworks/...`**（另存在 onedir 形态）⇒ 改为**按名字搜索整棵产物** ✓ 两种布局都对

另：`.gitignore` 缺 `node_modules` ✗（实测一次 `git add -N .` 暂存了 **59 万行依赖**）⇒ 已补 + 回归测试 ✓

仍未做（如实 ✓ 不写成已完成 ✗）：**Windows 原生包**（需 Windows 环境或 CI ✓ PyInstaller 不能交叉编译 ✓）·
**干净 OS 实测**（当前机器不是干净环境 ✓ 已知 ✓）

## ⑤ CLI/pip 渠道 · 干净环境实跑（本轮达成 ✓）

「干净环境」的范围（如实 ✓ 不夸大 ✗）：**空 HOME + 独立 venv + 只装我们构建的 wheel**（不引用源码树 ✓）。
仍非"干净 OS"✗（当前机器是开发机 ✓ 已装过 node/codex 等 ✓）—— 真正的干净 OS 实测需另一台机器或 CI ✓。

命令与输出（原文 ✓）：

    # ① 干净前缀建 venv
    uv venv /tmp/cleanuser/venv --python 3.11
    # ② 只装 wheel（不依赖源码树）
    uv pip install --python /tmp/cleanuser/venv/bin/python \
        /tmp/wheel/agent_mailbox-0.8.1a1-py3-none-any.whl
    # ③ 空 HOME 运行
    env -i HOME=/tmp/cleanuser PATH=... agent-mailbox selfcheck
    ⇒ 15/15 全过（含：产出物引用回读 ✓ · 团队墙/账本/审计链 ✓ · 检索中文双路径 ✓ ·
       通讯录白名单 ✓ · 单向桥接 ✓ · 观察窗判据「风暴 0 · 真验收 0 · 审计 3/3」✓）

入口可用性：

    agent-mailbox --help   ⇒ usage: agent-mailbox [-h] [--home HOME] [--port PORT] [--no-browser]

**结论**：`py3-none-any` 的 wheel 在**干净 HOME** 下**装得上、跑得起、自检 15/15 全过** ✓
⇒ 在 Windows 原生包与签名就绪前，这就是**可对外的那条渠道** ✓

未做（如实 ✗）：macOS app 的**首启 GUI 实测**（需真机/干净 OS ✓）· Windows 原生包（需环境或 CI ✓）


## ⑥ 零引擎形态（老板 2026-10-07 拍板 ✓ 本条为**当前有效形态** ✓）

老板原话：「**我们给用户的里面，就是干净的工具，不包括任何 agent cli**」✗

find … -name codex

**两次修正的教训（都留了断言 ✓）**：
1. 第一片只挡了  ✗ ⇒ 引擎仍从 **** 整棵进包 ✗（实测 777M / 9 个引擎二进制 ✓）
2. 第二片挡  后 ⇒ **152M / 0 个引擎** ✓（余下 ~152M ≈ Python 运行时 + node ✓ node 是通用运行时 ✗ 不是 agent ✓）

**未做 / 待办（如实 ✗）**：首启「无引擎 ⇒ 常态引导」文案 ✓ · 老板定名后改包名/bundle id 并重建 ✓ ·
  Windows 包（需环境或 CI ✓）· 干净 OS 实测（本机为开发机 ✓）

## ⑥ 零引擎形态（老板 2026-10-07 拍板 ✓ **当前有效形态** ✓）

老板原话：「**我们给用户的里面，就是干净的工具，不包括任何 agent cli**」✗

```
命令: .build-venv/bin/python scripts/build-workbench.py \
        --runtime-dir src/agent_mailbox/runtime_bridge \
        --node-binary "/Applications/Agent Mailbox.app/Contents/Frameworks/runtime/bin/node" \
        --output-dir /tmp/appbuild-clean2 --name "AgentMailboxClean"   # 临时名 ✓ 等你定名后替换 ✓
产物: AgentMailboxClean.app = **152M**（旧形态 780M ⇒ 省 628M ✓）
引擎二进制: **0 个** ✓（`find … -name codex` 无输出 ✓）
分发件: AgentMailboxClean-0.8.1a1-macos-arm64-unsigned.zip · SHA256: 844ded94b1a7a44cfa213c08a2ee89acdf3206a38fe551cf1c40342be35e9f37
清单: distribution_signed=false · notarized=false · macos_ui_element=true ✓
```

**两次修正的教训（都留了断言 ✓）**：
1. 第一片只挡了 `binaries=` ✗ ⇒ 引擎仍从 **`datas=`** 整棵进包 ✗（实测 777M / 9 个引擎二进制 ✓）
2. 第二片挡 `datas=` 后 ⇒ **152M / 0 个引擎** ✓（余下 ≈ Python 运行时 + node ✓ node 是通用运行时✗ 非 agent ✓）

**未做 / 待办（如实 ✗）**：首启「无引擎 ⇒ 常态引导」文案 ✓ · 老板定名后改包名/bundle id 并重建 ✓ ·
  Windows 包（需环境或 CI ✓）· 干净 OS 实测（本机为开发机 ✓）

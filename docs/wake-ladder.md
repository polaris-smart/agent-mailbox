# 唤醒阶梯（T31 规格 · 仓内）

> 背景：2026-10-04 的 353 封风暴里，**v0.7 的"逐信唤醒"是四条风暴源之一**
> （`WatchPaths` 挂在收件箱 ⇒ 每来一封起一个进程）。所以唤醒这件事只有一条铁律：
> **绝不做 per-letter 进程**。下面是替代表，按"便宜 → 贵"排序。

## 阶梯

| 档 | 机制 | 成本 | 何时用 | 状态 |
|---|---|---|---|---|
| **0** | **不唤醒**：员工**主动查**（`project_messages` / `project_tasks`） | 0 | 默认。v0.8 的 `notification_mode=manual_check` 就是它 | ✅ 已有 |
| **1** | **钩子轻通道**：宿主在会话起始调一次 `agent-mailbox brief`，把**有预算的简报**贴进上下文 | 一次进程、几十毫秒；**无常驻** | 装了 hook 的宿主（Claude Code / Codex / Hermes…） | ✅ 本轮实现 |
| **2** | **单内核**（可选）：一个常驻进程轮询并"叫醒 idle agent" | 1 个进程、持续开销 | 只在"必须叫醒睡着的 agent"时启用 | ⏸ 可选；启用必须满足下方护栏 |
| ❌ | **逐信唤醒**（v0.7 形态） | 每信一进程 | **永不** | 🚫 已退役 |

## 档 2 若启用，必须满足（护栏 = 反风暴七闸的子集）

- **S1** 绝不裸 `KeepAlive`（要么 `SuccessfulExit:false`，要么带重启熔断 = `storm_breaker.py`）；
- **S7** 常驻 ≤1 个进程，且有状态文件 + 一条命令可停；
- **S4** 通知折叠 + 每小时上限；**S5** 日志只在状态变化时写；
- **S3** 回声冻结：同一往返 ≥3 轮且状态无变化 ⇒ 冻结（`echo_guard.py`）；
- 触发前先算**升级预算**（`escalation_budget`），超了就抑制并留痕（`workbench_policy.py`）。

## 钩子怎么装（示例：宿主支持 SessionStart 钩子时）

```sh
# 会话起始把简报注入上下文（输出已被预算约束，默认 1200 字节）
agent-mailbox brief --employee <employee_id> --project <project_id> --budget 1200
```

要点：
- **只读**：简报不写任何东西（有测试），因此钩子失败也只是"少一段提示"，不会坏数据；
- **有预算**：`render()` 先给截断提示留位置再截内容（否则"截断后反而超预算"）；
- **去噪**：只给计数与前几条标题，不给消息流水；且会把
  **被折叠/被抑制**的信号顶上来（**安静不等于无事**）。

## 可选宿主叫醒 hook

`wake --once` 只在宿主明确接入后交付叫醒请求。macOS/Linux 继续读取
`<store home>/wake/wake-hook.sh`，要求文件可执行；Windows 读取同一目录的
`wake-hook.ps1`，使用 Windows 系统目录中的 WindowsPowerShell，以
`-NoProfile -NonInteractive -File` 启动。两种 hook 都收到两个独立字符串参数：
员工 ID 和投递 ID。投递 ID 对应 `wake-outbox/<投递 ID>.json`，不把消息正文拼入命令。
hook 与 outbox 始终钉在 store home；`--state-dir` 不改变宿主入口。

Windows 遵守机器现有执行策略，不传 ExecutionPolicy 覆盖参数、不修改系统策略。
在 Restricted 策略下，PS1 会被拒绝；AllSigned 要求受信任的签名，RemoteSigned
仍可能拒绝带互联网来源标记的未签名脚本。被拒绝或返回非零都不计成功叫醒、不写
`.ok`；缺脚本或解释器则保留待办。[微软执行策略说明](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies?view=powershell-5.1)

PS1 宿主 hook 应先声明参数并设置 `$ErrorActionPreference = 'Stop'`，只有实际宿主接口
确认接收请求后才 `exit 0`。若调用原生宿主程序，检查并传播 `$LASTEXITCODE`；出错时
返回非零。PowerShell 非终止错误可能不影响退出码，不能只打印错误后正常结束。
退出码 0 只表示 hook 报告宿主已接收，不能证明员工已开始处理或人类已接受结果。
[微软 PowerShell 启动与退出码说明](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1)

`wake --install` 仅在 macOS 写 launchd 单元，仍需用户显式加载。Linux/Windows
返回不支持且不创建 plist；`--once` 可由宿主已有调度器调用，此处不安装其它系统服务。

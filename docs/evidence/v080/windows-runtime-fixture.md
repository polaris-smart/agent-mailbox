# Windows ACP fixture 启动预算修复

2026-10-01。真实 Windows 3.10 CI 诊断记录于本机 `/tmp/v080-beta4-windows-diagnostic.log`：首个普通 ACP fixture 已收到 `initialize` 请求，但结果为 `TIMEOUT / Agent startup timed out`，未到完成阶段。先前只打印 `failed != completed`，不能定位原因；首断言现输出错误、事件类型、请求方法及平台，不输出 MCP 环境、任务正文或凭据。

固定依赖 ACPX 0.19.3 在 Windows 使用 PowerShell/CIM 查询进程出生身份，启动包含本机进程安全检查，不能以原 fixture 的 1 秒预算当作完整产品启动能力。正常 fixture 改用产品现有 15 秒启动预算，事件等待有界 20 秒。故意 init_hang/new_hang 仍为 1 秒；普通 prompt hang 仍使用 100 ms 任务期限，并新增实际 session/prompt 请求断言，防止把启动失败误当 prompt 超时通过。

stderr 并发读取、只保存最多 64 KiB 诊断但检查所有字节中的合成凭据。关闭仍要求桥接进程在 8 秒内退出；stderr/stdout reader 在有界等待后必须结束，不再在进程退出后无限 `.stderr.read()`。业务结果、权限、完整输出与不重复执行断言保留。

Mac 固定 Node/ACPX fixture 全回归及最终 Windows CI 结果由主任务收口记录；不能以 Mac 通过宣称 Windows 已通过。没有改依赖源码、跳过正常 Windows fixture 或关闭进程身份校验。

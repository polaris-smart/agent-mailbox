"""Open the independent agent-mailbox workbench."""

from .cli import main

if __name__ == "__main__":
    # 控制台脚本返回码必须与 python -m 一致（否则脚本化调用会把失败当成功）
    raise SystemExit(main())

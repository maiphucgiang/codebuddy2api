"""Run the real gateway against a simulated upstream: ``python -B -m preview``.

The preview keeps throwaway credentials in its own data directory and never reads the
repository's ``.env`` or ``auth/``, so it is safe for UI work, API debugging and demos.
"""

import argparse
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from preview import public, sandbox, seed  # noqa: E402
from preview.upstream import Upstream  # noqa: E402

MARKER = ".preview"


def prepare(directory: Path) -> Path:
    """Seed a new data directory, or reuse one this command created before."""
    if directory.resolve() == (ROOT / "auth").resolve():
        sys.exit("预览服务不能使用仓库的 auth/ 目录")
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / MARKER).exists():
        return directory
    if any(directory.iterdir()):
        sys.exit(f"预览数据目录必须为空或由预览服务创建：{directory}")
    seed.write_credentials(directory)
    seed.write_history(directory / "logs.sqlite3")
    seed.write_media(directory)
    (directory / MARKER).write_text("codebuddy2api preview data\n", encoding="utf-8")
    return directory


def main():
    ap = argparse.ArgumentParser(description="codebuddy2api 预览服务：真实网关 + 模拟上游，不含真实凭证；"
                                             "未列出的参数原样传给网关")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址")
    ap.add_argument("--port", type=int, default=8790, help="监听端口")
    ap.add_argument("--api-key", default="preview", help="WebUI 与 API 使用的密钥")
    ap.add_argument("--data-dir", metavar="PATH", help="保留预览数据的目录；默认每次启动使用新的临时目录")
    ap.add_argument("--public", action="store_true", help="公开部署：禁止导入导出凭证、修改设置和清空日志等操作")
    ap.add_argument("--reset-hours", type=float, default=0, metavar="HOURS",
                    help="运行指定小时后退出，由进程管理器用全新数据重启；0 表示不重置")
    args, gateway_args = ap.parse_known_args()
    # Settings from the developer's shell must not leak into the preview, nor real paths into it.
    for key in [key for key in os.environ if key.startswith("CODEBUDDY")]:
        del os.environ[key]
    temporary = None
    if args.data_dir:
        directory = prepare(Path(args.data_dir))
    else:
        temporary = tempfile.TemporaryDirectory(prefix="codebuddy2api-preview-")
        directory = prepare(Path(temporary.name))
    os.environ["CODEBUDDY_AUTH_DIR"] = str(directory)
    sandbox.install(Upstream())
    os.chdir(directory)  # The gateway reads .env from the working directory.
    # Name the default key, but never echo one passed on the command line.
    key = "preview" if args.api_key == "preview" else "已使用 --api-key 指定的值"
    sys.stderr.write(f"[preview] 数据目录：{directory}\n[preview] 密钥：{key}\n"
                     f"[preview] 前端热更新：CODEBUDDY_WEBUI_PROXY=http://{args.host}:{args.port} vp dev\n")
    sys.argv = ["converter.py", "serve", "--host", args.host, "--port", str(args.port), "--api-key", args.api_key,
                *gateway_args]
    import converter
    if args.public:
        converter.app.add_middleware(public.Guard)
    if args.reset_hours > 0:
        public.schedule_reset(converter.CONFIG, args.reset_hours)
    try:
        return converter.main()
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    sys.exit(main())

"""本地启动入口：python run.py [--port 8002] [--host 127.0.0.1]"""

from __future__ import annotations

import argparse

import uvicorn

from app.core.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 TripTide 后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--no-reload", action="store_true", help="关闭热重载")
    args = parser.parse_args()

    print(f"\n  TripTide  →  http://{args.host}:{args.port}/")
    print(f"  接口文档   →  http://{args.host}:{args.port}/docs")
    print(f"  数据库     →  {'SQLite（DB_URL 未配置）' if settings.is_sqlite else 'MySQL'}")
    print(f"  DeepSeek   →  {'已配置' if settings.has_llm else '未配置（走规则兜底）'}")
    print(f"  高德 Key   →  {'已配置' if settings.has_amap else '未配置（用离线种子坐标）'}\n")

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        reload_dirs=["app"] if not args.no_reload else None,
    )


if __name__ == "__main__":
    main()

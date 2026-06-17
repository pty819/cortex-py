"""CLI 入口:cortex db init / worker / serve / probe-llm。"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cortex", description="cortex CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    db_parser = sub.add_parser("db", help="schema 管理")
    db_sub = db_parser.add_subparsers(dest="db_cmd", required=True)
    db_sub.add_parser("init", help="建 cortex schema(幂等)")
    db_sub.add_parser("reset", help="DROP + 重建 cortex schema")

    sub.add_parser("worker", help="跑队列 worker")
    serve = sub.add_parser("serve", help="跑 FastAPI")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("probe-llm", help="R1 探针:测 Minimax json_schema 支持")
    sub.add_parser("smoke", help="端到端冒烟(入库→抽取→检索→回答)")
    sub.add_parser("mcp", help="跑 MCP server(stdio,本地单 agent)")
    mcp_http = sub.add_parser("mcp-http", help="跑 MCP server(streamable-http,多人共享)")
    mcp_http.add_argument("--host", default="0.0.0.0")
    mcp_http.add_argument("--port", type=int, default=8001)

    args = ap.parse_args(argv)

    if args.cmd == "db":
        from . import db
        if args.db_cmd == "init":
            db.init_schema(drop=False)
            print("✓ schema initialized (cortex)")
        elif args.db_cmd == "reset":
            db.init_schema(drop=True)
            print("✓ schema reset (cortex)")
        return 0

    if args.cmd == "probe-llm":
        from .extraction.probe import run_probe
        return run_probe()

    if args.cmd == "worker":
        from .worker.runner import run_worker
        run_worker()
        return 0

    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("cortex.api.app:app", host=args.host, port=args.port, reload=False)
        return 0

    if args.cmd == "smoke":
        from .smoke import run_smoke
        return run_smoke()

    if args.cmd == "mcp":
        from .mcp_server import main_stdio
        main_stdio()
        return 0

    if args.cmd == "mcp-http":
        from .mcp_server import main_http
        main_http(host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())

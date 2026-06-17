"""MCP streamable-http 验收:多人共享 + 按 X-Cortex-Scope 头隔离。

起 cortex mcp-http 子进程,两个 client 用不同 scope 头连接:
  scopeA: memory_store → memory_search 命中
  scopeB: memory_search 不该看到 A 的(隔离)
依赖:scripts/db_proxy.py 在跑。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import httpx

PORT = 8002
URL = f"http://127.0.0.1:{PORT}/mcp"
SCOPE_A = "org:mcp/user:alice"
SCOPE_B = "org:mcp/user:bob"
PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1; print(f"  ✓ {name} {detail}")
    else:
        FAIL += 1; print(f"  ✗ {name} {detail}")


async def call_tool(session, tool, args):
    res = await session.call_tool(tool, args)
    txt = res.content[0].text if getattr(res, "content", None) else "{}"
    try:
        return json.loads(txt)
    except Exception:
        return txt


async def with_client(scope, fn):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"X-Cortex-Scope": scope}
    async with streamablehttp_client(URL, headers=headers) as streams:
        # SDK 版本差异:可能 2 元或 3 元
        r, w = streams[0], streams[1]
        async with ClientSession(r, w) as session:
            await session.initialize()
            return await fn(session)


async def main_async():
    # 1. 重置 schema
    subprocess.run([sys.executable, "-m", "cortex.cli", "db", "init"], check=False,
                   env=dict(os.environ), capture_output=True)

    # 2. 起 mcp-http 子进程
    proc = subprocess.Popen([sys.executable, "-m", "cortex.cli", "mcp-http", "--port", str(PORT)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # 等就绪
        ready = False
        for _ in range(40):
            try:
                httpx.get(f"http://127.0.0.1:{PORT}/mcp", timeout=1.0)
                ready = True; break
            except Exception:
                await asyncio.sleep(0.5)
        check("mcp-http server 起来", ready, "(轮询 /mcp)")

        print("=== tools/list(streamable-http)===")
        ntools = await with_client(SCOPE_A, lambda s: s.list_tools())
        check(f"HTTP tools/list 返回 {len(ntools.tools)}(>=20)", len(ntools.tools) >= 20)

        print("=== scopeA: memory_store + search(应命中)===")
        async def store_and_search_a(s):
            st = await call_tool(s, "memory_store", {"text": "Alice owns the Q3 Renewal project."})
            await asyncio.sleep(0.3)
            se = await call_tool(s, "memory_search", {"query": "who owns Q3 Renewal"})
            return st, se
        st, se = await with_client(SCOPE_A, store_and_search_a)
        check("scopeA store 抽出 facts>=1", st.get("facts_extracted", 0) >= 1, str(st)[:120])
        check("scopeA store 用了 scopeA", st.get("scope") == SCOPE_A, str(st.get("scope")))
        nf_a = len(se.get("facts", []))
        check(f"scopeA search 命中 facts={nf_a}(>=1)", nf_a >= 1, str(se)[:120])

        print("=== scopeB: search 同一查询(隔离,应为 0)===")
        se_b = await with_client(SCOPE_B, lambda s: call_tool(s, "memory_search", {"query": "who owns Q3 Renewal"}))
        nf_b = len(se_b.get("facts", []))
        check(f"scopeB 看不到 A 的记忆(facts={nf_b}, 隔离)", nf_b == 0, str(se_b)[:120])

        print("=== 显式 scope arg 覆盖头 ===")
        se_arg = await with_client(SCOPE_B, lambda s: call_tool(s, "memory_search", {"query": "who owns Q3 Renewal", "scope": SCOPE_A}))
        check("显式 scope=A 覆盖 B 头 → 命中", len(se_arg.get("facts", [])) >= 1)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print(f"\n=== MCP HTTP 验收:PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))

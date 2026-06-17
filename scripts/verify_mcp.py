"""MCP 验收:子进程跑 cortex.mcp_server(stdio),JSON-RPC 驱动 initialize→tools/list→tools/call。

模拟 agent 真实注册路径。需 DB 代理(scripts/db_proxy.py)在跑。
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time

SCOPE = "org:mcp/test/user:agent"
PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1; print(f"  ✓ {name} {detail}")
    else:
        FAIL += 1; print(f"  ✗ {name} {detail}")


def main():
    env = dict(os.environ, CORTEX_SCOPE=SCOPE)
    # 1. 重置 schema(确保 memory_search 干净)
    subprocess.run([sys.executable, "-m", "cortex.cli", "db", "init"], check=False,
                   env=env, capture_output=True)

    proc = subprocess.Popen([sys.executable, "-m", "cortex.mcp_server"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, env=env)

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()

    def read_resp(expected_id, timeout=30.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            r, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not r:
                if proc.poll() is not None:
                    err = proc.stderr.read()
                    return {"error": f"server exited: {err[:300]}"}
                continue
            line = proc.stdout.readline()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == expected_id:
                return msg
        return {"error": "timeout"}

    def call(tool, args, _id):
        send({"jsonrpc": "2.0", "id": _id, "method": "tools/call",
              "params": {"name": tool, "arguments": args}})
        r = read_resp(_id)
        if "error" in r and "result" not in r:
            return None, str(r)
        res = r.get("result", {})
        if res.get("isError"):
            return None, res.get("content", [{}])[0].get("text", "")
        txt = res.get("content", [{}])[0].get("text", "{}")
        try:
            return json.loads(txt), None
        except Exception:
            return txt, None

    print("=== initialize ===")
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "verify", "version": "1.0"}}})
    init = read_resp(1)
    check("initialize 握手", "result" in init, str(init.get("error", ""))[:120])
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    print("=== tools/list ===")
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tl = read_resp(2)
    ntools = len(tl.get("result", {}).get("tools", [])) if "result" in tl else 0
    check(f"tools/list 返回 {ntools} 工具(>=20)", ntools >= 20, "")

    print("=== tools/call health_check ===")
    h, err = call("health_check", {}, 3)
    check("health_check ok", h and h.get("ok"), str(err)[:120] or str(h)[:120])

    print("=== tools/call memory_store(同步抽取)===")
    s, err = call("memory_store", {"text": "Priya Rao owns the Q3 Renewal project at Acme."}, 4)
    check("memory_store 返回 event_id", s and s.get("event_id"), str(err)[:120] or str(s)[:150])
    check("memory_store 同步抽取 facts>=1", s and s.get("facts_extracted", 0) >= 1, str(s)[:150])

    print("=== tools/call memory_search(检索刚存的)===")
    r5, err = call("memory_search", {"query": "who owns the Q3 Renewal"}, 5)
    nfacts = len(r5.get("facts", [])) if r5 else 0
    check(f"memory_search 命中 facts={nfacts}(>=1)", nfacts >= 1, str(err)[:120] or str(r5)[:150])
    check("memory_search 有 context_block", r5 and bool(r5.get("context_block")), "")

    print("=== tools/call answer ===")
    a, err = call("answer", {"query": "who owns the Q3 Renewal"}, 6)
    check("answer 返回回答文本", a and a.get("answer"), str(err)[:120] or str(a)[:150])

    print("=== tools/call entity_list ===")
    el, err = call("entity_list", {}, 7)
    check("entity_list 返回 items", el and "items" in el, str(err)[:120])

    proc.stdin.close()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()

    print(f"\n=== MCP 验收:PASS={PASS} FAIL={FAIL} ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

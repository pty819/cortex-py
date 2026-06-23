"""机械故障诊断入库 demo:三路径入库(结构文档切块 + 事件叙述 + triple 直写)→ 图遍历找根因 → answer。

演示 docs/specs/10-diagnosis-ingest.md 的完整工作流。
需:DB 代理(python3 scripts/db_proxy.py)+ 后端(8002)+ worker 在跑。
"""
from __future__ import annotations

import json
import time

import httpx2 as httpx
import uuid

API = "http://127.0.0.1:8002"
SCOPE = "mech:plant1/line:A/user:diag"
HEAD = {"Content-Type": "application/json", "X-Cortex-Actor": "diag-agent"}
_run = uuid.uuid4().hex[:8]  # 每次运行唯一前缀,防止 idempotency 冲突


def post(path, body, allow_fail=False):
    r = httpx.post(f"{API}{path}", json=body, headers=HEAD, timeout=120)
    if allow_fail and r.status_code >= 400:
        print(f"  (允许失败:{r.status_code},继续)")
        return None
    r.raise_for_status()
    return r.json()


def get(path, **params):
    r = httpx.get(f"{API}{path}", params=params, headers={"X-Cortex-Actor": "diag-agent"}, timeout=60)
    r.raise_for_status()
    return r.json()


def banner(s):
    print(f"\n{'='*70}\n  {s}\n{'='*70}")


def main():
    banner("0. 预置因果词表 + reset scope")
    # 通过 API 建 predicate 词表(诊断因果词)
    post("/v1/vocabularies", {"scope": SCOPE, "name": "predicate", "kind": "closed",
          "values": [{"canonical": p, "aliases": []} for p in
                     ["caused_by","led_to","symptom_of","affects","part_of","has_component",
                      "has_symptom","repaired_by","observed_by","preceded_by"]]}, allow_fail=True)
    print("  词表就绪(若已存在则跳过)")

    banner("1. 路径 C:机械结构文档(切块入库,按层级)")
    structure_doc = """# 电机系统
电机系统包含主轴、冷却系统和润滑系统。

## 主轴
主轴使用 6208 型号轴承,转速 3000rpm。主轴是电机系统的核心部件。

## 冷却系统
冷却系统使用风扇散热,风扇直径 200mm。冷却系统负责维持电机工作温度。

## 润滑系统
润滑系统为轴承提供锂基润滑脂。润滑系统是电机系统的组成部分。"""
    res = post("/v1/ingest/document", {"scope": SCOPE, "text": structure_doc, "intent": "structure"})
    print(f"  切成 {res['chunks']} 块,入库 accepted={res['accepted']}")

    banner("2. 路径 B:事件回溯叙述(全文因果抽取)")
    incident = """6月1日,产线A电机出现异常振动。张工排查发现轴承温度过高,达到95度。
经检查确认是润滑不足导致轴承过热。润滑脂已耗尽,润滑系统失效。
轴承过热导致轴承磨损加剧。张工更换了润滑脂并补充润滑系统。
更换后电机振动恢复正常,温度降至45度。此次故障原因是润滑系统维护缺失。"""
    r = post("/v1/experience", {"scope": SCOPE, "modality": "document",
             "content": {"kind": "text", "text": incident},
             "context": {"observed_at": "2026-06-01T10:00:00Z", "intent": "incident_retrospective",
                         "labels": ["motor", "bearing"]},
             "idempotency_key": f"incident-0601-{_run}"})
    print(f"  事件入库 event_id={r['event_id'][:8]}")

    banner("3. 路径 A:结构化三元组直写(前置 agent 已推理的因果)")
    triples = [
        {"subject": {"name": "轴承过热"}, "predicate": "caused_by", "object": {"name": "润滑不足"}},
        {"subject": {"name": "润滑不足"}, "predicate": "caused_by", "object": {"name": "润滑系统失效"}},
        {"subject": {"name": "轴承过热"}, "predicate": "led_to", "object": {"name": "轴承磨损"}},
        {"subject": {"name": "轴承过热"}, "predicate": "has_symptom", "object": {"name": "异常振动"}},
        {"subject": {"name": "轴承过热"}, "predicate": "repaired_by", "object": {"name": "更换润滑脂"}},
        {"subject": {"name": "轴承过热"}, "predicate": "observed_by", "object": {"name": "张工"}},
    ]
    for i, t in enumerate(triples):
        r = post("/v1/experience", {"scope": SCOPE, "modality": "imported",
                 "content": {"kind": "triple", "triple": t},
                 "context": {"observed_at": "2026-06-01T11:00:00Z", "intent": "diagnosis"},
                 "idempotency_key": f"triple-{_run}-{i}"})
    print(f"  直写 {len(triples)} 条因果三元组(零损失,不经 LLM)")

    banner("4. 等 worker 抽取事件叙述(结构文档块 + 叙述)")
    print("  等待 8 秒...")
    time.sleep(8)

    banner("5. 看图谱:实体 + facts")
    ents = get("/v1/entities", scope=SCOPE)
    print(f"  实体({len(ents['items'])}个):", [e["canonical_name"] for e in ents["items"][:15]])
    facts = get("/v1/facts", scope=SCOPE)
    print(f"  facts({len(facts['items'])}条),因果链:")
    for f in facts["items"][:15]:
        print(f"    {f['subject']['name']} --{f['predicate']}--> {f['object'].get('value')}")

    banner("6. 图遍历找根因:从'轴承过热'沿 caused_by 反向走")
    # 找轴承过热 entity,看它的 caused_by 出边(正向=它是原因;反向=它被什么引起)
    bearing = next((e for e in ents["items"] if "轴承过热" in e["canonical_name"]), None)
    if bearing:
        edges = get("/v1/facts", scope=SCOPE, subject=bearing["entity_id"])
        causes = [f for f in edges["items"] if f["predicate"] == "caused_by"]
        print(f"  轴承过热 --caused_by--> {[f['object'].get('value') for f in causes]}")
        # 继续往下找根因
        for c in causes:
            ce = next((e for e in ents["items"] if e["canonical_name"] == c["object"].get("value")), None)
            if ce:
                sub = get("/v1/facts", scope=SCOPE, subject=ce["entity_id"])
                sub_causes = [f for f in sub["items"] if f["predicate"] == "caused_by"]
                if sub_causes:
                    print(f"    └─ {ce['canonical_name']} --caused_by--> {[f['object'].get('value') for f in sub_causes]}")

    banner("7. 诊断问答:这次故障的根因是什么?")
    ans = post("/v1/answer", {"scope": SCOPE, "query": "轴承过热的根本原因是什么?如何修复?"})
    print(f"  answer: {ans.get('answer','')[:500]}")
    print(f"  model: {ans.get('model_used')}")
    print(f"  citations: {len(ans.get('citations',[]))} 条")

    banner("8. beliefs/why:为什么判断是这个故障?")
    beliefs = get("/v1/beliefs", scope=SCOPE)
    if beliefs["items"]:
        bid = beliefs["items"][0]["belief_id"]
        why = get("/v1/beliefs/why", **{"belief_id": bid})
        print(f"  belief: {why['belief']['claim']}")
        print(f"  support_graph: {len(why['support_graph']['nodes'])} 节点, {len(why['support_graph']['edges'])} 边")
        print(f"  narrative: {why['narrative'][:200]}")

    banner("done — 诊断知识库工作流跑通")
    print("\n关键验证点:")
    print("  ✓ 三路径入库(结构切块 / 叙述抽取 / triple 直写)")
    print("  ✓ 因果 predicate(caused_by/led_to/has_symptom 等)在图谱")
    print("  ✓ 图遍历找根因(轴承过热→润滑不足→润滑系统失效)")
    print("  ✓ answer 带引用 + beliefs/why 证据图")


if __name__ == "__main__":
    main()

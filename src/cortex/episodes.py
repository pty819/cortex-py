"""Episodes + 诊断 Case 管理。

两种模式:
  1. 自动分段(segment_scope):按时间窗/preceded_by 自动分组(原有逻辑)
  2. 显式 Case(create_case):下游 agent 创建诊断 case,关联 events,更新阶段/根因/修复

Case 生命周期: open → investigating → resolved → closed
Case 诊断阶段(phase): observation → scoping → investigation → correlation → root_cause → remediation → regression
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .db import session_scope

WINDOW_MIN = 30

_VALID_PHASES = {"observation", "scoping", "investigation", "correlation",
                 "root_cause", "remediation", "regression"}
_VALID_STATUSES = {"open", "investigating", "resolved", "closed"}


# ── 自动分段(原有逻辑 + case_id 感知)──────────────────────────────────────
def segment_scope(scope: str, since: Optional[str] = None) -> Dict[str, Any]:
    """扫 scope 内未排除的 events,按时间窗或 case_id 封存 episode。
    如果 events 有 case_id,同 case_id 的 events 分到同一 episode(不论时间间隔)。"""
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT event_id::text, observed_at, observed_actor, context, case_id
            FROM events WHERE scope=:s AND excluded_from_recall=false
            ORDER BY observed_at
        """), {"s": scope}).fetchall()
        if not rows:
            return {"built": 0, "items": []}

        already = set()
        ex = conn.execute(text("SELECT unnest(event_ids)::text FROM episodes WHERE scope=:s"), {"s": scope}).fetchall()
        already = {r[0] for r in ex}
        fresh = [r for r in rows if r[0] not in already]
        if not fresh:
            return {"built": 0, "items": [], "note": "no new events"}

        # 分组:先按 case_id 分(有 case_id 的),无 case_id 的按时间窗
        groups: List[list] = []
        case_groups: Dict[str, list] = {}
        time_group: list = []
        last_t = None
        for r in fresh:
            cid = r[4]  # case_id
            if cid:
                case_groups.setdefault(cid, []).append(r)
            else:
                if last_t is not None and time_group and (r.observed_at - last_t).total_seconds() > WINDOW_MIN * 60:
                    groups.append(time_group); time_group = []
                time_group.append(r); last_t = r.observed_at
        if time_group:
            groups.append(time_group)
        groups.extend(case_groups.values())

        items = []
        for g in groups:
            eids = [x[0] for x in g]
            eid_set = set(eids)
            actors = sorted({x[2] for x in g if x[2]})
            chain = []
            for x in g:
                ctx = x[3] or {}
                for pid in (ctx.get("preceded_by") or []):
                    if pid in eid_set:
                        chain.append({"from": pid, "to": x[0], "relation": "precedes"})
            g_case_id = g[0][4]  # 取第一个的 case_id
            ep = conn.execute(text("""
                INSERT INTO episodes (scope, event_ids, actors, causal_chain, started_at, ended_at, valid_from, sealed, case_id)
                VALUES (:s, CAST(:eids AS uuid[]), CAST(:actors AS text[]), CAST(:chain AS jsonb),
                        :st, :et, :st, true, :cid)
                RETURNING episode_id::text, started_at::text, ended_at::text
            """), {"s": scope,
                   "eids": "{" + ",".join(eids) + "}",
                   "actors": "{" + ",".join(actors) + "}" if actors else "{}",
                   "chain": json.dumps(chain),
                   "st": g[0].observed_at, "et": g[-1].observed_at,
                   "cid": g_case_id}).fetchone()
            items.append({"episode_id": ep[0], "event_ids": eids, "actors": actors,
                          "started_at": ep[1], "ended_at": ep[2], "causal_chain": chain,
                          "case_id": g_case_id})
        return {"built": len(items), "items": items}


# ── 显式 Case 管理 ─────────────────────────────────────────────────────────
def create_case(*, scope: str, title: Optional[str] = None, case_id: Optional[str] = None,
                equipment: Optional[str] = None, lot: Optional[str] = None,
                recipe: Optional[str] = None, metadata: Optional[Dict] = None) -> Dict[str, Any]:
    """创建一个诊断 case(空 episode,待关联 events)。返回 {episode_id, case_id}。"""
    with session_scope() as conn:
        row = conn.execute(text("""
            INSERT INTO episodes (scope, title, case_id, equipment, lot, recipe, metadata,
                                  started_at, valid_from, sealed, status, phase)
            VALUES (:s, :t, :cid, :eq, :lot, :rec, CAST(:meta AS jsonb),
                    now(), now(), false, 'open', 'observation')
            RETURNING episode_id::text
        """), {"s": scope, "t": title, "cid": case_id, "eq": equipment, "lot": lot,
               "rec": recipe, "meta": json.dumps(metadata or {})}).fetchone()
        return {"episode_id": row[0], "case_id": case_id, "scope": scope, "status": "open", "phase": "observation"}


def update_case(episode_id: str, **fields) -> Dict[str, Any]:
    """更新 case 的 phase/status/root_cause/resolution/equipment/lot/recipe/title。"""
    allowed = {"title", "equipment", "lot", "recipe", "root_cause", "resolution", "metadata"}
    phase = fields.get("phase")
    status = fields.get("status")
    if phase and phase not in _VALID_PHASES:
        return {"error": f"invalid phase: {phase}, valid: {_VALID_PHASES}"}
    if status and status not in _VALID_STATUSES:
        return {"error": f"invalid status: {status}, valid: {_VALID_STATUSES}"}
    allowed.update({"phase", "status"})

    sets = []
    params: Dict[str, Any] = {"eid": episode_id}
    for k, v in fields.items():
        if k in allowed and v is not None:
            if k == "metadata":
                sets.append(f"{k} = CAST(:{k} AS jsonb)")
                params[k] = json.dumps(v)
            else:
                sets.append(f"{k} = :{k}")
                params[k] = v
    if not sets:
        return {"error": "no valid fields to update"}
    with session_scope() as conn:
        r = conn.execute(text(f"UPDATE episodes SET {', '.join(sets)} WHERE episode_id=CAST(:eid AS uuid)"), params)
        if not r.rowcount:
            return {"error": "case not found"}
    return {"episode_id": episode_id, "updated": list(params.keys())[1:]}


def add_event_to_case(episode_id: str, event_id: str) -> Dict[str, Any]:
    """把 event 关联到 case:更新 events.case_id + episodes.event_ids。"""
    with session_scope() as conn:
        ep = conn.execute(text("SELECT scope, event_ids::text[], case_id FROM episodes WHERE episode_id=CAST(:e AS uuid)"),
                          {"e": episode_id}).fetchone()
        if not ep:
            return {"error": "case not found"}
        existing = set(ep[1] or [])
        if event_id in existing:
            return {"episode_id": episode_id, "event_id": event_id, "note": "already in case"}
        new_ids = list(existing) + [event_id]
        conn.execute(text("""
            UPDATE episodes SET event_ids = CAST(:ids AS uuid[]) WHERE episode_id=CAST(:e AS uuid)
        """), {"ids": "{" + ",".join(new_ids) + "}", "e": episode_id})
        # 写 events.case_id: 优先用业务 case_id,无则用 episode_id
        conn.execute(text("UPDATE events SET case_id=:cid WHERE event_id=CAST(:e AS uuid)"),
                     {"cid": ep[2] or episode_id, "e": event_id})
    return {"episode_id": episode_id, "event_id": event_id, "added": True}


def get_case(episode_id: str) -> Optional[Dict[str, Any]]:
    """返回完整 case:episode 元数据 + events 列表 + facts 列表 + beliefs 列表。"""
    with session_scope() as conn:
        ep = conn.execute(text("""
            SELECT episode_id::text, scope, title, case_id, equipment, lot, recipe, phase,
                   root_cause, resolution, status, metadata, event_ids::text[], actors::text[],
                   causal_chain, started_at::text, ended_at::text
            FROM episodes WHERE episode_id=CAST(:e AS uuid) AND recorded_to IS NULL
        """), {"e": episode_id}).fetchone()
        if not ep:
            return None
        case = dict(episode_id=ep[0], scope=ep[1], title=ep[2], case_id=ep[3], equipment=ep[4],
                    lot=ep[5], recipe=ep[6], phase=ep[7], root_cause=ep[8], resolution=ep[9],
                    status=ep[10], metadata=ep[11] or {}, event_ids=list(ep[12] or []),
                    actors=list(ep[13] or []), causal_chain=ep[14] or [],
                    started_at=ep[15], ended_at=ep[16])

        # events
        if case["event_ids"]:
            evrows = conn.execute(text("""
                SELECT event_id::text, modality, content, observed_actor, observed_at::text
                FROM events WHERE event_id = ANY(CAST(:ids AS uuid[]))
            """), {"ids": "{" + ",".join(case["event_ids"]) + "}"}).fetchall()
            case["events"] = [{"event_id": r[0], "modality": r[1], "content": r[2],
                                "observed_actor": r[3], "observed_at": r[4]} for r in evrows]
        else:
            case["events"] = []

        # facts (通过 supports 关联到 events 的)
        if case["event_ids"]:
            frows = conn.execute(text("""
                SELECT f.fact_id::text, f.predicate, f.object_type, f.object_value,
                       s.canonical_name, o.canonical_name, f.confidence
                FROM facts f JOIN entities s ON s.entity_id=f.subject_id
                LEFT JOIN entities o ON o.entity_id=f.object_entity_id
                WHERE f.scope=:s AND f.valid_to IS NULL AND f.recorded_to IS NULL
                AND EXISTS (SELECT 1 FROM unnest(f.supports) AS sid WHERE sid = ANY(CAST(:ids AS uuid[])))
                LIMIT 50
            """), {"s": case["scope"], "ids": "{" + ",".join(case["event_ids"]) + "}"}).fetchall()
            case["facts"] = [{"fact_id": r[0], "predicate": r[1], "object_type": r[2],
                              "object_value": r[3], "subject_name": r[4],
                              "object_name": r[5], "confidence": r[6]} for r in frows]
        else:
            case["facts"] = []

        # beliefs
        brows = conn.execute(text("""
            SELECT belief_id::text, claim, confidence, stance
            FROM beliefs WHERE scope=:s AND valid_to IS NULL AND recorded_to IS NULL LIMIT 10
        """), {"s": case["scope"]}).fetchall()
        case["beliefs"] = [{"belief_id": r[0], "claim": r[1], "confidence": r[2], "stance": r[3]} for r in brows]
    return case


def list_cases(scope: str, status: Optional[str] = None, equipment: Optional[str] = None,
               limit: int = 50) -> List[Dict[str, Any]]:
    """列出 cases,可按 status/equipment 过滤。"""
    sql = """SELECT episode_id::text, title, case_id, equipment, lot, recipe, phase,
                    root_cause, resolution, status, started_at::text, ended_at::text
             FROM episodes WHERE scope=:s AND recorded_to IS NULL"""
    p: Dict[str, Any] = {"s": scope, "lim": limit}
    if status:
        sql += " AND status=:st"; p["st"] = status
    if equipment:
        sql += " AND equipment=:eq"; p["eq"] = equipment
    sql += " ORDER BY started_at DESC LIMIT :lim"
    with session_scope() as conn:
        rows = conn.execute(text(sql), p).fetchall()
    return [dict(episode_id=r[0], title=r[1], case_id=r[2], equipment=r[3], lot=r[4],
                 recipe=r[5], phase=r[6], root_cause=r[7], resolution=r[8], status=r[9],
                 started_at=r[10], ended_at=r[11]) for r in rows]


def search_cases(scope: str, query: str, limit: int = 20) -> List[Dict[str, Any]]:
    """按 root_cause/title/equipment 模糊搜 cases。"""
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT episode_id::text, title, equipment, phase, root_cause, resolution, status, started_at::text
            FROM episodes WHERE scope=:s AND recorded_to IS NULL
            AND (title ILIKE :q OR root_cause ILIKE :q OR equipment ILIKE :q OR resolution ILIKE :q)
            ORDER BY started_at DESC LIMIT :lim
        """), {"s": scope, "q": f"%{query}%", "lim": limit}).fetchall()
    return [dict(episode_id=r[0], title=r[1], equipment=r[2], phase=r[3], root_cause=r[4],
                 resolution=r[5], status=r[6], started_at=r[7]) for r in rows]


# ── 原有 list_episodes 保留(向下兼容)────────────────────────────────────────
def list_episodes(scope: str, limit: int = 50) -> List[Dict[str, Any]]:
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT episode_id::text, event_ids::text[], actors::text[], causal_chain,
                   started_at::text, ended_at::text, sealed
            FROM episodes WHERE scope=:s AND recorded_to IS NULL
            ORDER BY started_at DESC LIMIT :lim
        """), {"s": scope, "lim": limit}).fetchall()
    return [{"episode_id": r[0], "event_ids": list(r[1] or []), "actors": list(r[2] or []),
             "causal_chain": r[3] or [], "started_at": r[4], "ended_at": r[5], "sealed": r[6]} for r in rows]

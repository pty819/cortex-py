"""Episodes segmenter:把 scope 内 events 按时间窗封存为有界 episode。

策略(起步,可替换接口):
  - 相邻 event 的 observed_at 间隔 > 阈值(默认 30min)→ 封存当前组
  - actors = 组内 observed_actor 去重
  - causal_chain = 组内 context.preceded_by → {from,to,relation:'precedes'}
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .config import load_config
from .db import session_scope

WINDOW_MIN = 30


def segment_scope(scope: str, since: Optional[str] = None) -> Dict[str, Any]:
    """扫 scope 内未排除的 events,按时间窗封存 episode。返回 {built, items}。"""
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT event_id::text, observed_at, observed_actor, context
            FROM events WHERE scope=:s AND excluded_from_recall=false
            ORDER BY observed_at
        """), {"s": scope}).fetchall()
        if not rows:
            return {"built": 0, "items": []}

        # 已属某 episode 的 event 不重复封存
        already = set()
        ex = conn.execute(text("SELECT unnest(event_ids)::text FROM episodes WHERE scope=:s"), {"s": scope}).fetchall()
        already = {r[0] for r in ex}
        fresh = [r for r in rows if r[0] not in already]
        if not fresh:
            return {"built": 0, "items": [], "note": "no new events"}

        # 分组(时间窗)
        groups: List[list] = []
        cur: list = []
        last_t = None
        for r in fresh:
            if last_t is not None and cur and (r.observed_at - last_t).total_seconds() > WINDOW_MIN * 60:
                groups.append(cur); cur = []
            cur.append(r); last_t = r.observed_at
        if cur:
            groups.append(cur)

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
            ep = conn.execute(text("""
                INSERT INTO episodes (scope, event_ids, actors, causal_chain, started_at, ended_at, valid_from, sealed)
                VALUES (:s, CAST(:eids AS uuid[]), CAST(:actors AS text[]), CAST(:chain AS jsonb),
                        :st, :et, :st, true)
                RETURNING episode_id::text, started_at::text, ended_at::text
            """), {"s": scope,
                   "eids": "{" + ",".join(eids) + "}",
                   "actors": "{" + ",".join(actors) + "}" if actors else "{}",
                   "chain": json.dumps(chain),
                   "st": g[0].observed_at, "et": g[-1].observed_at}).fetchone()
            items.append({"episode_id": ep[0], "event_ids": eids, "actors": actors,
                          "started_at": ep[1], "ended_at": ep[2], "causal_chain": chain})
        return {"built": len(items), "items": items}


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

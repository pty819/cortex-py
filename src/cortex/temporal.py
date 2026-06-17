"""Temporal phrases:NL 时间短语注册 + 解析(05 §C 档 / 07 §5)。

expression = 两 ISO8601 duration 以 '..' 隔,相对 anchor。如 last_week = -P7D..P0D → [anchor-7d, anchor]。
解析:parse_temporal('last week', reference_date) → (from, to)。
"""
from __future__ import annotations

import re
from datetime import timedelta, datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import text

from .db import session_scope

_DUR_RE = re.compile(r"^(-)?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")

_DEFAULTS = [
    ("last week", "-P7D..P0D"),
    ("last month", "-P30D..P0D"),
    ("yesterday", "-P1D..P0D"),
    ("last quarter", "-P90D..P0D"),
    ("last year", "-P365D..P0D"),
]


def _parse_dur(s: str) -> timedelta:
    m = _DUR_RE.match(s.strip())
    if not m:
        raise ValueError(f"bad ISO8601 duration: {s!r}")
    sign = -1 if m.group(1) else 1
    y, mo, w, d, h, mi, se = (int(x) if x else 0 for x in m.groups()[1:])
    return sign * timedelta(days=y * 365 + mo * 30 + w * 7 + d, hours=h, minutes=mi, seconds=se)


def parse_expression(expr: str, anchor: datetime) -> Tuple[datetime, datetime]:
    parts = expr.split("..")
    if len(parts) != 2:
        raise ValueError(f"expression must be 'dur..dur': {expr!r}")
    return anchor + _parse_dur(parts[0]), anchor + _parse_dur(parts[1])


def seed_defaults() -> int:
    n = 0
    with session_scope() as conn:
        for name, expr in _DEFAULTS:
            r = conn.execute(text("""
                INSERT INTO temporal_phrases (name, expression, is_default)
                VALUES (:n, :e, true) ON CONFLICT (name) DO NOTHING
            """), {"n": name, "e": expr})
            n += r.rowcount or 0
    return n


def register_phrase(name: str, expression: str, anchor: Optional[datetime] = None) -> str:
    with session_scope() as conn:
        row = conn.execute(text("""
            INSERT INTO temporal_phrases (name, anchor, expression, is_default)
            VALUES (:n, COALESCE(:a, now()), :e, false)
            ON CONFLICT (name) DO UPDATE SET expression=:e, anchor=COALESCE(:a, now())
            RETURNING phrase_id::text
        """), {"n": name.lower(), "a": anchor, "e": expression}).fetchone()
        return row[0]


def list_phrases() -> List[dict]:
    with session_scope() as conn:
        rows = conn.execute(text("""
            SELECT name, expression, is_default, anchor::text FROM temporal_phrases ORDER BY name
        """)).fetchall()
    return [{"name": r[0], "expression": r[1], "is_default": r[2], "anchor": r[3]} for r in rows]


def delete_phrase(name: str) -> bool:
    with session_scope() as conn:
        r = conn.execute(text("DELETE FROM temporal_phrases WHERE name=:n"), {"n": name.lower()})
        return (r.rowcount or 0) > 0


def parse_temporal(natural: str, reference_date: Optional[datetime] = None) -> Optional[Tuple[datetime, datetime]]:
    """词表命中 → (from, to)。reference_date 默认 now。"""
    ref = reference_date or datetime.now(timezone.utc)
    with session_scope() as conn:
        row = conn.execute(text("SELECT expression, anchor FROM temporal_phrases WHERE name=:n"),
                           {"n": natural.lower().strip()}).fetchone()
    if not row:
        return None
    anchor = row[1].replace(tzinfo=timezone.utc) if row[1] else ref
    return parse_expression(row[0], ref if ref else anchor)

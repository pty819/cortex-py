"""Token 预算工具:按相关度顺序填 items 到 token 预算,超预算停。替代硬编码 [:N] 截断。

estimator:粗估 token(~4 字符/token,中文偏保守按 2 字符/token,取中 ~3)。
专业场景(机械诊断)证据链长,不能硬截条数;按预算填保证不丢关键信息。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def estimate_tokens(obj: Any) -> int:
    """粗估对象的 token 数(JSON 序列化后按 ~3 字符/token)。"""
    if isinstance(obj, str):
        return max(1, len(obj) // 3)
    try:
        return max(1, len(json.dumps(obj, ensure_ascii=False, default=str)) // 3)
    except Exception:  # noqa: BLE001
        return max(1, len(str(obj)) // 3)


def fit_to_budget(items: List[Any], max_tokens: Optional[int],
                  reserve_tokens: int = 0) -> List[Any]:
    """按顺序填 items 到 (max_tokens - reserve_tokens) 预算。max_tokens=None 返回全部。"""
    if max_tokens is None:
        return list(items)
    budget = max(0, max_tokens - reserve_tokens)
    out: List[Any] = []
    used = 0
    for it in items:
        t = estimate_tokens(it)
        if used + t > budget and out:  # 超预算且已有至少一条,停(保证不返回空)
            break
        out.append(it)
        used += t
        if used >= budget:
            break
    return out


def fit_dicts_by_field(items: List[Dict], field: str, max_tokens: Optional[int],
                       reserve_tokens: int = 0) -> List[Dict]:
    """按某字段内容估算 token 填充(如 facts 按 summary)。"""
    if max_tokens is None:
        return list(items)
    budget = max(0, max_tokens - reserve_tokens)
    out, used = [], 0
    for it in items:
        t = estimate_tokens(it.get(field, ""))
        if used + t > budget and out:
            break
        out.append(it)
        used += t
        if used >= budget:
            break
    return out

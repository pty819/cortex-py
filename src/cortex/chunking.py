"""文档切块:按标题/段落切长文档,保留层级 path,不傻切固定窗口。

机械结构文档(有标题章节)→ 按标题切;无标题 → 按段落;段落过长 → 按句号。
每块带 path(如 "电机系统/主轴")和 depth,便于抽取时建 part_of 关系。
不 overlap(机械结构块边界清晰,overlap 反而混淆)。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.M)
_MIN_CHUNK = 200      # 最小块字数(太小合并到父)
_MAX_CHUNK = 2000     # 单块上限(超过再按句号细分)


def chunk_document(text: str, min_chars: int = _MIN_CHUNK,
                   max_chars: int = _MAX_CHUNK) -> List[Dict[str, Any]]:
    """切长文档。返回 [{text, heading, path, depth}]。

    策略:
      1. 有 markdown 标题 → 按标题层级切,每块带 path(祖先标题拼接)
      2. 无标题 → 按段落(双换行)切
      3. 块 > max_chars → 按句号细切
      4. 块 < min_chars 且非末尾 → 合并到上一块
    """
    text = text.strip()
    if not text:
        return []
    if _HEADING_RE.search(text):
        chunks = _chunk_by_headings(text)
    else:
        chunks = _chunk_by_paragraphs(text)
    # 过短合并 + 过长细分
    chunks = _merge_small(chunks, min_chars)
    chunks = _split_large(chunks, max_chars)
    return chunks


def _chunk_by_headings(text: str) -> List[Dict[str, Any]]:
    """按 markdown 标题切,维护层级栈。"""
    chunks: List[Dict[str, Any]] = []
    lines = text.split("\n")
    stack: List[tuple] = []   # [(level, title)]
    cur_text: List[str] = []
    cur_heading = ""
    cur_depth = 0

    def flush():
        body = "\n".join(cur_text).strip()
        if body:
            path = "/".join(t for _, t in stack)
            chunks.append({"text": body, "heading": cur_heading, "path": path, "depth": cur_depth})

    for ln in lines:
        m = _HEADING_RE.match(ln)
        if m:
            flush()
            cur_text = []
            level = len(m.group(1))
            title = m.group(2).strip()
            # 维护层级栈:弹出比当前 level 深的
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_heading = title
            cur_depth = len(stack)
            # 标题行本身也带上(让 LLM 知道当前在讲哪个部件)
            cur_text.append(ln)
        else:
            cur_text.append(ln)
    flush()
    return chunks


def _chunk_by_paragraphs(text: str) -> List[Dict[str, Any]]:
    """无标题:按双换行段落切。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [{"text": p, "heading": "", "path": "", "depth": 0} for p in paras]


def _merge_small(chunks: List[Dict], min_chars: int) -> List[Dict]:
    """只合并无 heading 的段落块;带 heading 的块(结构节点)保持独立。"""
    if len(chunks) <= 1:
        return chunks
    out = [dict(chunks[0])]
    for c in chunks[1:]:
        if not c.get("heading") and len(out[-1]["text"]) < min_chars and not out[-1].get("heading"):
            out[-1]["text"] += "\n" + c["text"]
        else:
            out.append(dict(c))
    return out


def _split_large(chunks: List[Dict], max_chars: int) -> List[Dict]:
    out = []
    for c in chunks:
        if len(c["text"]) <= max_chars:
            out.append(c)
            continue
        # 按句号/换行细切
        sents = re.split(r"(?<=[。.!！\n])\s+", c["text"])
        cur = ""
        for s in sents:
            if len(cur) + len(s) > max_chars and cur:
                out.append({**c, "text": cur})
                cur = s
            else:
                cur += s
        if cur:
            out.append({**c, "text": cur})
    return out

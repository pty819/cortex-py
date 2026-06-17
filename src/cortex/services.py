"""外部服务客户端:embedding(rereank)/rerank(prism)/LLM(Minimax,缺 key 走确定性 mock)。

无 LLM key 时,LLM 抽取/回答走确定性 mock(规则解析),保证整条管线可端到端验证;
key 入 config 后自动切真实 Minimax(见 llm_configured())。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

import httpx

from .config import RerankCfg, EmbeddingCfg, LLMTierCfg, load_config, llm_configured


# ── embedding ──────────────────────────────────────────────────────────────
def embed_texts(texts: List[str], cfg: Optional[EmbeddingCfg] = None) -> List[List[float]]:
    """调 jina-v5 → 返回 dim=1024 向量列表(顺序对齐输入)。"""
    cfg = cfg or load_config().embedding
    url = cfg.api_base.rstrip("/") + "/embeddings"
    with httpx.Client(timeout=cfg.timeout) as cli:
        r = cli.post(url, json={"model": cfg.model, "input": texts},
                     headers={"Authorization": f"Bearer {cfg.api_key}"})
        r.raise_for_status()
        data = r.json()["data"]
    # 按 index 排序,确保顺序对齐
    data.sort(key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def embed_one(text: str) -> List[float]:
    return embed_texts([text])[0]


# ── rerank ──────────────────────────────────────────────────────────────────
def rerank(query: str, documents: List[str], cfg: Optional[RerankCfg] = None) -> List[Dict[str, Any]]:
    """调 prism rerank → 返回 [{"index","relevance_score","document"}, ...] 按 score 降序。"""
    cfg = cfg or load_config().rerank
    url = cfg.api_base.rstrip("/") + "/rerank"
    with httpx.Client(timeout=cfg.timeout) as cli:
        r = cli.post(url, json={"model": cfg.model, "query": query, "documents": documents, "top_n": cfg.top_n},
                     headers={"Authorization": f"Bearer {cfg.api_key}"})
        r.raise_for_status()
        out = r.json()
    results = out.get("results") or out.get("data") or []
    results.sort(key=lambda d: d.get("relevance_score", 0), reverse=True)
    return results


# ── LLM ─────────────────────────────────────────────────────────────────────
def _llm_client(tier: str):
    """返回 (openai client, model, tier_cfg)。无 key 时返回 None。"""
    from openai import OpenAI
    cfg = load_config().llm.model_dump()[tier]
    if not cfg.get("api_key") or str(cfg["api_key"]).startswith("REPLACE_WITH"):
        return None
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["api_base"], timeout=cfg["timeout"],
                    max_retries=cfg.get("max_retries", 2))
    return client, cfg["model"], cfg


def llm_chat(tier: str, system: str, user: str, response_format: Optional[Dict] = None) -> str:
    """同步调 LLM;返回 assistant 文本。无 key 抛 LLMUnavailable。"""
    c = _llm_client(tier)
    if c is None:
        raise LLMUnavailable(f"LLM tier '{tier}' 无 key(配置占位符)。")
    client, model, _ = c
    kwargs: Dict[str, Any] = {"model": model, "messages": [{"role": "system", "content": system},
                                                           {"role": "user", "content": user}],
                              "temperature": 0.0}
    if response_format:
        kwargs["response_format"] = response_format
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


class LLMUnavailable(RuntimeError):
    pass


def strip_think(raw: str) -> str:
    """剥 Minimax-M3 的 <think>...</think> 推理段(闭合 + 未闭合都处理)。"""
    if not raw:
        return ""
    s = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    s = re.sub(r"<think>.*$", "", s, flags=re.S)   # 未闭合兜底
    return s.strip()


def parse_llm_json(raw: str) -> Any:
    """从 LLM 响应里健壮提取 JSON 对象:剥 think → 试 ```json``` 代码块 → 括号匹配。"""
    s = strip_think(raw)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, flags=re.S)
    if m:
        return json.loads(m.group(1))
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    instr = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        else:
            if c == '"':
                instr = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(s[start:i + 1])
    return json.loads(s[start:])  # 让 json 报具体错


# ── mock 抽取器(确定性,无 key 时用)─────────────────────────────────────────
# 规则:从文本里识别 "X <predicate> Y" 模式 + 大写专有名词,产出确定性三元组。
# 这不是真抽取,只为让管线在缺 LLM key 时可端到端验证(图能长出来、检索有东西)。
_PREDICATES = {
    "works at": "works_at", "work at": "works_at", "joined": "joined",
    "owns": "owns", "own": "owns", "leads": "leads", "lead": "leads",
    "manages": "manages", "reports to": "reports_to",
    "uses": "uses", "use": "uses", "depends on": "depends_on",
    "signed": "signed", "renewed": "renewed", "acquired": "acquired",
    "is": "is_a", "are": "is_a",
}

_PROPER_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b")


def _split_proper(phrase: str) -> str:
    return phrase.strip().rstrip(",.;")


def mock_extract(text: str) -> Dict[str, Any]:
    """确定性抽取:按连接词分句,每句找 "X <pred> Y",产出干净三元组。

    这不是真抽取,只为让管线在缺 LLM key 时可端到端验证(图能长出来、检索有东西)。
    """
    entities: Dict[str, Dict[str, Any]] = {}
    facts: List[Dict[str, Any]] = []
    # 按句号/分号/连接词切分,降低贪吃
    clauses = re.split(r"(?:\.\s+|\;\s*|\s+and\s+|\s+but\s+|,\s+(?=[A-Z]))", text.replace("\n", " "))
    for clause in clauses:
        cl = clause.strip()
        if not cl:
            continue
        for pat, pred in _PREDICATES.items():
            m = re.search(rf"^([A-Z][\w&''\-']*(?:\s+[A-Z][a-z]+){{0,2}})\s+{re.escape(pat)}\s+(.+)$", cl)
            if not m:
                continue
            subj = _split_proper(m.group(1))
            obj = _split_proper(m.group(2)).rstrip(".;")
            # object 截断到第一个名词短语(避免吞掉后续)
            obj = re.split(r"\s+(?:and|but|while|because|so|then|which|that|,|\.)\s", obj)[0].strip()
            if not subj or not obj or len(subj) > 50 or len(obj) > 50:
                continue
            for nm, ty in ((subj, "person_or_org"), (obj, "person_or_org")):
                entities.setdefault(nm.lower(), {"name": nm, "type": ty, "description": nm})
            facts.append({"subject": subj, "predicate": pred, "object": obj, "object_type": "entity"})

    # 兜底:若无模式命中,把专有名词当实体
    if not entities:
        names = list(dict.fromkeys(_split_proper(m.group(1)) for m in _PROPER_RE.finditer(text)))[:6]
        for nm in names:
            if len(nm) > 2:
                entities.setdefault(nm.lower(), {"name": nm, "type": "entity", "description": nm})

    return {"entities": list(entities.values()), "facts": facts,
            "_model": "mock-extractor", "_note": "确定性规则抽取(无 LLM key);配 key 后切真实 Minimax-M3"}


def mock_answer(query: str, pack_json: str) -> str:
    """无 LLM key 时,从 pack 里拼一个带引用的简答。"""
    try:
        pack = json.loads(pack_json)
    except Exception:  # noqa: BLE001
        return "(无可用上下文)"
    facts = pack.get("layers", {}).get("facts", [])
    if not facts:
        return "(暂无相关记忆)"
    lines = []
    for i, f in enumerate(facts[:4], 1):
        s = f.get("subject", {}).get("name", "?")
        p = f.get("predicate", "?")
        o = f.get("object", {}).get("value", "?")
        lines.append(f"[{i}] {s} {p} {o}")
    return "据已入库记忆:" + "; ".join(lines) + "。"

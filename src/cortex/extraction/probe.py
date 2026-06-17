"""R1 探针:测 Minimax-M3 对 response_format=json_schema 的支持。
无 key 时:文档化 fallback 链,返回 0。有 key 时:实测三种模式,打印结果。
"""
from __future__ import annotations

import json

from .. import services
from ..config import load_config, llm_configured


def run_probe() -> int:
    print("=== R1 探针:Minimax-M3 structured output 支持 ===")
    if not llm_configured("extraction"):
        print("状态:无 LLM key。当前走确定性 mock 抽取,管线可端到端跑。")
        print("fallback 链(配 key 后按序尝试):")
        print("  1. json_schema  (response_format type=json_schema)")
        print("  2. json_object  (response_format type=json_object + prompt 要求 JSON)")
        print("  3. prompt       (纯 prompt + 应用层 JSON 修复)")
        print("配置:llm.extraction.structured_output_mode = json_schema|json_object|prompt")
        print("\n请提供 Minimax key(填入 config.yaml 或 CORTEX_LLM_EXTRACTION_API_KEY)后重跑此探针。")
        return 0

    cfg = load_config().llm.extraction
    test_text = "Priya Rao works at Acme Corp and owns the Q3 Renewal project."
    schema = {"type": "object", "properties": {"facts": {"type": "array", "items": {"type": "object"}}},
              "required": ["facts"]}
    for mode in ["json_schema", "json_object"]:
        try:
            rf = ({"type": "json_schema", "json_schema": {"name": "t", "schema": schema}}
                  if mode == "json_schema" else {"type": "json_object"})
            out = services.llm_chat("extraction", "Extract facts as JSON {facts:[]}.", test_text, rf)
            json.loads(out)  # 能解析即视为支持
            print(f"  [{mode}] ✓ 支持,响应可解析为 JSON")
        except Exception as e:  # noqa: BLE001
            print(f"  [{mode}] ✗ 失败:{e}")
            print(f"      → 降级到下一档(mode={mode} 不可用)")
    print("\n依据结果把 llm.extraction.structured_output_mode 设为支持的最高档。")
    return 0

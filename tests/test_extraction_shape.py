"""抽取结果结构校验单测(_validate_extraction_shape)。

覆盖 P0-2 的核心:推理模型返回语法合法、但被 token 截断的残缺 JSON 时,
校验函数必须判定为不完整,从而让 _llm_extract 的 fallback 链真正跳到下一个 mode。
本测试纯函数,不依赖 DB。
"""
import pytest

from cortex.extraction.pipeline import _validate_extraction_shape


# ── 合法输入 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("data", [
    {"entities": [], "facts": []},                                   # 空抽取,合法
    {"entities": [{"name": "A", "type": "org", "description": "x"}]},
    {"facts": [{"subject": "A", "predicate": "p", "object": "B"}]},
    {"entities": [{"name": "A"}], "facts": [
        {"subject": "A", "predicate": "p", "object": "B", "confidence": 0.9}]},
])
def test_valid_shapes_pass(data):
    ok, why = _validate_extraction_shape(data)
    assert ok is True
    assert why == ""


# ── 残缺输入(核心场景:被 token 截断的 JSON)─────────────────────────────────
def test_fact_missing_required_field_is_incomplete():
    """facts 数组里有一项缺 object(典型的半截对象截断)。"""
    ok, why = _validate_extraction_shape(
        {"entities": [{"name": "A"}],
         "facts": [{"subject": "A", "predicate": "p"}]})  # 缺 object
    assert ok is False
    assert "object" in why


def test_fact_missing_subject_is_incomplete():
    ok, why = _validate_extraction_shape(
        {"facts": [{"predicate": "p", "object": "B"}]})  # 缺 subject
    assert ok is False
    assert "subject" in why


def test_fact_missing_predicate_is_incomplete():
    ok, why = _validate_extraction_shape(
        {"facts": [{"subject": "A", "object": "B"}]})  # 缺 predicate
    assert ok is False
    assert "predicate" in why


def test_entity_missing_name_is_incomplete():
    ok, why = _validate_extraction_shape(
        {"entities": [{"type": "org", "description": "no name here"}]})  # 缺 name
    assert ok is False
    assert "name" in why


def test_facts_not_a_list_is_incomplete():
    ok, why = _validate_extraction_shape({"facts": {"subject": "A"}})  # dict 而非 list
    assert ok is False
    assert "facts" in why


def test_fact_entry_not_an_object_is_incomplete():
    ok, why = _validate_extraction_shape({"facts": ["not-an-object"]})
    assert ok is False


# ── 边界:仅部分键存在仍可校验 ────────────────────────────────────────────────
def test_only_entities_present_validates_entities():
    ok, why = _validate_extraction_shape({"entities": [{"name": "A"}]})
    assert ok is True


def test_missing_field_reports_index():
    """第 3 条 fact 残缺,错误信息要能定位到索引,便于调试。"""
    ok, why = _validate_extraction_shape(
        {"facts": [
            {"subject": "A", "predicate": "p", "object": "B"},
            {"subject": "A", "predicate": "p", "object": "C"},
            {"subject": "A", "predicate": "p"},  # 第 3 条缺 object
        ]})
    assert ok is False
    assert "facts[2]" in why

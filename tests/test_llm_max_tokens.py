"""llm_chat 的 max_tokens 优先级单测(P1-1)。

验证:max_tokens 参数不传(默认 None)时读 tier 配置 llm.<tier>.max_tokens;
显式传入时覆盖配置。这保障了 entity-link 的 1024 覆盖 + extraction 读 32768。
不调真实 OpenAI,用假 client 捕获 kwargs。
"""
import types

import cortex.services as services


class _FakeChoice:
    def __init__(self):
        self.message = types.SimpleNamespace(content="{}")


class _FakeResp:
    def __init__(self):
        self.choices = [_FakeChoice()]


class _FakeCompletions:
    def __init__(self):
        self.captured_kwargs = None

    def create(self, **kwargs):
        self.captured_kwargs = kwargs
        return _FakeResp()


class _FakeClient:
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions())


def _patch_llm(monkeypatch, tier_cfg):
    """让 _llm_client 返回同一个 fake_client 实例 + tier_cfg。返回 fake 供断言。"""
    fake = _FakeClient()
    monkeypatch.setattr(
        services, "_llm_client",
        lambda tier: (fake, tier_cfg.get("model", "m"), tier_cfg),
    )
    return fake


def test_uses_config_max_tokens_when_param_not_passed(monkeypatch):
    """不传 max_tokens → 读 cfg['max_tokens']=32768。"""
    fake = _patch_llm(monkeypatch, {"model": "m", "max_tokens": 32768})
    services.llm_chat("extraction", "sys", "user")
    assert fake.chat.completions.captured_kwargs["max_tokens"] == 32768


def test_explicit_param_overrides_config(monkeypatch):
    """显式传 max_tokens=1024(entity-link 场景)→ 覆盖配置值。"""
    fake = _patch_llm(monkeypatch, {"model": "m", "max_tokens": 32768})
    services.llm_chat("extraction", "sys", "user", max_tokens=1024)
    assert fake.chat.completions.captured_kwargs["max_tokens"] == 1024


def test_falls_back_to_default_16384_when_cfg_missing(monkeypatch):
    """cfg 里没有 max_tokens 键 → 兜底 16384。"""
    fake = _patch_llm(monkeypatch, {"model": "m"})  # 无 max_tokens
    services.llm_chat("extraction", "sys", "user")
    assert fake.chat.completions.captured_kwargs["max_tokens"] == 16384


def test_explicit_none_reads_config(monkeypatch):
    """显式传 None 等价于不传 → 读配置。"""
    fake = _patch_llm(monkeypatch, {"model": "m", "max_tokens": 8192})
    services.llm_chat("extraction", "sys", "user", max_tokens=None)
    assert fake.chat.completions.captured_kwargs["max_tokens"] == 8192

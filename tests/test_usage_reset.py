"""--reset 清可用性，不清 token 累计。"""
from __future__ import annotations

from pathlib import Path

from llm.pool import ModelRouter, ModelSpec, ModelStatus


def test_reset_keeps_token_totals(tmp_path: Path):
    router = ModelRouter([ModelSpec(name="m")], state_path=tmp_path / "pool.json")
    spec = router.specs[0]
    router.mark_success(spec, 5, 7)
    router.mark_failure(spec, __import__("llm.pool", fromlist=["FailureKind"]).FailureKind.QUOTA, "没额度")
    assert router._states["m"].status is ModelStatus.REMOVED
    router.reset()
    st = router._states["m"]
    assert st.status is ModelStatus.OK
    assert st.calls == 1
    assert st.prompt_tokens == 5
    assert st.completion_tokens == 7
    again = ModelRouter([ModelSpec(name="m")], state_path=tmp_path / "pool.json")
    assert again._states["m"].prompt_tokens == 5

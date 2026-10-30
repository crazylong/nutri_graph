"""流式按句推送、模型记账和 Redis 适配。不连真 Redis。"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager

import pytest

import config
import kg.sensitive as sens

pytestmark = pytest.mark.no_db
from chatbus import MemoryBus, iter_sse, stream_limit_seconds
from graph.boundary import MODEL_UNAVAILABLE, PROVISIONAL_DISCLAIMER, WEAK_DISCLAIMER
from graph.live import Live
from graph.nodes import _complete_from_prompt, memory_write_node
from kg.sensitive import MEDICAL_FALLBACK, POLITICAL_ANSWER
from llm.pool import ModelSpec
from llm.streamctx import StreamCancelled, bind, mark_skip_save, skip_save


@pytest.fixture(autouse=True)
def _restore_sensitive_words():
    yield
    sens._words = None
    # 模型失败会在工作线程里打上「这一轮不落库」。用例之间清掉，免得后面的落库测试被跳过。
    from llm.streamctx import _hook, _nosave

    _nosave.set(False)
    _hook.set(None)


def _words():
    rows = [(sens.normalize(w), w, c, "block", 3) for w, c in sens._BUILTIN]
    rows.append((sens.normalize("最好"), "最好", "superlative", "record", 1))
    sens._words = rows


def _live(events, clock=None):
    _words()
    return Live(lambda event, data: events.append((event, data)) or True, clock=clock)


def _texts(events, name):
    return [data.get("text", "") for event, data in events if event == name]


def test_deleted_then_kept_sentence_does_not_show_fallback():
    events = []
    live = _live(events)
    assert live.feed("这个能治疗糖尿病。") is True
    assert live.feed("还能降血压。") is True
    assert live.feed("黑豆每100g含蛋白质36g。") is True
    final = live.finish()
    assert "蛋白质" in final
    assert MEDICAL_FALLBACK not in final
    assert MEDICAL_FALLBACK not in "".join(_texts(events, "text_delta"))
    assert "正在核对表述" in _texts(events, "status")
    assert final == live.screen


def test_newline_between_sentences_stays():
    events = []
    live = _live(events)
    live.feed("第一句。\n")
    live.feed("第二句。")
    assert "\n" in live.finish()


def test_retract_fragment_that_later_becomes_a_claim():
    events = []
    live = _live(events)
    live.feed("黑" * 80)
    assert live.sent_text
    live.feed("能治疗。")
    assert any(event == "answer_replace" and data.get("text") == "" for event, data in events)
    assert live.screen == ""


def test_negated_claim_and_superlative_stay():
    events = []
    live = _live(events)
    live.feed("这不能治疗。黑豆最好。")
    final = live.finish()
    assert "不能治疗" in final
    assert "最好" in final


def test_weak_note_prefixes_page_and_drops_when_body_is_gone():
    events = []
    live = _live(events)
    live.push_note(WEAK_DISCLAIMER)
    live.feed("这个能治疗。")
    assert live.finish() == WEAK_DISCLAIMER
    assert MEDICAL_FALLBACK not in live.screen
    live2_events = []
    live2 = _live(live2_events)
    live2.push_note(PROVISIONAL_DISCLAIMER)
    live2.feed("黑豆每100g含蛋白质36g。")
    final = live2.finish()
    assert final.startswith(PROVISIONAL_DISCLAIMER)
    assert "蛋白质" in final


def test_political_replaces_and_drops_the_note():
    events = []
    live = _live(events)
    live.push_note(WEAK_DISCLAIMER)
    assert live.feed("有人谈台独。后面还有。") is False
    assert live.finish() == POLITICAL_ANSWER
    assert WEAK_DISCLAIMER not in live.screen


def test_unavailable_is_a_normal_answer(monkeypatch):
    events = []
    live = _live(events)

    def boom(prompt, **kwargs):
        from llm.router import LLMUnavailableError

        raise LLMUnavailableError("empty")

    monkeypatch.setattr("llm.stream_model", boom)
    with bind(live):
        out = _complete_from_prompt("p", basis=["claim"], confidence=0.8)
    assert out["answer"] == MODEL_UNAVAILABLE
    assert out["basis"] == []
    assert not any(event == "error" for event, _data in events)
    assert live.screen == MODEL_UNAVAILABLE


def test_skip_save_and_session_fallback():
    from api.chat import chat_payload

    with bind(object()):
        mark_skip_save()
        assert skip_save()
        assert memory_write_node({"question": "q", "answer": "a", "user_id": "u"}) == {}
    assert chat_payload({"answer": "a"}, "sess")["session_id"] == "sess"
    assert chat_payload({"answer": "a", "session_id": "from-graph"}, "sess")["session_id"] == "from-graph"


def test_push_failure_does_not_skip_save():
    live = Live(lambda event, data: False)
    with bind(live):
        live.text_delta("hi")
        assert live.broken
        assert not skip_save()


class _Router:
    def __init__(self, names):
        self.names = names
        self.failures = []
        self.successes = []

    def candidates(self):
        return [ModelSpec.parse(name) for name in self.names]

    def spec(self, name):
        return ModelSpec.parse(name)

    def mark_failure(self, spec, kind, reason):
        self.failures.append(spec.name)

    def mark_success(self, spec, pin=0, pout=0):
        self.successes.append((spec.name, pin, pout))


class _Chunk:
    def __init__(self, content="", usage=None):
        self.content = content
        self.usage_metadata = usage


class _Model:
    def __init__(self, chunks, fail_at=None):
        self.chunks = chunks
        self.fail_at = fail_at
        self.calls = 0

    def stream(self, messages, stream_usage=True):
        assert stream_usage is True
        self.calls += 1
        for index, chunk in enumerate(self.chunks):
            if self.fail_at is not None and index >= self.fail_at:
                raise ConnectionError("down")
            yield chunk


def _patch_pool(monkeypatch, router, models):
    def chat_model(spec, **kwargs):
        return models[spec.name]

    monkeypatch.setattr("llm.middleware.get_router", lambda: router)
    monkeypatch.setattr("llm.middleware.chat_model", chat_model)
    monkeypatch.setattr("llm.middleware.in_pool", lambda name: True)
    monkeypatch.setattr("llm.middleware.fallback_spec", lambda: None)


def test_failure_before_text_switches_model(monkeypatch):
    from llm.agent import stream_model

    router = _Router(["a", "b"])
    models = {
        "a": _Model([_Chunk("不会发出去")], fail_at=0),
        "b": _Model([_Chunk("行")]),
    }
    _patch_pool(monkeypatch, router, models)
    assert stream_model("p") == "行"
    assert router.failures == ["a"]
    assert router.successes[0][0] == "b"


def test_failure_after_text_does_not_switch(monkeypatch):
    from llm.agent import stream_model
    from llm.streamctx import ModelStreamBroken

    router = _Router(["a", "b"])
    models = {
        "a": _Model([_Chunk("已经"), _Chunk("出了")], fail_at=1),
        "b": _Model([_Chunk("不该用到")]),
    }
    _patch_pool(monkeypatch, router, models)
    with pytest.raises(ModelStreamBroken):
        stream_model("p")
    assert router.failures == ["a"]
    assert router.successes == []
    assert models["b"].calls == 0


def test_cancel_does_not_mark_failure(monkeypatch):
    from llm.agent import stream_model

    router = _Router(["a"])
    models = {"a": _Model([_Chunk("一"), _Chunk("二")])}
    _patch_pool(monkeypatch, router, models)

    class Target:
        def __init__(self):
            self.n = 0

        def cancelled(self):
            return self.n > 0

        def feed(self, piece):
            self.n += 1
            return True

    target = Target()
    with bind(target):
        with pytest.raises(StreamCancelled):
            stream_model("p")
        assert skip_save()
    assert router.failures == []


def test_usage_keeps_the_last_nonempty(monkeypatch, caplog):
    from llm.agent import stream_model

    router = _Router(["a"])
    models = {
        "a": _Model([
            _Chunk("行"),
            _Chunk("", usage={"input_tokens": 3, "output_tokens": 4}),
            _Chunk(""),
        ]),
    }
    _patch_pool(monkeypatch, router, models)
    stream_model("p")
    assert router.successes[0][1:] == (3, 4)

    router2 = _Router(["b"])
    models2 = {"b": _Model([_Chunk("行")])}
    _patch_pool(monkeypatch, router2, models2)
    with caplog.at_level(logging.WARNING):
        stream_model("p")
    assert router2.successes[0][1:] == (0, 0)
    assert any("usage_metadata" in rec.message for rec in caplog.records)


def test_political_still_counts_success(monkeypatch):
    from llm.agent import stream_model

    _words()
    events = []
    live = _live(events)
    router = _Router(["a"])
    models = {"a": _Model([_Chunk("有人谈台独。"), _Chunk("后面。")])}
    _patch_pool(monkeypatch, router, models)
    with bind(live):
        text = stream_model("p")
    assert "台独" in text
    assert router.successes
    assert router.failures == []
    assert live.screen == POLITICAL_ANSWER


def test_stream_limit_uses_pool_size(monkeypatch):
    monkeypatch.setattr(config, "LLM_TIMEOUT", 10)

    class Router:
        def candidates(self):
            return [1, 2, 3]

    monkeypatch.setattr("llm.router.get_router", lambda: Router())
    monkeypatch.setattr("llm.router.fallback_spec", lambda: object())
    assert stream_limit_seconds() == 4 * 10 + 120


def test_list_stays_until_done_is_read(monkeypatch):
    from api.chat import _run_stream

    @contextmanager
    def _no_trace(*_args, **_kwargs):
        yield None

    class Agent:
        def invoke(self, state):
            return {"answer": "固定话术", "session_id": state.get("session_id")}

    monkeypatch.setattr("observability.tracing.trace", _no_trace)
    monkeypatch.setattr("graph.workflow.get_agent", lambda: Agent())
    bus = MemoryBus()
    thread = _run_stream(bus, "sid", {"question": "q", "user_id": "u"}, "q", "u", "sess")
    thread.join(2)
    assert bus.exists("sid")
    assert any(item["event"] == "done" for item in bus.events("sid"))
    assert any(item["data"].get("text") == "固定话术" for item in bus.events("sid"))

    async def read():
        frames = []
        async for frame in iter_sse(bus, "sid", time.monotonic() + 2):
            frames.append(frame)
        return frames

    frames = asyncio.run(read())
    assert not bus.exists("sid")
    assert any(frame.startswith("event: done") for frame in frames)


def test_graph_basis_is_the_claim(monkeypatch):
    from graph.nodes import generate_node

    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "")
    out = generate_node({
        "question": "黑豆",
        "_graph_results": [{"name": "黑豆", "etype": "n", "elevel": "A", "claim": "含蛋白质"}],
        "_vector_results": [],
        "_evidence_counts": {"A": 1},
        "_entities": [],
    })
    assert out["basis"] == ["含蛋白质"]
    assert out["confidence"] == 0.8

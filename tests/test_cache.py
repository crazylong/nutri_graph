"""缓存键含忌口哈希；命中后不再打加载函数；epoch 变了就换键。"""
from __future__ import annotations

import utils.cache as cache


class _Redis:
    def __init__(self):
        self.store = {}

    def ping(self):
        return True

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def incr(self, key):
        self.store[key] = int(self.store.get(key) or 0) + 1
        return self.store[key]


def _use(monkeypatch, client):
    monkeypatch.setattr("config.CACHE_ENABLED", True)
    cache.reset_client()
    monkeypatch.setattr(cache, "_connect", lambda: client)


def test_allergen_hash_differs():
    assert cache.allergen_hash(["虾", "牛奶"]) == cache.allergen_hash(["牛奶", "虾"])
    assert cache.allergen_hash(["虾"]) != cache.allergen_hash([])


def test_embedding_and_graph_hit_once(monkeypatch):
    fake = _Redis()
    _use(monkeypatch, fake)
    calls = {"n": 0}

    def load():
        calls["n"] += 1
        return [0.1, 0.2]

    assert cache.cached_embedding("m", "黑豆", load) == [0.1, 0.2]
    assert cache.cached_embedding("m", "黑豆", load) == [0.1, 0.2]
    assert calls["n"] == 1

    loads = {"n": 0}

    def rows():
        loads["n"] += 1
        return [{"cid": "a"}]

    key = cache.graph_key("pair", "ING_1", [], "guideline", True)
    assert cache.cached_json(key, 30, rows) == [{"cid": "a"}]
    assert cache.cached_json(key, 30, rows) == [{"cid": "a"}]
    assert loads["n"] == 1
    cache.bump_graph_epoch()
    key2 = cache.graph_key("pair", "ING_1", [], "guideline", True)
    assert key2 != key
    assert cache.cached_json(key2, 30, rows) == [{"cid": "a"}]
    assert loads["n"] == 2
    other = cache.graph_key("pair", "ING_1", ["虾"], "guideline", True)
    assert other != key2

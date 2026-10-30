"""食材切分与重录写图规则。不连库。"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.no_db

from graph.nodes import _eligible_terms, _mentions_from_model
from kg.food_admin import relation_method, relation_terms
from kg.normalize import LinkedEntity, LinkResult, fold_alias
from kg.textsim import char_similarity
from kg.merge import match_props_for_existing
from kg.segment import extract_foods, split_compound


def test_latin_case_is_the_same_alias():
    assert fold_alias("维C") == fold_alias("维c")
    assert char_similarity(fold_alias("维c"), fold_alias("维C")) == 1.0
    assert char_similarity("维c", "维C") == 0.5


def test_canonical_name_is_exact_and_other_spellings_are_aliases():
    assert relation_method("西红柿", "西红柿", "substring") == "exact"
    assert relation_method("番茄", "西红柿", "substring") == "alias"
    assert relation_method("西红", "西红柿", "fuzzy") == "fuzzy"


def test_space_separated_foods_stay_two_terms():
    assert relation_terms("胡萝卜 鸡蛋") == ["胡萝卜", "鸡蛋"]
    assert relation_terms("胡萝卜和鸡蛋") == ["胡萝卜", "鸡蛋"]
    assert relation_terms("胡萝卜，鸡蛋") == ["胡萝卜", "鸡蛋"]


def test_split_keeps_three_substrings():
    parts = split_compound("猪脚天麻当归", llm=lambda _text: ["猪脚", "天麻", "当归"])
    assert parts == ["猪脚", "天麻", "当归"]
    state = {"_unresolved": parts, "_split_terms": parts}
    assert _eligible_terms(state) == ["猪脚", "天麻", "当归"]


def test_split_drops_single_characters_and_non_substrings():
    assert split_compound("猪脚天麻当归", llm=lambda _text: ["天", "麻", "天麻粉"]) == []


def test_split_failure_returns_empty():
    def boom(_text):
        raise RuntimeError("down")

    assert split_compound("猪脚天麻当归", boom) == []


def test_split_timeout_returns_empty(monkeypatch):
    monkeypatch.setattr("kg.segment.SPLIT_TIMEOUT_SEC", 0.2)

    def slow(_text):
        time.sleep(0.5)
        return ["猪脚"]

    assert split_compound("猪脚天麻当归", slow) == []


HEADACHE = "天麻可以把头疼病治好吗？"


def test_model_sees_the_whole_question():
    seen = {}

    def llm(text):
        seen["q"] = text
        return ["天麻"]

    assert extract_foods(HEADACHE, llm=llm) == ["天麻"]
    assert seen["q"] == HEADACHE


def test_short_food_from_model_is_kept():
    assert extract_foods("黑豆怎么吃", llm=lambda _q: ["黑豆"]) == ["黑豆"]


def test_only_model_food_is_unresolved():
    class Linker:
        llm = staticmethod(lambda _q: ["天麻"])

        def link(self, term):
            assert term == "天麻"
            return LinkResult()

    _entities, unresolved, split_terms = _mentions_from_model(HEADACHE, [], Linker())
    assert unresolved == ["天麻"]
    assert split_terms == ["天麻"]
    assert "把头疼病治" not in unresolved


def test_model_miss_does_not_keep_the_sentence():
    class Linker:
        llm = staticmethod(lambda _q: [])

        def link(self, _term):
            raise AssertionError("模型没点名时不应该再拿残句去链接")

    entities, unresolved, split_terms = _mentions_from_model(HEADACHE, [], Linker())
    assert entities == []
    assert unresolved == []
    assert split_terms == []


def test_model_failure_keeps_dictionary_hits_only():
    known = LinkedEntity(
        canonical_id="pork",
        label="Ingredient",
        name="猪脚",
        score=1.0,
        method="substring",
        matched_alias="猪脚",
    )

    class Linker:
        def __init__(self):
            self.llm = self._boom

        def _boom(self, _question):
            raise RuntimeError("down")

        def link(self, _term):
            raise AssertionError("模型失败时不应该再拿原句去链接")

    entities, unresolved, split_terms = _mentions_from_model(
        "天麻跟猪脚煲汤合适吗", [known], Linker(),
    )
    assert entities == [known]
    assert unresolved == []
    assert split_terms == []


def test_known_food_from_model_is_linked_not_registered():
    class Linker:
        llm = staticmethod(lambda _q: ["猪脚", "天麻"])

        def link(self, term):
            result = LinkResult()
            if term == "猪脚":
                result.entities.append(LinkedEntity(
                    canonical_id="pork",
                    label="Ingredient",
                    name="猪脚",
                    score=1.0,
                    method="exact",
                    matched_alias="猪脚",
                ))
            return result

    entities, unresolved, split_terms = _mentions_from_model(
        "天麻跟猪脚煲汤合适吗", [], Linker(),
    )
    assert [item.canonical_id for item in entities] == ["pork"]
    assert unresolved == ["天麻"]
    assert split_terms == ["天麻"]


def test_other_unresolved_still_capped_at_two():
    state = {
        "_unresolved": ["甲甲", "乙乙", "丙丙", "猪脚", "天麻", "当归"],
        "_split_terms": ["猪脚", "天麻", "当归"],
    }
    assert _eligible_terms(state) == ["猪脚", "天麻", "当归", "甲甲", "乙乙"]


def test_existing_node_props_keep_provenance():
    props = {
        "canonical_name": "新名字",
        "category": "蔬菜",
        "summary": "一句",
        "provenance": "bundle:9 manual:1",
        "nature": "平",
    }
    plain = match_props_for_existing(props, re_record=False)
    assert "canonical_name" not in plain
    assert "provenance" not in plain
    assert plain["nature"] == "平"
    again = match_props_for_existing(props, re_record=True)
    assert again["canonical_name"] == "新名字"
    assert again["category"] == "蔬菜"
    assert again["summary"] == "一句"
    assert "provenance" not in again

"""食材切分与重录写图规则。不连库。"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.no_db

from graph.nodes import _eligible_terms, _split_unlinked
from kg.food_admin import relation_terms
from kg.merge import match_props_for_existing
from kg.segment import split_compound


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


def test_split_timeout_returns_empty():
    def slow(_text):
        time.sleep(4)
        return ["猪脚"]

    assert split_compound("猪脚天麻当归", slow) == []


def test_unlinked_short_term_is_not_split():
    assert _split_unlinked("黑豆", object()) is None


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

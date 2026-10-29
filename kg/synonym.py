"""同义模型。默认关闭，本期不调用模型，也不写库。"""
from __future__ import annotations

import logging

import config

log = logging.getLogger("kg.synonym")


def suggest(term: str, candidates: list | None = None) -> dict | None:
    """只返回候选。开关关闭或模型不可用时返回 None。"""
    if not config.TRIAGE_SYNONYM_ENABLED:
        return None
    log.info("同义模型开关开着，本期没有模型调用：%s", term)
    return None

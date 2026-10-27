"""
kg/extract.py —— LLM 抽取三元组（落到候选表，不直写主图）

设计要点：
  - 强制 JSON Schema 输出，pydantic 校验，结构不合法的直接丢弃
  - 每个三元组必须带 source_locator（原文定位），无引用 = 视为幻觉，丢弃
  - 抽取结果只落 PostgreSQL kg_candidate 表，人工确认后才进主图
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from kg.schema import EvidenceLevel, EvidenceType, RelType

log = logging.getLogger("kg.extract")

# LLM 抽取的 System Prompt
EXTRACT_SYSTEM = """你是食疗知识图谱的关系抽取专家。从给定的食材说明/膳食指南文本中，抽取「食材—关系—目标」三元组。

必须严格遵守：
1. 只抽取文本中明确出现的断言，禁止推理、禁止脑补。
2. 每个三元组必须能定位到原文，cite 填写原文中对应的那一句（逐字）。
3. 证据类型 evidence_type 只能取：
   guideline(指南/共识) / measured(实测/研究) / traditional(传统典籍) / anecdotal(民间说法/未验证)
4. 证据等级 evidence_level 只能取 A/B/C/D。
5. 关系类型 rel 只能取：
   CONTAINS_NUTRIENT / RECOMMEND_PAIR / INCOMPATIBLE_WITH / CONTRA_POPULATION / SUBSTITUTE_FOR / CONTRA_ITEM
6. 目标实体 dst 若是食材用 canonical_id(ING_xxx)，营养素用 NUT_xxx，人群用 POP_xxx，禁忌对象用 TAB_xxx。
   如果无法确定目标实体的 canonical_id，dst_id 填 "UNRESOLVED"，并在 dst_name 里写清名称。
7. 文本里没有关系时，输出空数组，不要硬凑。"""


class Triple(BaseModel):
    src_id: str = Field(..., description="源节点 canonical_id，如 ING_00001")
    rel: str = Field(..., description="关系类型")
    dst_id: str = Field(..., description="目标节点 canonical_id，无法确定填 UNRESOLVED")
    dst_name: str = Field(..., description="目标实体名称")
    claim_text: str = Field(..., description="断言原文")
    evidence_type: str = Field(..., description="guideline|measured|traditional|anecdotal")
    evidence_level: str = Field(..., description="A|B|C|D")
    cite: str = Field(..., description="原文中支撑该断言的句子，逐字摘录")


class ExtractResult(BaseModel):
    triples: list[Triple] = Field(default_factory=list)


class TripleExtractor:
    """用 LLM 抽取三元组。llm 是兼容 OpenAI 协议的客户端（DashScope）。"""

    def __init__(self, llm):
        self.llm = llm

    def _build_prompt(self, src_id: str, src_name: str, text: str) -> str:
        return (
            f"源食材：{src_name}（canonical_id = {src_id}）\n\n"
            f"以下是该食材的说明/指南文本：\n{text}\n\n"
            f"请抽取其中的关系三元组，以 JSON 返回，格式："
            '{"triples": [{"src_id":"...","rel":"...","dst_id":"...","dst_name":"...",'
            '"claim_text":"...","evidence_type":"...","evidence_level":"...","cite":"..."}]}'
        )

    def extract(self, src_id: str, src_name: str, text: str) -> list[dict[str, Any]]:
        prompt = self._build_prompt(src_id, src_name, text)
        resp = self.llm.invoke(prompt)
        content = getattr(resp, "content", resp) if not isinstance(resp, str) else resp
        return self._parse(content)

    def _parse(self, content: str) -> list[dict[str, Any]]:
        # 剥离可能的 markdown 代码块包裹
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            log.warning("LLM 输出无 JSON，丢弃：%s", content[:80])
            return []
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            log.warning("JSON 解析失败，丢弃：%s", e)
            return []
        try:
            result = ExtractResult.model_validate(obj)
        except ValidationError as e:
            log.warning("Schema 校验失败，丢弃：%s", e)
            return []

        out = []
        for t in result.triples:
            # 引用必填校验：无引用 = 幻觉
            if not t.cite.strip():
                log.warning("三元组无 cite，丢弃：%s -> %s", t.src_id, t.dst_name)
                continue
            # 关系类型 / 证据枚举合法性（pydantic 只校验了字符串，这里再兜底）
            try:
                RelType(t.rel)
                EvidenceType(t.evidence_type)
                EvidenceLevel(t.evidence_level)
            except ValueError:
                log.warning("枚举非法，丢弃：rel=%s type=%s level=%s", t.rel, t.evidence_type, t.evidence_level)
                continue
            out.append(t.model_dump())
        return out

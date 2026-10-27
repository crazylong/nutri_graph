"""
eval/metrics.py —— Ragas 评估指标（含自定义弃答指标）

参考 gme_rag/Multimodal_RAG/evaluate/evaluate_self.py 的封装方式：
  LangchainLLMWrapper / LangchainEmbeddingsWrapper 包装 DashScope 客户端。

核心指标：
  Faithfulness（第一指标，对应「不臆造」）
  ResponseRelevancy
  LLMContextPrecision
  AbstentionCorrectness（自定义，该拒答时是否拒答）
  UncataloguedHandling（自定义，未收录食材是否诚实登记且不编造）

ragas 版本注意：0.4.x 的自定义指标 API 和旧版（0.2.x）完全不同 ——
  评分入口是 `_single_turn_ascore(self, sample, callbacks)`（不再是 `_single_score(row)`），
  列声明是 `_required_columns = {SingleTurnSample: {"user_input", ...}}`（不再是字符串字典）。
"""
from __future__ import annotations

import re
from typing import Any

from ragas import SingleTurnSample, EvaluationDataset
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import (
    Faithfulness,
    ResponseRelevancy,
    LLMContextPrecisionWithoutReference,
)
from ragas.metrics.base import Metric, MetricWithLLM, SingleTurnMetric


class AbstentionCorrectness(SingleTurnMetric):
    """自定义指标：该弃答的问题是否真的弃答（而非给推测性建议）。

    reference = "ABSTAIN" 表示该问题在图谱中无依据，正确答案是拒答。
    分数 = 1 表示正确拒答，0 表示错误地给出了具体建议。
    纯规则判定，不调 LLM，可复现。
    """

    name = "abstention_correctness"
    _required_columns = {
        SingleTurnSample: {"user_input", "response", "reference"},
    }

    def init(self, run_config) -> None:
        pass  # 纯规则，无需初始化

    async def _single_turn_ascore(self, sample: SingleTurnSample, callbacks) -> float:
        response = sample.response or ""
        reference = (sample.reference or "").strip().upper()

        if reference != "ABSTAIN":
            return 1.0  # 非弃答题，不评分

        # 弃答话术有多种变体，共同点是「拒绝给出具体建议」。
        # 关键：不能用「没有确切依据」这种长串去匹配，因为实际话术是
        # 「没有**能回答这个问题的**确切依据」——中间插了字，长串匹配会漏。
        # 「确切依据」这个词本身只出现在弃答语境里，是最可靠的信号。
        abstain_markers = [
            "确切依据",          # 覆盖「没有…确切依据」各种插字变体
            "无法回答", "无法给出", "无法确定",
            "未提供", "未涉及", "未找到", "未提及", "未经验证",
            "不能回答", "不能据此",
        ]
        abstained = any(m in response for m in abstain_markers)
        return 1.0 if abstained else 0.0


class UncataloguedHandling(SingleTurnMetric):
    """自定义指标：图谱里没有的食材，回答是否「诚实登记且不编造」。

    reference = "UNCATALOGUED" 表示该食材不在图谱里，正确行为是：
      ① 明确说暂未收录 / 已记录
      ② 不编造任何营养素数值
      ③ 不做功能/疗效断言

    三个子项各 0/1，取平均。全部用确定性规则，不花 LLM 调用。
    """

    name = "uncatalogued_handling"
    _required_columns = {
        SingleTurnSample: {"user_input", "response", "reference"},
    }

    def init(self, run_config) -> None:
        pass  # 纯规则，无需初始化

    # ① 承认未收录
    _ACK_MARKERS = [
        "暂未收录", "尚未收录", "没有收录", "未收录", "知识库中没有",
        "知识库里没有", "已记录", "已反馈", "已提交",
    ]
    # ② 编造营养数值：出现「数字 + 营养单位」即判失败
    _NUTRIENT_RE = re.compile(
        r"\d+(?:\.\d+)?\s*(?:g|mg|μg|ug|kcal|千卡|大卡|千焦|kJ|微克|毫克|克)\b",
        re.IGNORECASE,
    )
    # ③ 医疗/夸大断言
    _OVERCLAIM_MARKERS = [
        "治疗", "主治", "疗效", "根治", "降血糖", "降血压", "抗癌",
        "排毒", "壮阳", "包治", "特效",
    ]

    async def _single_turn_ascore(self, sample: SingleTurnSample, callbacks) -> float:
        reference = (sample.reference or "").strip().upper()
        if reference != "UNCATALOGUED":
            return 1.0  # 非本题型，不评分

        response = sample.response or ""
        if not response.strip():
            return 0.0  # 空回答等于什么都没做对

        ack = 1.0 if any(m in response for m in self._ACK_MARKERS) else 0.0
        no_numbers = 0.0 if self._NUTRIENT_RE.search(response) else 1.0
        no_overclaim = 0.0 if any(m in response for m in self._OVERCLAIM_MARKERS) else 1.0
        return (ack + no_numbers + no_overclaim) / 3


def build_metrics(evaluator_llm, evaluator_embeddings) -> list[Metric]:
    """构造评估指标列表。

    bypass_n=True 是必须的：ragas 检测到 ChatOpenAI「支持多候选生成」后，
    会用 generate_prompt(n=n) 把 n 参数发给模型，而 DashScope 兼容端点只接受 n=1，
    会报 400 Invalid n value。关掉后 ragas 改走 generate_prompt(prompts=[prompt]*n)，
    逐个生成，绕开这个不兼容。
    """
    llm = LangchainLLMWrapper(evaluator_llm, bypass_n=True)
    emb = LangchainEmbeddingsWrapper(evaluator_embeddings)
    return [
        Faithfulness(llm=llm),
        ResponseRelevancy(llm=llm, embeddings=emb),
        LLMContextPrecisionWithoutReference(llm=llm),
        AbstentionCorrectness(),      # 纯规则，不调 LLM
        UncataloguedHandling(),       # 纯规则，不调 LLM
    ]


def adapt_chinese(metrics: list[Metric]) -> list[Metric]:
    """中文适配：旧版 ragas 用 set_prompt_language，0.4.x 删了。

    ragas 0.4.x 的 adapt_prompts(language, llm) 会**额外调 LLM 去翻译 prompt**，
    既花钱又引入不可复现性 —— 对「出一版基线数字」来说没必要，
    评估对象（response + context）本身是中文，英文 prompt 也能评，qwen 中英都通。
    所以这里只做兼容：有旧方法就用（免费），没有就跳过。
    """
    for m in metrics:
        if hasattr(m, "set_prompt_language"):
            m.set_prompt_language("chinese")
    return metrics


def make_dataset(rows: list[dict[str, Any]]) -> EvaluationDataset:
    """从样本构造评估数据集。

    rows: [{user_input, retrieved_contexts, response, reference}]
    reference 为 "ABSTAIN" 表示该题应弃答。
    """
    samples = [
        SingleTurnSample(
            user_input=r["user_input"],
            retrieved_contexts=r.get("retrieved_contexts", []),
            response=r["response"],
            reference=r.get("reference"),
        )
        for r in rows
    ]
    return EvaluationDataset(samples=samples)

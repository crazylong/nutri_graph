"""
kg/usda.py —— USDA FoodData Central 客户端

为什么选它做在线主源（对比中国食物成分表在线平台）：
  * 有正式 API、免费的 key、公有领域（CC0），可合法自动化调用
  * Foundation / SR Legacy 的数值就是**每 100g**，正好是 CONTAINS_NUTRIENT 的口径
  * 中国食物成分表查询平台没有公开 API，只能抓 HTML，且数据不可商用 ——
    那是「离线批量导入」的活，不该放进请求链路

两个端点返回结构**不一样**，这是实测踩出来的：
  /foods/search   foodNutrients 是扁平结构 {nutrientId, nutrientName, nutrientNumber, value}
                  foodCategory 是**字符串**
  /food/{fdcId}   foodNutrients 是嵌套结构 {nutrient: {id, number, name, unitName}, amount}
                  foodCategory 是**对象** {id, code, description}

匹配营养素必须用 `nutrient.id`（1008/1003/… 新编号），
不能用 `nutrientNumber` —— 那是老的 ndb 编号（能量是 208，不是 1008）。
"""
from __future__ import annotations

import logging
from typing import Any

import config

log = logging.getLogger("kg.usda")

# nutrient.id → (内部字段名, 中文名, 单位)
NUTRIENTS: dict[int, tuple[str, str, str]] = {
    1008: ("energy_kcal", "能量", "kcal"),
    1062: ("energy_kj", "能量", "kJ"),
    1003: ("protein", "蛋白质", "g"),
    1004: ("fat", "脂肪", "g"),
    1005: ("carb", "碳水化合物", "g"),
    1079: ("fiber", "膳食纤维", "g"),
    1093: ("sodium", "钠", "mg"),
}


class UsdaError(RuntimeError):
    """调用失败。区分「可重试」与「不可重试」，别让限流把任务永久打死。"""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class UsdaClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ):
        self.api_key = api_key or config.FDC_API_KEY
        self.base_url = (base_url or config.FDC_BASE_URL).rstrip("/")
        self.timeout = timeout or config.FDC_TIMEOUT

    def _get(self, path: str, params: dict[str, Any]) -> dict:
        import httpx

        params = {**params, "api_key": self.api_key}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{self.base_url}{path}", params=params)
        except Exception as exc:  # noqa: BLE001 —— 网络问题可重试
            raise UsdaError(f"请求失败：{exc}", retryable=True) from exc

        if resp.status_code == 429:
            # DEMO_KEY 是 30/小时、50/天，很容易撞到；这是可重试但要等
            raise UsdaError("触发 USDS 限流（429）", retryable=True)
        if resp.status_code == 404:
            raise UsdaError("资源不存在（404）", retryable=False)
        if resp.status_code >= 400:
            raise UsdaError(f"HTTP {resp.status_code}: {resp.text[:200]}", retryable=False)
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise UsdaError(f"响应不是合法 JSON：{exc}", retryable=False) from exc

    def search(self, query: str, page_size: int = 8) -> list[dict]:
        """搜候选。只取 Foundation 与 SR Legacy —— Branded 是厂商自填，不可信。"""
        data = self._get(
            "/foods/search",
            {
                "query": query,
                "dataType": config.FDC_DATA_TYPES,
                "pageSize": page_size,
            },
        )
        return data.get("foods", []) or []

    def get_food(self, fdc_id: int | str) -> dict:
        return self._get(f"/food/{fdc_id}", {})


def parse_nutrients(detail: dict) -> list[dict]:
    """从详情响应里取我们关心的营养素（每 100g）。"""
    out: list[dict] = []
    for n in detail.get("foodNutrients", []) or []:
        nutrient = n.get("nutrient") or {}
        nid = nutrient.get("id")
        if nid not in NUTRIENTS:
            continue
        key, name, unit = NUTRIENTS[nid]
        amount = n.get("amount")
        if amount is None:
            continue
        out.append(
            {
                "key": key,
                "name": name,
                "amount": float(amount),
                "unit": nutrient.get("unitName") or unit,
                "nutrient_id": nid,
            }
        )
    # 固定顺序，便于比对与展示
    order = {k: i for i, k in enumerate(NUTRIENTS)}
    out.sort(key=lambda x: order.get(x["nutrient_id"], 99))
    return out


def category_description(food: dict | str | None) -> str:
    """两个端点的 foodCategory 一个是对象一个是字符串，统一成字符串。"""
    if isinstance(food, dict):
        cat = food.get("foodCategory")
    else:
        cat = food
    if isinstance(cat, dict):
        return str(cat.get("description") or "")
    return str(cat or "")

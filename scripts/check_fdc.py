"""
scripts/check_fdc.py —— 检查 USDA FoodData Central 的 API key 有没有配好

为什么单独做一个命令：这个接口的所有错误都长得很像，但原因完全不同 ——

  API_KEY_MISSING   压根没给 key（浏览器直接打开 /fdc/v1 就是这个，因为没带参数）
  API_KEY_INVALID   key 拼错了 / 已被停用（官方会主动停用公开泄露的 key）
  429 OVER_RATE_LIMIT  配了但超额（DEMO_KEY 只有 30 次/小时、50 次/天，全站共享）

用 curl 试很容易把 429 看成"key 不对"，然后反复去重新申请。
这里一次性把状态、剩余额度、实际命中的条目都打出来。

用法：
  python -m scripts.check_fdc
  python -m scripts.check_fdc --query "soybeans mature seeds raw"
"""
from __future__ import annotations

import argparse
import sys

import httpx

import config

SIGNUP_URL = "https://fdc.nal.usda.gov/api-key-signup"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="soybeans mature seeds raw")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    key = (config.FDC_API_KEY or "").strip()
    print(f"key      : {'(空)' if not key else (key[:6] + '…' + key[-4:] if len(key) > 12 else key)}"
          f"  (长度 {len(key)})")
    print(f"endpoint : {config.FDC_BASE_URL}/foods/search")

    if not key:
        print("\n❌ 没有配置 FDC_API_KEY")
        print(f"   去 {SIGNUP_URL} 申请（First name / Last name / Email 三项，")
        print("   key 会发到邮箱，40 个字符）")
        return 2

    if key == "DEMO_KEY":
        print("\n⚠️ 用的是 DEMO_KEY：30 次/小时、50 次/天，而且是**所有使用者共享**这一个桶。")
        print(f"   自己做东西请去 {SIGNUP_URL} 申请，免费，1000 次/小时。")

    url = f"{config.FDC_BASE_URL.rstrip('/')}/foods/search"
    try:
        with httpx.Client(timeout=args.timeout) as c:
            r = c.get(
                url,
                params={
                    "query": args.query,
                    "dataType": config.FDC_DATA_TYPES,
                    "pageSize": 3,
                    "api_key": key,
                },
            )
    except httpx.HTTPError as exc:
        print(f"\n❌ 网络不通：{type(exc).__name__}: {exc}")
        return 2

    limit = r.headers.get("X-RateLimit-Limit")
    remain = r.headers.get("X-RateLimit-Remaining")
    if limit or remain:
        print(f"quota    : {remain}/{limit} 剩余（本小时）")

    if r.status_code == 200:
        foods = r.json().get("foods", [])
        print(f"\n✅ key 可用。命中 {len(foods)} 条：")
        for f in foods:
            print(f"   {f.get('fdcId')}  [{f.get('dataType')}]  {f.get('description')}")
        return 0

    body = r.text[:200].replace("\n", " ")
    print(f"\n❌ HTTP {r.status_code}  {body}")

    if "API_KEY_MISSING" in r.text:
        print("   → 没带 api_key。浏览器直接打开 API 根路径就会这样，不是服务坏了。")
        print(f"     申请：{SIGNUP_URL}")
    elif "API_KEY_INVALID" in r.text:
        print("   → key 无效或已被停用（官方会主动停掉公开泄露的 key）。重新申请一个。")
        print(f"     {SIGNUP_URL}")
    elif r.status_code == 429:
        print("   → 超额。DEMO_KEY 是 30 次/小时且全站共享，等一小时或换自己的 key：")
        print(f"     {SIGNUP_URL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

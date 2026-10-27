"""
scripts/auth_smoke.py —— 手机号验证码登录全链路自检

两种跑法：

  A. 进程内（不占端口，CI 友好；PG 必须可达）
       DEBUG=true python -m scripts.auth_smoke --in-process

  B. 打真实服务（先起 uvicorn）
       SMS_PROVIDER=console DEBUG=true uvicorn app:app --port 8899
       python -m scripts.auth_smoke --base-url http://127.0.0.1:8899

覆盖：
  1. 发验证码              2. 验证码换令牌
  3. 带令牌取用户信息      4. 无令牌 / 坏令牌 401
  5. refresh 旋转          6. **旧 refresh 重放 → 整族吊销**
  7. 手机号格式校验        8. 同号重发间隔频控
  9. 验证码连错 5 次锁定   10. 注销清理
"""
from __future__ import annotations

import argparse
import sys

import httpx

PASS, FAIL = "✅ PASS", "❌ FAIL"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  → {detail}" if detail else ""))
    return ok


def _make_client(base_url: str, in_process: bool):
    if not in_process:
        return httpx.Client(base_url=base_url, timeout=30.0), False
    from fastapi.testclient import TestClient

    from app import app  # 延迟导入：先让 --help 不依赖项目依赖

    return TestClient(app), True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8899")
    ap.add_argument("--in-process", action="store_true", help="不起服务，直接跑 ASGI app")
    ap.add_argument("--phone", default="13800138000", help="主流程用的手机号")
    ap.add_argument("--phone2", default="13900139000", help="锁定测试用的手机号")
    args = ap.parse_args()

    c, in_proc = _make_client(args.base_url, args.in_process)

    # ---------- 0. 服务可达 ----------
    try:
        c.get("/health")
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL}  服务不可达 {args.base_url}：{exc}")
        return 2

    # ---------- 1. 发验证码 ----------
    r = c.post("/auth/sms", json={"phone": args.phone, "device_fp": "smoke-dev-1"})
    ok = r.status_code == 200 and r.json().get("ok")
    dev_code = r.json().get("dev_code", "") if ok else ""
    check("发送验证码", ok, f"{r.status_code} provider={r.json().get('provider') if ok else r.text[:120]}")
    if not ok or not dev_code:
        print("   拿不到 dev_code：确认 SMS_PROVIDER=console 且 DEBUG=true")
        return 2

    # ---------- 2. 手机号格式 ----------
    r = c.post("/auth/sms", json={"phone": "12345"})
    check("手机号格式校验（非法号 422）", r.status_code == 422, str(r.status_code))

    # ---------- 3. 同号重复发送 ----------
    r = c.post("/auth/sms", json={"phone": args.phone})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check(
        "同号 60s 内重发被频控（429）",
        r.status_code == 429 and body.get("detail", {}).get("code") == "too_frequent",
        f"{r.status_code} retry-after={r.headers.get('retry-after')}",
    )

    # ---------- 4. 验证码换令牌 ----------
    r = c.post("/auth/token", json={"phone": args.phone, "code": dev_code})
    ok = r.status_code == 200
    tok = r.json() if ok else {}
    check("验证码换令牌", ok and "access_token" in tok and "refresh_token" in tok, str(r.status_code))
    if not ok:
        print("   ", r.text[:300])
        return 1
    access, refresh = tok["access_token"], tok["refresh_token"]
    check("令牌语义对齐 OAuth2", tok.get("token_type") == "bearer" and tok.get("expires_in") == 1800,
          f"token_type={tok.get('token_type')} expires_in={tok.get('expires_in')}")

    # ---------- 5. 取当前用户 ----------
    r = c.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    me = r.json() if r.status_code == 200 else {}
    check("带令牌取用户信息", r.status_code == 200 and me.get("phone_mask", "").count("*") == 4,
          f"{r.status_code} phone_mask={me.get('phone_mask')}")

    # ---------- 6. 未授权 ----------
    r = c.get("/auth/me")
    check("无令牌 401", r.status_code == 401, str(r.status_code))
    r = c.get("/auth/me", headers={"Authorization": "Bearer not-a-token"})
    check("坏令牌 401", r.status_code == 401, str(r.status_code))

    # ---------- 7. refresh 旋转 ----------
    r = c.post("/auth/refresh", json={"refresh_token": refresh})
    ok = r.status_code == 200
    rotated = r.json() if ok else {}
    check("refresh 换新令牌", ok and rotated.get("refresh_token") != refresh, str(r.status_code))

    # ---------- 8. 旧 refresh 重放 → 整族吊销 ----------
    r = c.post("/auth/refresh", json={"refresh_token": refresh})
    code = r.json().get("detail", {}).get("code") if r.status_code == 401 else ""
    check("旧 refresh 重放被拒（401 token_reuse）", r.status_code == 401 and code == "token_reuse",
          f"{r.status_code} code={code}")

    # 整族吊销后，刚换到的那条也不能再用
    r = c.post("/auth/refresh", json={"refresh_token": rotated.get("refresh_token", "")})
    check("重放后整族吊销（新 refresh 一起失效）", r.status_code == 401, str(r.status_code))

    # ---------- 9. 验证码连错锁定 ----------
    r = c.post("/auth/sms", json={"phone": args.phone2, "device_fp": "smoke-dev-2"})
    ok2 = r.status_code == 200
    check("第二个手机号发码成功", ok2, str(r.status_code))
    statuses = []
    for _ in range(5):
        rr = c.post("/auth/token", json={"phone": args.phone2, "code": "000000"})
        statuses.append(rr.status_code)
    check("连续 5 次错误验证码 → 401", statuses == [401] * 5, str(statuses))
    rr = c.post("/auth/token", json={"phone": args.phone2, "code": "000000"})
    code = rr.json().get("detail", {}).get("code") if rr.status_code == 429 else ""
    check("第 6 次被锁定（429 locked）", rr.status_code == 429 and code == "locked",
          f"{rr.status_code} code={code} retry-after={rr.headers.get('retry-after')}")

    # ---------- 10. 注销 ----------
    # 用第 7 步换到的 access（JWT 无状态，refresh 被吊销不影响它），避免再等 60s 间隔
    acc = rotated.get("access_token", "")
    r = c.delete("/auth/me", headers={"Authorization": f"Bearer {acc}"})
    check("注销账号（清 PII + 吊销令牌）", r.status_code == 200, str(r.status_code))
    r = c.get("/auth/me", headers={"Authorization": f"Bearer {acc}"})
    check("注销后旧 access 失效", r.status_code == 401, str(r.status_code))

    # ---------- 汇总 ----------
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    mode = "进程内(ASGI)" if in_proc else args.base_url
    print(f"\n{'=' * 56}\n通过 {passed}/{total}    运行方式：{mode}")
    failed = [n for n, ok, _ in _results if not ok]
    if failed:
        print("失败项：" + "、".join(failed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

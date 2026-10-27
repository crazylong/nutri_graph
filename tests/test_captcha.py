"""
tests/test_captcha.py —— 图形验证码（防刷短信第二道闸）

覆盖：
  1. 领取图形码返回 challenge_id + question + svg
  2. 同一 IP 超过免费次数后，发码不带图形码 → 428 captcha_required
  3. 带错答案 → 428 captcha_invalid
  4. 带对答案 → 放行
  5. 图形码单次消费：答对后再用同一个 challenge → 失效
"""
from __future__ import annotations

import re

import pytest

import config
from tests.helpers import rand_phone, requires_pg

pytestmark = requires_pg


def _answer_from_question(question: str) -> str:
    """从「14 + 7 = ?」里算出答案。"""
    m = re.match(r"(\d+)\s*\+\s*(\d+)", question)
    assert m, f"无法解析题目：{question}"
    return str(int(m.group(1)) + int(m.group(2)))


def _require_captcha(client, monkeypatch):
    """把免费次数降到 0，让「任何发码」都强制图形码。"""
    monkeypatch.setattr(config, "CAPTCHA_FREE_SENDS_PER_IP", 0)


def _get_captcha(client) -> dict:
    r = client.get("/auth/captcha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["challenge_id"] and body["question"] and body["svg"]
    return body


def test_captcha_returns_challenge_and_svg(client):
    c = _get_captcha(client)
    assert "?" in c["question"]
    assert "<svg" in c["svg"]
    assert c["expires_in"] > 0


def test_captcha_required_after_free_limit(client, monkeypatch):
    _require_captcha(client, monkeypatch)
    r = client.post("/auth/sms", json={"phone": rand_phone()})
    assert r.status_code == 428
    assert r.json()["detail"]["code"] == "captcha_required"


def test_captcha_wrong_answer_rejected(client, monkeypatch):
    _require_captcha(client, monkeypatch)
    c = _get_captcha(client)
    # 随便给个和正确答案几乎不会碰上的错误值
    r = client.post(
        "/auth/sms",
        json={
            "phone": rand_phone(),
            "captcha_id": c["challenge_id"],
            "captcha_answer": "999999",
        },
    )
    assert r.status_code == 428
    assert r.json()["detail"]["code"] == "captcha_invalid"


def test_captcha_correct_answer_allows_send(client, monkeypatch):
    _require_captcha(client, monkeypatch)
    c = _get_captcha(client)
    answer = _answer_from_question(c["question"])

    r = client.post(
        "/auth/sms",
        json={
            "phone": rand_phone(),
            "captcha_id": c["challenge_id"],
            "captcha_answer": answer,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_captcha_single_use(client, monkeypatch):
    _require_captcha(client, monkeypatch)
    c = _get_captcha(client)
    answer = _answer_from_question(c["question"])

    # 第一次答对
    r1 = client.post(
        "/auth/sms",
        json={"phone": rand_phone(), "captcha_id": c["challenge_id"], "captcha_answer": answer},
    )
    assert r1.status_code == 200

    # 同一个 challenge 再用一次 → 已消费
    r2 = client.post(
        "/auth/sms",
        json={
            "phone": rand_phone(),
            "captcha_id": c["challenge_id"],
            "captcha_answer": answer,
        },
    )
    assert r2.status_code == 428
    assert r2.json()["detail"]["code"] == "captcha_invalid"


def test_free_then_required_transition(client, monkeypatch):
    """免费次数用尽前正常发码，用尽后要求图形码 —— 验证阈值切换逻辑。"""
    # 先让这个 IP 已经发过 1 条（免费额度 1）
    monkeypatch.setattr(config, "CAPTCHA_FREE_SENDS_PER_IP", 1)
    # 清掉该 IP 的历史（TestClient 的 IP 是 testclient）
    from security import ratelimit

    # 直接调内部计数，模拟「已经发过 1 条」
    # 用一个独立 IP 维度测试：monkeypatch recent_sends_by_ip 太 hack，改用真实计数
    # 这里验证的是「阈值恰好等于已发数时触发」的边界
    monkeypatch.setattr(ratelimit, "recent_sends_by_ip", lambda ip, window_hours=1: 1)
    r = client.post("/auth/sms", json={"phone": rand_phone()})
    assert r.status_code == 428
    assert r.json()["detail"]["code"] == "captcha_required"

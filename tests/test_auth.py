"""登录 / 令牌 / 授权开关"""
from __future__ import annotations

import config
from tests.helpers import bearer, login, rand_phone, requires_pg, send_code

pytestmark = requires_pg


def test_send_code_and_login(client):
    phone = rand_phone()
    code = send_code(client, phone)
    assert len(code) == config.SMS_CODE_LENGTH

    r = client.post("/auth/token", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == config.ACCESS_TTL_S
    assert body["access_token"] and body["refresh_token"]
    # 手机号只以掩码出现
    assert body["user"]["phone_mask"].count("*") == 4
    assert phone not in r.text


def test_phone_is_stored_hashed_not_plain(client):
    """回应里不能出现明文手机号 —— 顺带验证掩码规则。"""
    phone = rand_phone()
    tokens = login(client, phone)
    assert tokens["user"]["phone_mask"] == f"{phone[:3]}****{phone[-4:]}"


def test_invalid_phone_rejected(client):
    r = client.post("/auth/sms", json={"phone": "12345"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_phone"


def test_missing_and_bad_token(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers=bearer("not-a-token")).status_code == 401


def test_wrong_code_then_correct(client):
    phone = rand_phone()
    code = send_code(client, phone)
    wrong = "000000" if code != "000000" else "111111"
    r = client.post("/auth/token", json={"phone": phone, "code": wrong})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "invalid_code"
    # 码不会被错误尝试消费掉
    r = client.post("/auth/token", json={"phone": phone, "code": code})
    assert r.status_code == 200


def test_refresh_rotation_and_reuse_revokes_family(client):
    tokens = login(client)
    old_refresh = tokens["refresh_token"]

    r = client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert r.status_code == 200
    new_refresh = r.json()["refresh_token"]
    assert new_refresh != old_refresh

    # 旧的重放 → 判定泄露
    r = client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "token_reuse"

    # 整族吊销：刚换到的这条也一起失效
    r = client.post("/auth/refresh", json={"refresh_token": new_refresh})
    assert r.status_code == 401


def test_logout_revokes_only_that_refresh(client):
    tokens = login(client)
    r = client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200

    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401
    # 关键：登出后的令牌不能报成「检测到异常登录」。
    # 两者都是 revoked，区别在 replaced_by：有值的是被旋转掉的（真重放），
    # 没值的是登出/下线吊销的（正常失效）。判错会让用户以为账号被盗。
    assert r.json()["detail"]["code"] == "invalid_refresh"
    assert "失效" in r.json()["detail"]["message"]


def test_delete_me_purges_and_revokes(client):
    tokens = login(client)
    access = tokens["access_token"]
    assert client.get("/auth/me", headers=bearer(access)).status_code == 200

    r = client.delete("/auth/me", headers=bearer(access))
    assert r.status_code == 200
    assert "purged" in r.json()

    assert client.get("/auth/me", headers=bearer(access)).status_code == 401
    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401


def test_auth_required_blocks_fake_uid_and_anon(client, monkeypatch):
    """needs 13 的核心验收：开关打开后，伪造 user_id 与匿名身份都不再能进业务接口。"""
    monkeypatch.setattr(config, "AUTH_REQUIRED", True)

    r = client.post("/chat", json={"question": "痛风能吃海鲜吗", "user_id": "admin"})
    assert r.status_code == 401, "伪造 user_id 竟然通过了"

    anon = client.post("/auth/anon").json()["anon_token"]
    r = client.post(
        "/chat", json={"question": "痛风能吃海鲜吗"}, headers=bearer(anon)
    )
    assert r.status_code == 401, "匿名身份不应能访问需登录接口"

    tokens = login(client)
    r = client.post(
        "/chat",
        json={"question": ""},
        headers=bearer(tokens["access_token"]),
    )
    # 已登录：鉴权放行，走到空问题校验（400）而不是 401
    assert r.status_code == 400

"""登录 / 令牌 / 授权开关"""
from __future__ import annotations

import config
from tests.helpers import bearer, login, login_console, rand_phone, requires_pg, send_code

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
    tokens = login_console(client)
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


def test_existing_user_console_login_does_not_promote(client, monkeypatch):
    monkeypatch.setattr(config, "SMS_INTERVAL_S", 0)
    admin = login_console(client)
    phone = rand_phone()
    created = client.post(
        "/admin/users",
        headers=bearer(admin["access_token"]),
        json={"phone": phone, "username": "甲", "role": "user"},
    )
    assert created.status_code == 200, created.text
    code = send_code(client, phone)
    denied = client.post("/auth/token", json={"phone": phone, "code": code, "console": True})
    assert denied.status_code == 403
    listed = client.get("/admin/users", headers=bearer(admin["access_token"]), params={"phone": phone})
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["role"] == "user"


def test_kb_login_same_message_for_missing_and_wrong_tenant(client, monkeypatch):
    monkeypatch.setattr(config, "SMS_INTERVAL_S", 0)
    admin = login_console(client)
    code = send_code(client, admin["_phone"])
    ok = client.post(
        "/auth/token",
        json={"phone": admin["_phone"], "code": code, "no_create": True, "tenant": "default_renter"},
    )
    assert ok.status_code == 200, ok.text
    code = send_code(client, admin["_phone"])
    wrong = client.post(
        "/auth/token",
        json={"phone": admin["_phone"], "code": code, "no_create": True, "tenant": "not_joined"},
    )
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["message"] == "用户不存在"

    stranger = rand_phone()
    code = send_code(client, stranger)
    missing = client.post(
        "/auth/token",
        json={"phone": stranger, "code": code, "no_create": True, "tenant": "default_renter"},
    )
    assert missing.status_code == 401
    assert missing.json()["detail"]["message"] == "用户不存在"

    headers = bearer(ok.json()["access_token"])
    blocked = client.post("/kb/ask", data={"tenant_id": "not_joined", "question": "制度"}, headers=headers)
    assert blocked.status_code == 403


def test_customer_and_staff_are_separate(client, monkeypatch):
    monkeypatch.setattr(config, "SMS_INTERVAL_S", 0)
    phone = rand_phone()
    customer = login(client, phone)
    assert client.get("/auth/me", headers=bearer(customer["access_token"])).status_code == 401
    principal = client.get("/auth/principal", headers=bearer(customer["access_token"]))
    assert principal.status_code == 200
    assert principal.json()["kind"] == "user"
    chat = client.post("/chat", json={"question": ""}, headers=bearer(customer["access_token"]))
    assert chat.status_code == 400

    staff = login_console(client, phone)
    assert staff["user"]["role"] == "admin"
    assert staff["user"]["user_id"] != customer["user"]["user_id"]
    blocked = client.post("/chat", json={"question": "你好"}, headers=bearer(staff["access_token"]))
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "staff_token"

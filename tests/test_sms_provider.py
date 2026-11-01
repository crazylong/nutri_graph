"""短信通道：配置缺失要报 503，发码响应要回显掩码号"""
from __future__ import annotations

import pytest

import config
from security.sms import reset_provider
from tests.helpers import rand_phone, requires_pg

pytestmark = requires_pg


@pytest.fixture
def unconfigured_dypns(monkeypatch):
    """把通道切成 dypns 且故意不给凭据。

    AK 必须显式清空：万一开发机的 .env 里填了真 AK，这条用例就会真的发出一条短信。
    """
    monkeypatch.setattr(config, "SMS_PROVIDER", "dypns")
    monkeypatch.setattr(config, "ALIBABA_CLOUD_ACCESS_KEY_ID", "")
    monkeypatch.setattr(config, "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
    monkeypatch.setattr(config, "SMS_SIGN_NAME", "")
    monkeypatch.setattr(config, "SMS_TEMPLATE_CODE", "")
    reset_provider()
    yield
    reset_provider()


def test_unconfigured_channel_returns_503(client, unconfigured_dypns):
    """没配好是部署问题，不是 500 —— 前端据此提示「通道不可用」而不是「服务异常」。"""
    r = client.post("/auth/sms", json={"phone": rand_phone()})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "sms_unconfigured"


def test_unconfigured_channel_blocks_login_too(client, unconfigured_dypns):
    """核验这一步同样要拦住：否则已有的验证码能在没通道时把号登进去。"""
    r = client.post("/auth/token", json={"phone": rand_phone(), "code": "123456"})
    assert r.status_code == 503


def test_send_response_echoes_masked_phone(client):
    """回显掩码号：用户要能确认「发的是不是我填的那个号」，且不能是明文。"""
    phone = rand_phone()
    r = client.post("/auth/sms", json={"phone": phone})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phone_mask"] == phone[:3] + "****" + phone[-4:]
    assert phone not in str(body)


def test_console_code_is_logged_for_admin_even_when_debug_off(client, monkeypatch):
    """DEBUG 关掉后公开接口不再回验证码，管理端流水里仍要有明文。"""
    import psycopg

    from security.crypto import phone_hash

    monkeypatch.setattr(config, "DEBUG", False)
    phone = rand_phone()
    r = client.post("/auth/sms", json={"phone": phone})
    assert r.status_code == 200, r.text
    assert "dev_code" not in r.json()
    with psycopg.connect(config.PG_DSN) as conn:
        row = conn.execute(
            "SELECT verify_code FROM sms_send_log WHERE phone_hash = %s "
            "ORDER BY id DESC LIMIT 1",
            (phone_hash(phone),),
        ).fetchone()
    assert row is not None
    assert len(row[0]) == config.SMS_CODE_LENGTH
    assert row[0].isdigit()

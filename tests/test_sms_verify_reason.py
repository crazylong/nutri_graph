"""核验失败要带回具体原因；只有说不清的故障才用「核验服务暂时不可用」。"""
from __future__ import annotations

import sys
import types

import pytest

import config
from security.sms import (
    VERIFY_UNAVAILABLE,
    DypnsProvider,
    dypns_verify_reason,
    reset_provider,
)

pytestmark = pytest.mark.no_db


def test_validate_fail_is_a_wrong_code():
    """这次线上的 isv.ValidateFail：验证码错误或已失效，不是服务挂了。"""
    assert dypns_verify_reason("isv.ValidateFail", "验证失败") == "验证码错误或已失效"
    assert dypns_verify_reason("", "验证失败") == "验证码错误或已失效"


def test_other_business_message_is_kept():
    assert dypns_verify_reason("isv.MOBILE_NUMBER_ILLEGAL", "号码格式非法") == "手机号格式不正确"
    assert dypns_verify_reason("isv.VERIFY_SCHEME_NOT_EXIST", "认证方案不存在") == "认证方案不存在"
    assert dypns_verify_reason("isp.QPS_LIMIT", "QPS受限") == "验证过于频繁，请稍后再试"


def test_unknown_fault_stays_generic():
    assert dypns_verify_reason("", "") == VERIFY_UNAVAILABLE
    assert dypns_verify_reason("isp.SYSTEM_ERROR", "系统错误") == VERIFY_UNAVAILABLE
    assert dypns_verify_reason("SignatureDoesNotMatch", "Specified signature does not match") == VERIFY_UNAVAILABLE
    dumped = "code: 400, 验证失败 request id: 01A19F91-43A5-504F-8C9E-E8E002F626C5"
    assert dypns_verify_reason("", dumped) == VERIFY_UNAVAILABLE


class _ValidateFail(Exception):
    def __init__(self):
        super().__init__(
            "Error: isv.ValidateFail code: 400, 验证失败 request id: 01A19F91"
        )
        self.code = "isv.ValidateFail"
        self.data = {
            "Code": "isv.ValidateFail",
            "Message": "验证失败",
            "statusCode": 400,
        }


class _BrokenPipe(Exception):
    pass


class _Client:
    def __init__(self, outcome):
        self.outcome = outcome

    def check_sms_verify_code_with_options(self, req, runtime):
        outcome = self.outcome
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Body:
    def __init__(self, success, code="", message="", verify_result=""):
        self.success = success
        self.code = code
        self.message = message
        self.model = types.SimpleNamespace(verify_result=verify_result)


def _provider(monkeypatch, outcome) -> DypnsProvider:
    monkeypatch.setattr(config, "ALIBABA_CLOUD_ACCESS_KEY_ID", "test-ak")
    monkeypatch.setattr(config, "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "test-sk")
    monkeypatch.setattr(config, "SMS_SIGN_NAME", "测试签名")
    monkeypatch.setattr(config, "SMS_TEMPLATE_CODE", "100001")
    monkeypatch.setattr(config, "SMS_SCHEME_NAME", "")
    reset_provider()

    models = types.ModuleType("alibabacloud_dypnsapi20170525.models")

    class CheckSmsVerifyCodeRequest:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    models.CheckSmsVerifyCodeRequest = CheckSmsVerifyCodeRequest
    pkg = types.ModuleType("alibabacloud_dypnsapi20170525")
    pkg.models = models
    monkeypatch.setitem(sys.modules, "alibabacloud_dypnsapi20170525", pkg)
    monkeypatch.setitem(sys.modules, "alibabacloud_dypnsapi20170525.models", models)
    monkeypatch.setattr(DypnsProvider, "_runtime", staticmethod(lambda: None))

    provider = DypnsProvider()
    provider._client = _Client(outcome)
    return provider


def test_verify_maps_validate_fail_exception(monkeypatch):
    provider = _provider(monkeypatch, _ValidateFail())
    ok, reason = provider.verify("13800138000", "603142")
    assert ok is False
    assert reason == "验证码错误或已失效"


def test_verify_unknown_exception_is_unavailable(monkeypatch):
    provider = _provider(monkeypatch, _BrokenPipe("connection reset"))
    ok, reason = provider.verify("13800138000", "603142")
    assert ok is False
    assert reason == VERIFY_UNAVAILABLE


def test_verify_failed_body_uses_its_message(monkeypatch):
    resp = types.SimpleNamespace(body=_Body(False, "isv.VERIFY_SCHEME_NOT_EXIST", "认证方案不存在"))
    provider = _provider(monkeypatch, resp)
    ok, reason = provider.verify("13800138000", "603142")
    assert ok is False
    assert reason == "认证方案不存在"


def test_verify_unknown_result_is_wrong_code(monkeypatch):
    resp = types.SimpleNamespace(body=_Body(True, "OK", "成功", "UNKNOWN"))
    provider = _provider(monkeypatch, resp)
    ok, reason = provider.verify("13800138000", "603142")
    assert ok is False
    assert reason == "验证码错误或已失效"

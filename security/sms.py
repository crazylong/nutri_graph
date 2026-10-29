"""
security/sms.py —— 短信通道抽象（console / dypns）

为什么不是一个 provider 写到底：
「短信服务 SMS」和「短信认证 PNVS」是两个产品，验证码的归属方不同 —— 
  * SMS：签名 + 模板自建，验证码由我方生成，自己落库校验
  * 短信认证：用平台赠送的签名与模板，**验证码由平台生成并托管**，只能调 CheckSmsVerifyCode 校验
所以接口统一成 send() / verify() 两个动作，各自实现，调用方不感知差异。

个人账号为什么走 dypns 而不是 SMS：
  官方明确「个人认证用户申请签名用途为自用的签名，无法通过签名实名制报备」，
  且已停止受理「测试或学习 / 已备案网站 / 公众号或小程序」等签名来源，
  个人账号拿不到可用签名，短信发不出去。详见 docs/plan-auth-and-dynamic-kg.md §1.3。
"""
from __future__ import annotations

import abc
import json
import logging
import uuid
from dataclasses import dataclass

import psycopg

import config
from security.crypto import hmac_hex, new_numeric_code, pepper, phone_hash, phone_mask

import db

log = logging.getLogger("security.sms")


class SmsConfigError(RuntimeError):
    """通道没配好（缺 AK / 缺签名 / 缺 SDK）。

    单独拎出来是因为它不是「用户操作错误」，也不是「上游抖动」：
    返回 500 会让前端显示「服务异常」，运维以为是 bug；
    返回 503 才是真相 —— 这个能力当前不可用，等管理员配好再来。
    """


@dataclass
class SendResult:
    ok: bool
    provider: str
    msg_id: str = ""
    dev_code: str = ""      # 仅 console provider 且 DEBUG 时回填
    error: str = ""


class SmsProvider(abc.ABC):
    name: str = ""

    @abc.abstractmethod
    def send(self, phone: str, purpose: str = "login") -> SendResult:
        ...

    @abc.abstractmethod
    def verify(self, phone: str, code: str, purpose: str = "login") -> tuple[bool, str]:
        """返回 (是否通过, 失败原因)。"""
        ...


# ============================================================
# console —— 本地开发用，不真发短信
# ============================================================
class ConsoleProvider(SmsProvider):
    name = "console"

    def _code_hash(self, phone: str, purpose: str, code: str) -> str:
        return hmac_hex(pepper(), f"{phone_hash(phone)}|{purpose}|{code}")

    def send(self, phone: str, purpose: str = "login") -> SendResult:
        code = new_numeric_code(config.SMS_CODE_LENGTH)
        code_hash = self._code_hash(phone, purpose, code)
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO sms_code (phone_hash, code_hash, purpose, expires_at) "
                "VALUES (%s, %s, %s, now() + (%s || ' seconds')::interval)",
                (phone_hash(phone), code_hash, purpose, config.SMS_VALID_TIME_S),
            )
        # 开发期把验证码打到日志，方便联调；生产用 dypns，不会走到这里
        log.warning(
            "[console-sms] 模拟下发 → %s  验证码=%s（%d 秒内有效）",
            phone_mask(phone),
            code,
            config.SMS_VALID_TIME_S,
        )
        dev_code = code if config.DEBUG else ""
        return SendResult(ok=True, provider=self.name, msg_id=f"console-{uuid.uuid4().hex[:8]}", dev_code=dev_code)

    def verify(self, phone: str, code: str, purpose: str = "login") -> tuple[bool, str]:
        ph = phone_hash(phone)
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, code_hash FROM sms_code "
                "WHERE phone_hash = %s AND purpose = %s AND consumed_at IS NULL "
                "AND expires_at > now() ORDER BY id DESC LIMIT 1",
                (ph, purpose),
            )
            row = cur.fetchone()
            if row is None:
                return False, "验证码已过期或不存在"
            row_id, code_hash = row
            if hmac_hex(pepper(), f"{ph}|{purpose}|{code}") != code_hash:
                return False, "验证码不正确"
            conn.execute("UPDATE sms_code SET consumed_at = now() WHERE id = %s", (row_id,))
        return True, ""


# ============================================================
# dypns —— 阿里云「短信认证」（号码认证服务）
# ============================================================
class DypnsProvider(SmsProvider):
    name = "dypns"

    def __init__(self) -> None:
        missing = [
            k
            for k, v in {
                "ALIBABA_CLOUD_ACCESS_KEY_ID": config.ALIBABA_CLOUD_ACCESS_KEY_ID,
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET": config.ALIBABA_CLOUD_ACCESS_KEY_SECRET,
                "SMS_SIGN_NAME": config.SMS_SIGN_NAME,
                "SMS_TEMPLATE_CODE": config.SMS_TEMPLATE_CODE,
            }.items()
            if not str(v).strip()
        ]
        if missing:
            raise SmsConfigError(
                "短信认证（dypns）缺少配置：" + ", ".join(missing) + "。"
                "签名/模板要在号码认证控制台的「赠送签名 / 赠送模板」页复制，不能自定义。"
                "开通步骤见 docs/sms-dypns-setup.md"
            )
        self._client = None

    # ---- SDK 惰性初始化：没装 SDK 也不影响 console 模式启动 ----
    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from alibabacloud_dypnsapi20170525.client import Client
            from alibabacloud_tea_openapi import models as open_api_models
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的明确提示
            raise SmsConfigError(
                "缺少短信认证 SDK：pip install alibabacloud_dypnsapi20170525"
            ) from exc

        cfg = open_api_models.Config(
            access_key_id=config.ALIBABA_CLOUD_ACCESS_KEY_ID,
            access_key_secret=config.ALIBABA_CLOUD_ACCESS_KEY_SECRET,
        )
        cfg.endpoint = "dypnsapi.aliyuncs.com"
        self._client = Client(cfg)
        return self._client

    @staticmethod
    def _runtime():
        from alibabacloud_tea_util import models as util_models

        return util_models.RuntimeOptions()

    def send(self, phone: str, purpose: str = "login") -> SendResult:
        try:
            from alibabacloud_dypnsapi20170525 import models

            client = self._get_client()
            out_id = f"{purpose}-{uuid.uuid4().hex[:12]}"
            req = models.SendSmsVerifyCodeRequest(
                phone_number=phone,
                sign_name=config.SMS_SIGN_NAME,
                template_code=config.SMS_TEMPLATE_CODE,
                template_param=config.SMS_TEMPLATE_PARAM,
                scheme_name=config.SMS_SCHEME_NAME or None,
                country_code="86",
                out_id=out_id,
                code_length=config.SMS_CODE_LENGTH,
                valid_time=config.SMS_VALID_TIME_S,
                interval=config.SMS_INTERVAL_S,
                code_type=1,              # 1 = 纯数字
                duplicate_policy=1,       # 同号重复发送时旧验证码失效
                return_verify_code=config.SMS_RETURN_VERIFY_CODE,
            )
            resp = client.send_sms_verify_code_with_options(req, self._runtime())
            body = getattr(resp, "body", None)
            if body is None or not getattr(body, "success", False):
                code = getattr(body, "code", "")
                msg = getattr(body, "message", "未知错误")
                log.warning("短信认证下发失败：code=%s msg=%s", code, msg)
                return SendResult(ok=False, provider=self.name, msg_id=out_id, error=f"{code}: {msg}")

            model = getattr(body, "model", None)
            dev_code = ""
            if config.SMS_RETURN_VERIFY_CODE and model is not None:
                dev_code = getattr(model, "verify_code", "") or ""
            return SendResult(ok=True, provider=self.name, msg_id=out_id, dev_code=dev_code)
        except SmsConfigError:
            # 没配好就是没配好，让 api 层翻译成 503，不要伪装成「上游失败」
            raise
        except Exception as exc:  # noqa: BLE001 —— 不让第三方异常穿透到业务层
            log.exception("短信认证调用异常")
            return SendResult(ok=False, provider=self.name, error=str(exc)[:200])

    def verify(self, phone: str, code: str, purpose: str = "login") -> tuple[bool, str]:
        try:
            from alibabacloud_dypnsapi20170525 import models

            client = self._get_client()
            req = models.CheckSmsVerifyCodeRequest(
                phone_number=phone,
                verify_code=code,
                scheme_name=config.SMS_SCHEME_NAME or None,
                country_code="86",
            )
            resp = client.check_sms_verify_code_with_options(req, self._runtime())
            body = getattr(resp, "body", None)
            if body is None or not getattr(body, "success", False):
                return False, getattr(body, "message", "核验服务不可用")
            model = getattr(body, "model", None)
            result = str(getattr(model, "verify_result", "") or "").upper()
            # 平台返回 PASS / UNKNOWN / FAIL，只有 PASS 算通过
            return (True, "") if result == "PASS" else (False, "验证码不正确或已过期")
        except SmsConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("短信认证核验异常")
            return False, "核验服务暂时不可用"


# ============================================================
# 工厂
# ============================================================
_provider: SmsProvider | None = None


def get_provider() -> SmsProvider:
    global _provider
    if _provider is None:
        name = config.SMS_PROVIDER
        if name == "dypns":
            _provider = DypnsProvider()
        elif name == "console":
            if not config.DEBUG:
                log.warning(
                    "SMS_PROVIDER=console 且 DEBUG=false：不会真正发送短信。"
                    "生产环境请改为 dypns 并配置 AK 与赠送签名/模板。"
                )
            _provider = ConsoleProvider()
        else:
            raise RuntimeError(f"未知的 SMS_PROVIDER：{name}（可选 console / dypns）")
        log.info("短信通道就绪：%s", _provider.name)
    return _provider


def reset_provider() -> None:
    """测试用：清掉缓存，便于切换配置。"""
    global _provider
    _provider = None


def describe_config() -> tuple[bool, list[str], bool]:
    """启动自检：不建连接、不发请求，只回答「通道能不能用」。

    返回 (凭据是否齐全, 缺哪些配置项, SDK 是否已安装)。
    只在 SMS_PROVIDER=dypns 时有意义；console 模式恒为 (True, [], True)。
    """
    if config.SMS_PROVIDER != "dypns":
        return True, [], True

    missing = [
        k
        for k, v in {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": config.ALIBABA_CLOUD_ACCESS_KEY_ID,
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": config.ALIBABA_CLOUD_ACCESS_KEY_SECRET,
            "SMS_SIGN_NAME": config.SMS_SIGN_NAME,
            "SMS_TEMPLATE_CODE": config.SMS_TEMPLATE_CODE,
        }.items()
        if not str(v).strip()
    ]
    try:
        import importlib.util

        sdk_ok = importlib.util.find_spec("alibabacloud_dypnsapi20170525") is not None
    except (ImportError, ValueError):
        sdk_ok = False
    return (not missing), missing, sdk_ok

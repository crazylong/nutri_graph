"""
security/captcha.py —— 图形验证码（算术题）

为什么是「本地生成」而不是第三方图形认证：
  阿里云「号码认证」同族确实有图形认证能力，但它需要额外开通产品、拿凭证，
  且无法在无凭证环境下验证。这里先用本地生成的算术题 + 图片，逻辑自洽、可测试，
  真上线要换成更抗 OCR 的方案（滑动/点选）时，接口不用变，只换 generate()。

为什么答案只存哈希、且绑定 challenge_id：
  和验证码同一套思路 —— 落库的是 HMAC(pepper, challenge_id|answer)，
  泄露 challenge_id 也反推不出答案；换个 challenge 同一答案也对不上。

防刷策略（配合 ratelimit）：
  频控拦「同一个号/设备高频」，图形码拦「换号批量轰炸」。
  首次发码免费，同一出口 IP 超过 CAPTCHA_FREE_SENDS_PER_IP 次后强制图形码。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

import psycopg

import config
from security.crypto import hmac_hex, pepper

import db

log = __import__("logging").getLogger("security.captcha")


@dataclass
class Captcha:
    challenge_id: str
    question: str          # 人类可读题目，如 "3 + 5 = ?"
    answer: str            # 纯数字答案
    svg: str               # 内联 SVG 图片
    expires_in: int


def _answer_hash(challenge_id: str, answer: str) -> str:
    return hmac_hex(pepper(), f"{challenge_id}|{answer}")


def _make_svg(question: str) -> str:
    """生成一个简单的算术题图片（SVG，加干扰线，不引入 Pillow）。

    抗 OCR 不是本方案的目标（算术题 + 干扰线足够挡脚本盲猜），
    真上线要换滑动/点选时替换这个函数即可。
    """
    import random

    rnd = random.Random(question)  # 同题同图，可复现
    w, h = 160, 52
    # 随机偏移的干扰线
    lines = []
    for _ in range(6):
        x1, y1 = rnd.randint(0, w), rnd.randint(0, h)
        x2, y2 = rnd.randint(0, w), rnd.randint(0, h)
        lines.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="#d9e2dd" stroke-width="1" />')
    # 随机噪点
    dots = []
    for _ in range(40):
        dots.append(f'<circle cx="{rnd.randint(0, w)}" cy="{rnd.randint(0, h)}" '
                    f'r="{rnd.randint(1, 2)}" fill="#c8d4cc" />')
    body = "".join(lines) + "".join(dots)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">'
        f'<rect width="{w}" height="{h}" fill="#f4f7f5" rx="6" />'
        f"{body}"
        f'<text x="{w/2}" y="{h/2 + 7}" text-anchor="middle" '
        f'font-family="monospace" font-size="22" font-weight="700" fill="#1c3329" '
        f'letter-spacing="2">{question}</text>'
        f"</svg>"
    )
    return svg


def generate() -> Captcha:
    """生成一道两位数以内加法题并落库。"""
    a = secrets.randbelow(20)
    b = secrets.randbelow(20)
    answer = str(a + b)
    question = f"{a} + {b} = ?"
    challenge_id = secrets.token_urlsafe(18)
    svg = _make_svg(question)

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO captcha_challenge (challenge_id, answer_hash, expires_at) "
            "VALUES (%s, %s, now() + (%s || ' seconds')::interval)",
            (challenge_id, _answer_hash(challenge_id, answer), config.CAPTCHA_TTL_S),
        )

    return Captcha(
        challenge_id=challenge_id,
        question=question,
        answer=answer,
        svg=svg,
        expires_in=config.CAPTCHA_TTL_S,
    )


def verify(challenge_id: str, answer: str) -> tuple[bool, str]:
    """校验图形码。答对即消费（单次有效），答错计一次，超次数作废。

    返回 (是否通过, 失败原因)。通过后 challenge 作废，不能再用同一个提交。
    """
    answer = (answer or "").strip()
    if not challenge_id or not answer:
        return False, "图形验证码不能为空"

    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, answer_hash, attempts, consumed_at, expires_at "
            "FROM captcha_challenge WHERE challenge_id = %s",
            (challenge_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False, "图形验证码已失效，请刷新重试"

        cid, stored_hash, attempts, consumed_at, expires_at = row
        if consumed_at is not None:
            return False, "图形验证码已使用，请刷新重试"

        import datetime as _dt

        if expires_at < _dt.datetime.now(_dt.timezone.utc):
            return False, "图形验证码已过期，请刷新重试"

        if attempts >= config.CAPTCHA_MAX_ATTEMPTS:
            conn.execute(
                "UPDATE captcha_challenge SET consumed_at = now() WHERE id = %s", (cid,)
            )
            return False, "尝试次数过多，请刷新重试"

        if hmac_hex(pepper(), f"{challenge_id}|{answer}") != stored_hash:
            conn.execute(
                "UPDATE captcha_challenge SET attempts = attempts + 1 WHERE id = %s", (cid,)
            )
            left = config.CAPTCHA_MAX_ATTEMPTS - attempts - 1
            return False, f"图形验证码不正确（还可尝试 {max(0, left)} 次）"

        conn.execute("UPDATE captcha_challenge SET consumed_at = now() WHERE id = %s", (cid,))
    return True, ""

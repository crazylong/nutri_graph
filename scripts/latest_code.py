"""
scripts/latest_code.py —— 从服务日志里取最近一条短信验证码

为什么需要这个命令：短信通道是 `console`（演示/未上真实通道），
而 `DEBUG=false` 时接口**不会**回填验证码（这是对的：一回填就等于
任何人都能以任意手机号登录）。所以验证码只存在于服务日志里。

手动翻日志要 `journalctl -u nutri-graph | grep 验证码 | tail -1`，
带手机号过滤更麻烦（日志里只有 138****8000 这种掩码）。这里一次做完。

用法：
  python -m scripts.latest_code                      # 最近一条，不限手机号
  python -m scripts.latest_code --phone 13800138000  # 只看这个号
  python -m scripts.latest_code --file /tmp/app.log  # 从文件读（不走 journald）
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# 日志格式：[console-sms] 模拟下发 → 138****8000  验证码=123456（300 秒内有效）
_LINE_RE = re.compile(r"→\s*(\d{3}\*{4}\d{4})\s*验证码=(\d{4,8})")


def read_log(unit: str, since: str, path: str | None) -> str:
    if path:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read()
        except OSError as exc:
            print(f"读不到日志文件 {path}：{exc}", file=sys.stderr)
            return ""
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "--since", since, "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        print(f"取 journald 失败（{exc}），可以用 --file 指定日志文件", file=sys.stderr)
        return ""


def mask_of(phone: str) -> str:
    p = re.sub(r"[\s\-()]", "", phone)
    for pre in ("+86", "0086"):
        if p.startswith(pre):
            p = p[len(pre):]
    if len(p) == 13 and p.startswith("86"):
        p = p[2:]
    return p[:3] + "****" + p[-4:] if len(p) == 11 else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="", help="只找这个手机号（11 位）")
    ap.add_argument("--unit", default="nutri-graph")
    ap.add_argument("--since", default="-2 hours")
    ap.add_argument("--file", default=None, help="从日志文件读，而不是 journald")
    args = ap.parse_args()

    text = read_log(args.unit, args.since, args.file)
    hits = _LINE_RE.findall(text)
    if not hits:
        print(f"没找到验证码。确认发过验证码，且时间窗够（--since '{args.since}'）。")
        return 1

    if args.phone:
        want = mask_of(args.phone)
        if not want:
            print(f"手机号看起来不对：{args.phone}", file=sys.stderr)
            return 2
        hits = [h for h in hits if h[0] == want]
        if not hits:
            print(f"没有 {want} 的验证码记录。")
            return 1

    mask, code = hits[-1]
    print(f"验证码：{code}    （发给 {mask}）")
    print(f"提示：{len(hits)} 条记录里取最新的一条；有效期见 config.SMS_VALID_TIME_S（默认 300 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

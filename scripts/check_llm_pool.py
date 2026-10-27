"""
scripts/check_llm_pool.py —— 模型池体检

用法：
    python -m scripts.check_llm_pool              # 只看状态，不发请求、不花额度
    python -m scripts.check_llm_pool --probe      # 对池内每个模型发一次最小请求，实测可用性
    python -m scripts.check_llm_pool --reset      # 清空状态：被移出池的模型全部回池

状态语义：
    ok       正常
    cooling  限流/抖动/模型不可用，冷却到点自动回池（其它错误走这条）
    removed  额度用尽，已移出 LLM_MODEL_POOL，不会自动回归（--reset 找回）

池内全部不可用时，调用会退到 .env 的 LLM_MODEL 兜底。
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata

import config


def _w(s: str) -> int:
    """显示宽度：中文占两列，直接用 len() 会错位。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def _pad(s: str, width: int, right: bool = False) -> str:
    gap = " " * max(width - _w(s), 0)
    return gap + str(s) if right else str(s) + gap


def _fmt_ts(ts: str | None) -> str:
    return (ts or "-").replace("T", " ")[5:16]


def render(snap: dict, verbose: bool = False) -> str:
    lines = []
    lines.append(f"当前使用：{snap['current'] or '（无，下一次调用会取队首）'}")
    lines.append(f"候选队列：{', '.join(snap['queue']) or '（空）'}")
    lines.append("")
    lines.append(
        _pad("模型", 26) + _pad("状态", 12) + _pad("可用", 6)
        + _pad("冷却至", 14) + _pad("调用", 6, True) + _pad("tokens", 10, True)
    )
    lines.append("-" * 78)
    for m in snap["models"]:
        note = "已移出池" if m.get("removed") else (
            _fmt_ts(m["cooldown_until"]) if m["cooldown_until"] else "-"
        )
        row = (
            _pad(m["name"], 26)
            + _pad(m["status"], 12)
            + _pad("是" if m["available"] else "否", 6)
            + _pad(note, 14)
            + _pad(m["calls"], 6, True)
            + _pad(m["tokens"], 10, True)
        )
        lines.append(row)
        if verbose and m["reason"]:
            lines.append(_pad("", 26) + f"↳ {m['reason'][:110]}")
    if snap.get("fallback"):
        lines.append("")
        lines.append(f"兜底模型（LLM_MODEL，池全不可用时才用）：{snap['fallback']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="百炼大语言模型池体检")
    p.add_argument("--probe", action="store_true", help="对每个模型发一次最小请求，实测可用性")
    p.add_argument("--reset", action="store_true", help="清空状态：被移出池的模型全部回池、冷却清零")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = p.parse_args(argv)

    from llm import pool_snapshot, probe_model, reset_pool_state

    if args.reset:
        reset_pool_state()
        print("模型池状态已重置。")

    if not config.DASHSCOPE_API_KEY:
        print("未配置 DASHSCOPE_API_KEY，无法调用任何模型。", file=sys.stderr)
        return 2

    if args.probe:
        snap = pool_snapshot()
        print("探测中（每个模型 1 次最小请求）…\n")
        ok = 0
        for m in snap["models"]:
            good, info = probe_model(m["name"])
            ok += good
            print(f"  [{'OK  ' if good else 'FAIL'}] {m['name']:<24} {info}")
        print(f"\n{ok}/{len(snap['models'])} 个模型可用\n")

    snap = pool_snapshot()
    print(json.dumps(snap, ensure_ascii=False, indent=2) if args.json else render(snap, verbose=True))
    return 0 if snap["queue"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

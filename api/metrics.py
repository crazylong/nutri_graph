"""手写的 Prometheus 文本。重查询有短缓存，进程内计数不缓存。"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

import config

log = logging.getLogger("api.metrics")

router = APIRouter(tags=["system"])

_lock = threading.Lock()
_counts = {"ok": 0, "degraded": 0, "abstain": 0, "error": 0}
_graph_hits = 0
_latencies: deque[float] = deque(maxlen=512)
_heavy = {"at": 0.0, "sms": 0, "queue": 0, "drift": 0.0}


def reset() -> None:
    global _graph_hits
    with _lock:
        for key in _counts:
            _counts[key] = 0
        _graph_hits = 0
        _latencies.clear()
    _heavy["at"] = 0.0


def note(status: str, graph_hit: bool, latency_ms: float) -> None:
    global _graph_hits
    if status not in _counts:
        status = "error"
    with _lock:
        _counts[status] += 1
        if graph_hit:
            _graph_hits += 1
        if latency_ms and latency_ms > 0:
            _latencies.append(float(latency_ms))


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _heavy_now() -> dict:
    ttl = float(config.METRICS_CACHE_TTL_S)
    now = time.monotonic()
    if _heavy["at"] and now - _heavy["at"] < ttl:
        return _heavy
    sms = 0
    depth = 0
    try:
        import db

        with db.connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM sms_send_log WHERE ok "
                "AND created_at > now() - interval '1 day'"
            ).fetchone()
            sms = int(row[0] if row else 0)
            exists = conn.execute(
                "SELECT to_regclass('public.kg_fetch_task') IS NOT NULL"
            ).fetchone()
            if exists and exists[0]:
                row = conn.execute(
                    "SELECT count(*) FROM kg_fetch_task WHERE status IN ('queued','running')"
                ).fetchone()
                depth = int(row[0] if row else 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("指标查库失败：%s", exc)
    drift = 0.0
    path = Path(config.NUTRIENT_DRIFT_PATH)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    try:
        if path.is_file():
            drift = float(json.loads(path.read_text(encoding="utf-8")).get("n") or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("读养分漂移文件失败：%s", exc)
    _heavy.update(at=now, sms=sms, queue=depth, drift=drift)
    return _heavy


def _pool_available() -> int:
    try:
        from llm import pool_snapshot

        snap = pool_snapshot()
        return sum(1 for item in snap.get("models") or [] if item.get("available"))
    except Exception as exc:  # noqa: BLE001
        log.warning("读模型池失败：%s", exc)
        return 0


def render() -> str:
    with _lock:
        counts = dict(_counts)
        hits = _graph_hits
        samples = list(_latencies)
    total = sum(counts.values())
    lines = [
        "# HELP nutri_chat_total 问答次数",
        "# TYPE nutri_chat_total counter",
    ]
    for status in ("ok", "degraded", "abstain", "error"):
        lines.append(f'nutri_chat_total{{status="{status}"}} {counts[status]}')
    avg = sum(samples) / len(samples) if samples else 0.0
    lines += [
        "# HELP nutri_chat_latency_ms 问答耗时",
        "# TYPE nutri_chat_latency_ms gauge",
        f'nutri_chat_latency_ms{{stat="avg"}} {avg:.3f}',
        f'nutri_chat_latency_ms{{stat="p95"}} {_pct(samples, 0.95):.3f}',
        "# TYPE nutri_graph_hit_ratio gauge",
        f"nutri_graph_hit_ratio {(hits / total) if total else 0:.6f}",
        "# TYPE nutri_degraded_ratio gauge",
        f"nutri_degraded_ratio {(counts['degraded'] / total) if total else 0:.6f}",
        "# TYPE nutri_abstain_ratio gauge",
        f"nutri_abstain_ratio {(counts['abstain'] / total) if total else 0:.6f}",
        "# TYPE nutri_llm_pool_available gauge",
        f"nutri_llm_pool_available {_pool_available()}",
    ]
    heavy = _heavy_now()
    lines += [
        "# TYPE nutri_sms_sent_today gauge",
        f"nutri_sms_sent_today {heavy['sms']}",
        "# TYPE nutri_fetch_queue_depth gauge",
        f"nutri_fetch_queue_depth {heavy['queue']}",
        "# TYPE nutri_nutrient_drift gauge",
        f"nutri_nutrient_drift {heavy['drift']}",
    ]
    return "\n".join(lines) + "\n"


@router.get("/metrics")
def metrics():
    if not config.METRICS_ENABLED:
        return Response(status_code=404)
    return PlainTextResponse(render(), media_type="text/plain; version=0.0.4")

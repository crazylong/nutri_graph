"""读本机 /metrics。能连上云监控就上报，否则只写日志。"""
from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

import config


def _configure() -> None:
    path = Path(config.LOG_DIR) / "metrics.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(handler.formatter)
    root.addHandler(stream)


def _fetch() -> str:
    url = os.getenv("METRICS_URL", f"http://127.0.0.1:{config.PORT}/metrics")
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _gauges(text: str) -> list[tuple[str, str, float]]:
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            left, value = line.rsplit(" ", 1)
            name, _, labels = left.partition("{")
            out.append((name, labels.rstrip("}"), float(value)))
        except ValueError:
            continue
    return out


def main() -> int:
    _configure()
    log = logging.getLogger("metrics.report")
    try:
        body = _fetch()
    except Exception as exc:  # noqa: BLE001
        log.error("读取 /metrics 失败：%s", exc)
        return 1
    rows = _gauges(body)
    for name, labels, value in rows:
        log.info("metric %s{%s} %s", name, labels, value)
    key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID") or config.ALIBABA_CLOUD_ACCESS_KEY_ID
    secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET") or config.ALIBABA_CLOUD_ACCESS_KEY_SECRET
    if not key_id or not secret:
        log.info("未配置云监控密钥，本轮只写日志")
        return 0
    try:
        import json

        from aliyunsdkcms.request.v20190101.PutCustomMetricRequest import PutCustomMetricRequest
        from aliyunsdkcore.client import AcsClient

        client = AcsClient(key_id, secret, os.getenv("CMS_REGION", "cn-hangzhou"))
        metric_list = [
            {"MetricName": name, "Dimensions": labels or "host=local", "Value": str(value)}
            for name, labels, value in rows
        ]
        request = PutCustomMetricRequest()
        request.set_accept_format("json")
        request.set_MetricList(json.dumps(metric_list, ensure_ascii=False))
        client.do_action_with_exception(request)
        log.info("已上报 %d 个指标", len(metric_list))
    except Exception as exc:  # noqa: BLE001 —— 没装 SDK 时 timer 不能因此失败
        log.warning("云监控上报跳过：%s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

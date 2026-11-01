"""每日一次：清理账本，干跑养分回填并写出漂移数。不写图。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import psycopg

import config


def _configure() -> None:
    path = Path(config.LOG_DIR) / "daily.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(handler)


def _drift_path() -> Path:
    path = Path(config.NUTRIENT_DRIFT_PATH)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    _configure()
    log = logging.getLogger("daily")
    with psycopg.connect(config.PG_DSN) as conn:
        deleted = conn.execute(
            "DELETE FROM llm_call_log WHERE ts < now() - make_interval(days => %s)",
            (config.CHAT_USAGE_RETENTION_DAYS,),
        ).rowcount
    log.info("清理 llm_call_log %s 行（保留 %s 天）", deleted, config.CHAT_USAGE_RETENTION_DAYS)
    drift = -1
    try:
        from scripts.backfill_nutrients import repair_nodes

        drift = int(repair_nodes(dry_run=True))
    except Exception as exc:  # noqa: BLE001
        log.warning("养分干跑失败：%s", exc)
    _drift_path().write_text(json.dumps({"n": drift}), encoding="utf-8")
    log.info("nutri_nutrient_drift=%s", drift)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

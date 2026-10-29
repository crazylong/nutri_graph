"""
tests/test_import_manual.py —— 手工录入接口（POST /kg/import/manual）

这条路径和上传表格共用 importer.validate_row，所以重点不是再测一遍校验规则，
而是钉住三件接口层的事：
  1. 校验不通过时必须 422 并**指出是哪个字段**（前端要靠它标红）；
  2. 通过时必须进「待审核」，不能直接进图谱；
  3. 同名再提交一次是覆盖同一个 bundle，不是堆第二条待审。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_manual_quota():
    """额度是「IP × 24 小时」计数的，会跨轮累积。

    一天里跑上六七遍全量用例，`kg_import_job` 里就攒够 40 条，
    于是整组用例集体 429 —— 看起来像接口坏了，其实是上一轮留下的计数。
    每条用例前清掉 TestClient 这个 IP 的计数（`testclient` 在真实库里不存在，
    不会误删生产数据）。
    """
    import db

    try:
        with db.connect() as conn:
            conn.execute(
                "DELETE FROM kg_import_job WHERE client_ip = 'testclient' "
                "AND source_key = 'manual_entry'"
            )
    except Exception:  # noqa: BLE001 —— 表还没建时不影响后面的用例
        pass
    yield


def _ok_payload(**over):
    base = {
        "name": "测试小米粥",
        "category": "谷物",
        "nature": "平",
        "aliases": "小米稀饭",
        "source": "中国食物成分表 第6版 · 测试",
        "energy_kcal": "46",
        "protein_g": "1.4",
        "fat_g": "0.7",
        "carb_g": "8.4",
        "calcium_mg": "10",
    }
    base.update(over)
    return base


def test_missing_required_nutrients_are_reported_per_field(client):
    r = client.post("/kg/import/manual", json={"name": "空条目", "category": "蔬菜"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_row"
    fields = {e["field"] for e in detail["errors"]}
    # 前端靠 field 找输入框标红，列名要和模板一致
    assert {"能量(kcal)", "蛋白质(g)", "脂肪(g)", "碳水化合物(g)"} <= fields


def test_bad_category_is_rejected(client):
    r = client.post("/kg/import/manual", json=_ok_payload(category="香草"))
    assert r.status_code == 422
    assert "不在枚举内" in r.json()["detail"]["message"]


def test_kj_written_as_kcal_gets_the_specific_hint(client):
    r = client.post("/kg/import/manual", json=_ok_payload(energy_kcal="1478"))
    assert r.status_code == 422
    assert "kJ" in r.json()["detail"]["message"]


def test_valid_row_lands_in_review_queue_not_in_graph(client):
    r = client.post("/kg/import/manual", json=_ok_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] in ("created", "updated")
    assert body["bundle_id"]
    assert body["review"]["required"] is True

    # 页面能查到，并且状态是「待审核」——过了审核才会变成 merged
    seen = client.get("/kg/import/foods", params={"q": "测试小米粥", "limit": 20}).json()
    hit = [i for i in seen["items"] if i["name"] == "测试小米粥"]
    assert hit, seen["items"]
    assert hit[0]["status"] == "pending_review"


def test_resubmit_updates_the_same_bundle(client):
    first = client.post("/kg/import/manual", json=_ok_payload(name="测试燕麦粥")).json()
    second = client.post("/kg/import/manual", json=_ok_payload(name="测试燕麦粥")).json()
    assert first["bundle_id"] == second["bundle_id"]
    assert second["action"] == "updated"


def test_empty_alias_and_note_are_fine(client):
    r = client.post(
        "/kg/import/manual",
        json=_ok_payload(name="测试白粥", aliases="", note="", nature=""),
    )
    assert r.status_code == 200, r.text

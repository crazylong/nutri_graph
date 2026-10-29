"""
tests/test_sensitive.py —— 敏感词（需求 1 + 2）

重点不是"词能不能匹配上"，而是这几条容易被做错的地方：

  1. 名称类字段和描述类字段的待遇不一样 —— 同一个词在名称里要拦，在描述里只提示；
  2. 命中之后的**回显**：register() 拒绝写入只挡住了"写库"，
     uncatalogued_node 还会用 r.term 拼 gaps 和 _food_requests；
  3. 输入侧只处理 political —— "菠菜能治疗贫血吗"必须照常回答菠菜；
  4. 输出扫描按类别分两种动作，不是一律整段替换；
  5. 三个批准口共用一道闸。
"""
from __future__ import annotations

import pytest

import db

from tests.helpers import PG_OK, requires_pg

# 政治类：命中即阻断；疗效类：描述里只提示
POLITICAL = "法轮功"
MEDICAL = "治疗"


@pytest.fixture(autouse=True)
def _fresh_words():
    """每条用例重新读词表：词表是进程内缓存，改了要 refresh 才生效。"""
    import kg.sensitive as sensitive

    sensitive.refresh()
    yield
    sensitive.refresh()


# ---------------------------------------------------------------
# 归一化：不做归一化等于没做
# ---------------------------------------------------------------
def test_normalization_catches_variants():
    import kg.sensitive as sensitive

    cases = [
        "治·疗",      # 插符号
        "治療",       # 繁体（_T2S 覆盖词表用到的字）
        "治 疗",      # 插空白
        "ＴＡＩ独" if False else "治-疗",  # 插连字符
    ]
    for text in cases:
        hits = sensitive.scan(text, field_name="name")
        assert any(h.word == MEDICAL for h in hits), f"没拦住变体：{text}"


def test_fullwidth_and_spaces_are_folded():
    import kg.sensitive as sensitive

    # 全角 + 中间插空格
    hits = sensitive.scan("加 微 信", field_name="note")
    assert any(h.word == "加微信" for h in hits)


# ---------------------------------------------------------------
# 字段分级：名称拦、描述提示（这是"功效和治疗不能同级"的落点）
# ---------------------------------------------------------------
@requires_pg
def test_medical_claim_blocked_in_name_but_only_reviewed_in_note():
    import kg.sensitive as sensitive

    in_name = sensitive.scan(f"{MEDICAL}糖尿病的茶", field_name="name")
    assert in_name and all(h.action == "block" for h in in_name), "名称里必须拦死"

    in_note = sensitive.scan(f"有助于{MEDICAL}糖尿病", field_name="note")
    assert in_note and all(h.action == "review" for h in in_note), "描述里只提示，不能一刀切"


def test_political_is_blocked_in_every_field():
    import kg.sensitive as sensitive

    for field in ("name", "note", "source", "claim_text"):
        hits = sensitive.scan(POLITICAL, field_name=field)
        assert hits and all(h.action == "block" for h in hits), field


def test_gongxiao_itself_is_not_a_hit():
    """误杀回归：「功效」是中医语料的正常语义单元，不能因为它像违规词就屏蔽。"""
    import kg.sensitive as sensitive

    assert sensitive.scan("清热解暑，有健脾功效", field_name="note") == []
    assert sensitive.scan("清热解暑", field_name="name") == []


# ---------------------------------------------------------------
# 登记入口：不写库、不回显
# ---------------------------------------------------------------
@requires_pg
def test_register_refuses_sensitive_term_and_does_not_echo():
    import kg.sensitive as sensitive

    from kg.request import register

    sensitive.refresh()
    r = register(f"{MEDICAL}糖尿病", who="u-test", source="chat")
    assert r.action == "rejected_not_food"
    assert r.term == "", "term 必须为空：调用方会把它拼进 gaps 和 _food_requests"
    assert MEDICAL not in r.message, "回显里不能带原词"


def test_uncatalogued_does_not_echo_rejected_term(monkeypatch):
    """register() 拒绝只挡住"写库"，回显在调用方。

    gaps 和 _food_requests 是用 r.term 拼的，/chat 的响应直接返回这两个字段。
    """
    import graph.nodes as nodes
    from kg.request import RegisterResult

    def fake_register(raw, **kwargs):
        return RegisterResult(
            action="rejected_not_food", term=raw, method="norm",
            code="sensitive", message="没能识别出具体是哪种食材",
        )

    monkeypatch.setattr(nodes, "register", fake_register, raising=False)
    out = nodes.uncatalogued_node(
        {"user_id": "u", "_unresolved": ["治疗糖尿病"], "_near_misses": []}
    )
    blob = str(out)
    assert "治疗糖尿病" not in blob, f"未收录出口仍然回显了原词：{blob}"
    assert out["_food_requests"] == []


# ---------------------------------------------------------------
# 输入侧只处理 political
# ---------------------------------------------------------------
def test_sensitive_guard_hits_political_only():
    import graph.nodes as nodes

    out = nodes.sensitive_guard_node({"question": f"{POLITICAL}能吃什么"})
    assert out.get("_redact") is True
    assert out.get("_redacted_kind") == "political"
    assert POLITICAL not in out["answer"], "固定话术里不能带原词"
    assert "换个具体的食材" in out["answer"]

    # 疗效词出现在问句里是问法，不是词条 —— 不能被当成"不是食材"
    assert nodes.sensitive_guard_node({"question": "菠菜能治疗贫血吗"}) == {}


# ---------------------------------------------------------------
# 输出扫描：两种动作
# ---------------------------------------------------------------
def test_output_scan_replaces_whole_answer_for_political():
    import kg.sensitive as sensitive

    cleaned, hits = sensitive.rewrite_answer(f"据说{POLITICAL}也能入菜。")
    assert hits
    assert POLITICAL not in cleaned
    assert cleaned == sensitive.POLITICAL_ANSWER


def test_output_scan_keeps_facts_and_drops_medical_claim():
    import kg.sensitive as sensitive

    text = "菠菜每 100g 含铁 2.7mg。它可以治疗贫血。和猪肝一起吃吸收更好。"
    cleaned, hits = sensitive.rewrite_answer(text)
    assert hits
    assert "菠菜" in cleaned, "食材事实要保留 —— 整段替换会把菠菜一起换掉"
    assert "猪肝" in cleaned, "搭配建议也要保留"
    assert "治疗" not in cleaned, "疗效断言要剔除"


def test_output_scan_falls_back_when_everything_is_dropped():
    import kg.sensitive as sensitive

    cleaned, hits = sensitive.rewrite_answer("这个可以治疗贫血。")
    assert hits
    assert cleaned == sensitive.MEDICAL_FALLBACK


def test_output_scan_keeps_superlative_and_drops_unnegated_claim():
    """「最好」只审计，不删句。同句里未否定的「治疗」仍整句删。"""
    import kg.sensitive as sensitive

    kept, hits = sensitive.rewrite_answer("黑豆和薏米一起吃最好。")
    assert kept == "黑豆和薏米一起吃最好。"
    assert any(h.word == "最好" for h in hits)

    dropped, hits = sensitive.rewrite_answer("菠菜能治疗贫血，但最好少吃。")
    assert dropped == sensitive.MEDICAL_FALLBACK
    assert any(h.word == "最好" for h in hits)
    assert any(h.word == "治疗" for h in hits)


def test_claim_negation_is_per_hit_with_end_to_start_gap():
    """否定看的是词尾到命中词头的间隔，不是往前截 6 个字。"""
    import kg.sensitive as sensitive

    keep_cannot, _ = sensitive.rewrite_answer("菠菜不能治疗贫血。")
    assert keep_cannot == "菠菜不能治疗贫血。"

    drop_can, _ = sensitive.rewrite_answer("菠菜能治疗贫血。")
    assert drop_can == sensitive.MEDICAL_FALLBACK

    # 「没有证据」离「治疗」有 5 个字，前 6 字截断会漏掉否定
    keep_evidence, hits = sensitive.rewrite_answer("没有证据表明菠菜能治疗贫血，但含铁较丰富。")
    assert "含铁" in keep_evidence
    assert "治疗" in keep_evidence
    assert any(h.word == "治疗" for h in hits)

    # 治愈没被否定：整句删，不能因为前面的「不能治疗」把后半句留下
    drop_cure, _ = sensitive.rewrite_answer("菠菜不能治疗贫血，但可以治愈夜盲。")
    assert "夜盲" not in drop_cure
    assert drop_cure == sensitive.MEDICAL_FALLBACK

    # 同一个词出现两次：有一处没被否定，整句删
    twice, _ = sensitive.rewrite_answer("菠菜治疗贫血，不能治疗夜盲。")
    assert twice == sensitive.MEDICAL_FALLBACK


# ---------------------------------------------------------------
# 批准闸
# ---------------------------------------------------------------
def test_assert_approvable_blocks_and_requires_confirm():
    import kg.sensitive as sensitive

    blocking = [sensitive.Hit("name", POLITICAL, "political", "block")]
    with pytest.raises(sensitive.SensitiveBlocked):
        sensitive.assert_approvable(blocking, confirm=True)

    pending = [sensitive.Hit("note", MEDICAL, "medical_claim", "review")]
    with pytest.raises(sensitive.SensitiveBlocked):
        sensitive.assert_approvable(pending, confirm=False)
    # 显式确认后放行
    sensitive.assert_approvable(pending, confirm=True)


def _row(**over) -> list:
    """按 COLUMNS 的顺序拼一行：手写下标很容易错位（分类/性味挨着）。"""
    from kg import importer

    base = {
        "name": "测试绿豆汤",
        "category": "谷物",
        "nature": "平",
        "aliases": "绿豆汤",
        "english_name": "",
        "scientific_name": "",
        "source": "中国食物成分表 第6版",
        "note": "",
        "energy_kcal": "46",
        "protein_g": "1.4",
        "fat_g": "0.7",
        "carb_g": "8.4",
        "calcium_mg": "10",
    }
    base.update(over)
    return [base.get(c.key, "") for c in importer.COLUMNS]


@requires_pg
def test_import_row_with_medical_claim_in_name_is_error():
    """完成条件：含疗效词的名称 → error，且提交接口不收 error 行。"""
    from kg import importer

    r = importer.validate_row(2, _row(name=f"{MEDICAL}糖尿病茶"), list(importer.COLUMNS))
    assert r.verdict == "error", r.messages
    assert not r.importable
    # 消息里不能回显原词
    assert MEDICAL not in str([m.as_dict() for m in r.messages])


@requires_pg
def test_normal_note_passes():
    from kg import importer

    r = importer.validate_row(2, _row(note="清热解暑，有健脾功效"), list(importer.COLUMNS))
    assert r.verdict in ("ok", "warn"), r.messages


@requires_pg
def test_chat_political_question_is_redacted_end_to_end(client):
    """端到端：命中边真的接到了 memory_write，而且四处都不留原词。

    只测节点不够 —— 上一版就是把命中边接到了 END，打码那一步根本没执行。
    """
    import kg.sensitive as sensitive

    r = client.post("/chat", json={"question": f"{POLITICAL}能吃什么"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert POLITICAL not in r.text, "响应里不能出现原词（含 gaps / food_requests）"
    assert body["answer"] == sensitive.POLITICAL_ANSWER
    assert body["food_requests"] == []

    # 落库的是占位，会话标题也不带原词
    sid = body.get("session_id")
    assert sid, "命中边没走到 memory_write（session_id 都没有）"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM message WHERE session_id = %s ORDER BY id", (sid,)
        ).fetchall()
        title = conn.execute("SELECT title FROM session WHERE id = %s", (sid,)).fetchone()
    assert rows, "这一轮没有落库"
    # 只打码用户侧；助手侧保留固定话术（它本来就不含原词，换成占位没有收益）
    user_msgs = [c for role, c in rows if role == "user"]
    assistant_msgs = [c for role, c in rows if role == "assistant"]
    assert user_msgs and all(c == sensitive.REDACT for c in user_msgs), rows
    assert all(POLITICAL not in c for c in assistant_msgs), rows
    assert POLITICAL not in (title[0] if title and title[0] else "")


@requires_pg
def test_marketing_word_in_note_is_warn_not_error():
    """营销词在描述里只是提示，不该把整行判死。"""
    from kg import importer

    r = importer.validate_row(2, _row(note="详情加微信咨询"), list(importer.COLUMNS))
    assert r.verdict == "warn", r.messages
    assert r.importable


@requires_pg
def test_name_only_blocks_name_and_keeps_description_and_answer():
    """功效只在名称拦截。描述放行，回答里也不按疗效整句删。"""
    import kg.sensitive as sensitive
    from kg import importer

    name_hits = sensitive.scan("菠菜功效", field_name="name")
    assert any(h.word == "功效" and h.action == "block" for h in name_hits)
    note_hits = sensitive.scan("清热解暑，有健脾功效", field_name="note")
    assert not any(h.word == "功效" for h in note_hits)

    blocked = importer.validate_row(2, _row(name="菠菜功效"), list(importer.COLUMNS))
    assert blocked.verdict == "error"
    passed = importer.validate_row(
        3, _row(note="清热解暑，有健脾功效"), list(importer.COLUMNS)
    )
    assert passed.verdict == "ok", passed.messages
    assert not any("功效" in m.text for m in passed.messages)

    text = "这道菜有健脾功效。"
    cleaned, hits = sensitive.rewrite_answer(text)
    assert cleaned == text
    assert not any(h.word == "功效" for h in hits)

    political = sensitive.scan("毛爷爷", field_name="note")
    assert any(h.word == "毛爷爷" and h.action == "block" for h in political)
    marketing = sensitive.scan("联系销售后下单", field_name="note")
    words = {h.word for h in marketing}
    assert "销售" in words and "下单" in words
    assert not any(h.word == "电话" for h in sensitive.scan("联系电话", field_name="note"))


def test_redact_trace_keeps_guard_output():
    from observability.tracing import redact_trace

    class _Trace:
        def __init__(self):
            self.payloads = []

        def update(self, **kwargs):
            self.payloads.append(kwargs)

    kept = _Trace()
    redact_trace(kept, "[内容已过滤]", keep_output=True)
    assert kept.payloads[0]["input"]["question"] == "[内容已过滤]"
    assert "output" not in kept.payloads[0]

    wiped = _Trace()
    redact_trace(wiped, "[内容已过滤]")
    assert wiped.payloads[0]["output"]["answer"] == "[内容已过滤]"


@requires_pg
def test_memory_write_red_flag_sets_guard_kind_and_keeps_answer():
    """红旗路径不经过 sensitive_guard。用户侧打码，就医回答原样留下。"""
    import graph.nodes as nodes
    import kg.sensitive as sensitive

    question = f"我胸痛，听说{POLITICAL}能吃"
    state = {"question": question, "user_id": "u-redact-guard"}
    state.update(nodes.guard_node(state))
    out = nodes.memory_write_node(state)
    assert out.get("_redact") is True
    assert out.get("_redacted_kind") == "guard"
    sid = out["session_id"]
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM message WHERE session_id = %s ORDER BY id",
            (sid,),
        ).fetchall()
        title = conn.execute("SELECT title FROM session WHERE id = %s", (sid,)).fetchone()
    user_msgs = [c for role, c in rows if role == "user"]
    assistant_msgs = [c for role, c in rows if role == "assistant"]
    assert user_msgs == [sensitive.REDACT]
    assert assistant_msgs and "就医" in assistant_msgs[0]
    assert POLITICAL not in assistant_msgs[0]
    assert POLITICAL not in (title[0] or "")


@requires_pg
def test_get_bundle_live_scan_uses_note_item_and_row_source_desc():
    """备注在 item 上。行上的 source_desc 不能被空的 item 盖掉。"""
    import secrets

    from kg.promote import get_bundle

    food_key = "t31-" + secrets.token_hex(4)
    with db.connect() as conn:
        bid = conn.execute(
            "INSERT INTO kg_food_bundle (food_key, canonical_name, source_desc, status) "
            "VALUES (%s, '测试豆', '详情加微信', 'pending_review') RETURNING id",
            (food_key,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO kg_food_bundle_item "
            "(bundle_id, item_type, field_name, field_value) VALUES "
            "(%s, 'field', 'note', '有助于治疗'), (%s, 'field', 'source_desc', '')",
            (bid, bid),
        )
    try:
        bundle = get_bundle(bid)
        live = {(h["field"], h["word"]) for h in bundle["hits_live"]}
        assert ("note", "治疗") in live
        assert ("source_desc", "加微信") in live
        assert bundle["source_desc"] == "详情加微信"
        assert bundle["hits_stored"] == []
    finally:
        with db.connect() as conn:
            conn.execute("DELETE FROM kg_food_bundle WHERE id = %s", (bid,))


@requires_pg
def test_candidate_review_needs_confirm_block_does_not(client):
    """review 不确认是 409，确认后 200。block 确认了也是 409。"""
    import secrets

    from tests.test_admin import _admin

    _, headers = _admin(client)
    review_id = block_id = None
    with db.connect() as conn:
        review_id = conn.execute(
            "INSERT INTO kg_candidate "
            "(batch_id, src_type, src_id, rel, dst_type, dst_id, dst_name, claim_text) "
            "VALUES ('t31', 'Ingredient', %s, 'SUITABLE_FOR', 'Symptom', 'DST_T31', "
            "'贫血', '菠菜能治疗贫血') RETURNING id",
            ("SRC_" + secrets.token_hex(4),),
        ).fetchone()[0]
        block_id = conn.execute(
            "INSERT INTO kg_candidate "
            "(batch_id, src_type, src_id, rel, dst_type, dst_id, dst_name, claim_text) "
            "VALUES ('t31', 'Ingredient', %s, 'SUITABLE_FOR', 'Symptom', 'DST_T31B', "
            "%s, '') RETURNING id",
            ("SRC_" + secrets.token_hex(4), POLITICAL),
        ).fetchone()[0]
    try:
        listed = client.get(
            "/admin/kg/candidates",
            params={"status": "pending", "limit": 500},
            headers=headers,
        )
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert items and "sensitive_block" in items[0]
        review_row = next((i for i in items if i["id"] == review_id), None)
        if review_row is not None:
            assert review_row["sensitive_block"] == 0
            assert review_row["sensitive_review"] >= 1
            assert "治疗" in review_row["sensitive_words"]
        block_row = next((i for i in items if i["id"] == block_id), None)
        if block_row is not None:
            assert block_row["sensitive_block"] >= 1

        denied = client.post(
            f"/admin/kg/candidates/{review_id}/approve",
            headers=headers,
            json={},
        )
        assert denied.status_code == 409, denied.text
        ok = client.post(
            f"/admin/kg/candidates/{review_id}/approve",
            headers=headers,
            json={"confirm_sensitive": True},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "approved"

        still = client.post(
            f"/admin/kg/candidates/{block_id}/approve",
            headers=headers,
            json={"confirm_sensitive": True},
        )
        assert still.status_code == 409, still.text
    finally:
        with db.connect() as conn:
            conn.execute(
                "DELETE FROM kg_candidate WHERE id = ANY(%s)",
                ([i for i in (review_id, block_id) if i is not None],),
            )


@requires_pg
def test_merge_nodes_keeps_review_and_logs_hash(caplog):
    import logging

    from kg.merge import _nodes_passing_sensitive

    caplog.set_level(logging.ERROR, logger="kg.merge")
    kept = _nodes_passing_sensitive(
        [
            {"id": 1, "name": POLITICAL, "props": {"canonical_name": POLITICAL}},
            {
                "id": 2,
                "name": "黄豆",
                "props": {"canonical_name": "黄豆", "summary": "有助于治疗贫血"},
            },
        ]
    )
    assert [row["id"] for row in kept] == [2]
    assert POLITICAL not in caplog.text
    assert "hash=" in caplog.text

"""
memory/anonymous_merge.py —— 匿名身份 → 注册账号的数据归并

为什么必须做：
  用户在未登录状态下说「我海鲜过敏」，这条画像挂在临时匿名身份上。
  登录时如果不归并，换个 user_id 就等于失忆 —— 而「记住你的忌口」正是记忆系统唯一的卖点。

要归并的四类数据：
  session      → 直接改 user_id（message 靠 session_id 关联，不需要动）
  user_profile → 不能直接改：目标账号可能已有画像，要按字段做并集
  memory_fact  → 直接改 user_id

设计约束：
  * 未登录时 user_profile 主键是 anon_id，登录后目标 key 可能已存在 → 必须 upsert 合并数组
  * 老表可能还没建（init_pg 未跑）→ 每张表独立 savepoint，失败只跳过这一张
  * 归并必须幂等：重复调用不产生重复数据（UPDATE 天然幂等，数组取并集也幂等）
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg

import config
from security.tokens import ANON_PREFIX

log = logging.getLogger("memory.anonymous_merge")


def is_anon_id(value: str) -> bool:
    return bool(value) and value.startswith(ANON_PREFIX)


def merge_anon_into_user(anon_id: str, user_id: str) -> dict[str, Any]:
    """把匿名身份下的数据搬到真实账号。返回归并明细（便于接口回显与排查）。"""
    result: dict[str, Any] = {
        "anon_id": anon_id,
        "user_id": user_id,
        "sessions": 0,
        "memory_facts": 0,
        "profile": False,
        "skipped": False,
        "errors": [],
    }
    if not is_anon_id(anon_id):
        # 只允许归并服务端签发的匿名标识，否则等于把任意 user_id 的历史并入自己
        result["skipped"] = True
        result["errors"].append("anon_id 非法（必须是服务端签发的 anon: 前缀）")
        return result
    if not user_id or user_id == anon_id:
        result["skipped"] = True
        return result

    with psycopg.connect(config.PG_DSN) as conn:
        for key, sql in (
            ("sessions", "UPDATE session SET user_id = %s WHERE user_id = %s"),
            ("memory_facts", "UPDATE memory_fact SET user_id = %s WHERE user_id = %s"),
        ):
            try:
                with conn.transaction():
                    cur = conn.cursor()
                    cur.execute(sql, (user_id, anon_id))
                    result[key] = cur.rowcount
            except Exception as exc:  # noqa: BLE001 —— 表不存在就跳过，不阻断登录
                log.warning("归并 %s 失败（跳过）：%s", key, exc)
                result["errors"].append(f"{key}: {exc}")

        # ---- user_profile：按字段求并集 ----
        try:
            with conn.transaction():
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO user_profile (
                        user_id, allergies, conditions, diet_style, avoid_list,
                        age, gender, height_cm, weight_kg, hobbies, symptoms, updated_at
                    )
                    SELECT %s, allergies, conditions, diet_style, avoid_list,
                           age, gender, height_cm, weight_kg, hobbies, symptoms, now()
                    FROM user_profile WHERE user_id = %s
                    ON CONFLICT (user_id) DO UPDATE SET
                        allergies = ARRAY(
                            SELECT DISTINCT unnest(
                                COALESCE(user_profile.allergies, '{}')
                                || COALESCE(EXCLUDED.allergies, '{}'))),
                        conditions = ARRAY(
                            SELECT DISTINCT unnest(
                                COALESCE(user_profile.conditions, '{}')
                                || COALESCE(EXCLUDED.conditions, '{}'))),
                        avoid_list = ARRAY(
                            SELECT DISTINCT unnest(
                                COALESCE(user_profile.avoid_list, '{}')
                                || COALESCE(EXCLUDED.avoid_list, '{}'))),
                        hobbies = ARRAY(
                            SELECT DISTINCT unnest(
                                COALESCE(user_profile.hobbies, '{}')
                                || COALESCE(EXCLUDED.hobbies, '{}'))),
                        symptoms = ARRAY(
                            SELECT DISTINCT unnest(
                                COALESCE(user_profile.symptoms, '{}')
                                || COALESCE(EXCLUDED.symptoms, '{}'))),
                        diet_style = COALESCE(
                            NULLIF(user_profile.diet_style, ''), EXCLUDED.diet_style),
                        gender = COALESCE(
                            NULLIF(user_profile.gender, ''), EXCLUDED.gender),
                        age = COALESCE(user_profile.age, EXCLUDED.age),
                        height_cm = COALESCE(user_profile.height_cm, EXCLUDED.height_cm),
                        weight_kg = COALESCE(user_profile.weight_kg, EXCLUDED.weight_kg),
                        updated_at = now()
                    """,
                    (user_id, anon_id),
                )
                result["profile"] = cur.rowcount > 0
                cur.execute("DELETE FROM user_profile WHERE user_id = %s", (anon_id,))
        except Exception as exc:  # noqa: BLE001
            log.warning("归并 user_profile 失败（跳过）：%s", exc)
            result["errors"].append(f"profile: {exc}")

    if result["sessions"] or result["profile"] or result["memory_facts"]:
        log.info(
            "匿名归并完成 anon=%s → user=%s：会话 %s 条 / 画像 %s / 长期事实 %s 条",
            anon_id, user_id, result["sessions"],
            "合并" if result["profile"] else "无",
            result["memory_facts"],
        )
    return result

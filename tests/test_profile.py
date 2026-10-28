"""画像写入：空标量不覆盖，数组并集，注销删整行。"""
from __future__ import annotations

import psycopg

import config
from memory.long_term import UserProfile
from tests.helpers import bearer, login, requires_pg

pytestmark = requires_pg


def test_update_skips_empty_scalars_and_unions_arrays(client):
    uid = login(client)["user"]["user_id"]
    profile = UserProfile()
    profile.update(uid, allergies=["牛奶"], age=30, hobbies=["跑步"], weight_kg=55)
    profile.update(
        uid,
        allergies=[],
        age=None,
        gender="",
        hobbies=["跑步", "游泳"],
        weight_kg=0,
        symptoms=["失眠"],
    )
    got = profile.get(uid)
    assert got["allergies"] == ["牛奶"]
    assert got["age"] == 30
    assert got["gender"] == ""
    assert got["weight_kg"] == 55
    assert set(got["hobbies"]) == {"跑步", "游泳"}
    assert got["symptoms"] == ["失眠"]

    profile.update(uid, hobbies=["游泳"])
    assert set(profile.get(uid)["hobbies"]) == {"跑步", "游泳"}


def test_delete_me_removes_profile_row(client):
    tokens = login(client)
    uid = tokens["user"]["user_id"]
    UserProfile().update(uid, age=35, symptoms=["失眠"], hobbies=["跑步"])
    r = client.delete("/auth/me", headers=bearer(tokens["access_token"]))
    assert r.status_code == 200
    with psycopg.connect(config.PG_DSN) as conn:
        n = conn.execute(
            "SELECT count(*) FROM user_profile WHERE user_id = %s",
            (uid,),
        ).fetchone()[0]
    assert n == 0

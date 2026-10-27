"""
kg/constraints.py —— Neo4j 约束与索引

注意：Neo4j 只支持「节点属性」唯一约束，不支持关系唯一约束。
因此幂等的根基是：
  1) 每个节点的 canonical_id 唯一约束（MERGE 的锚点）
  2) 关系用确定性 MERGE + relation_key 业务键（见 kg/schema.py）
"""
from __future__ import annotations

import logging

from neo4j import GraphDatabase

import config

log = logging.getLogger("kg.constraints")

# 节点唯一约束：FOR (n:Label) REQUIRE n.canonical_id IS UNIQUE
UNIQUE_CONSTRAINTS = [
    ("Ingredient", "canonical_id"),
    ("Nutrient", "canonical_id"),
    ("Population", "canonical_id"),
    ("Symptom", "canonical_id"),
    ("Taboo", "canonical_id"),
]

# 常规索引（加速查询）
INDEXES = [
    ("Ingredient", "canonical_name"),
    ("Ingredient", "category"),
    ("Nutrient", "name"),
    ("Symptom", "name"),
    ("Population", "name"),
    ("Taboo", "name"),
]


def ensure_schema() -> None:
    """幂等地创建所有约束与索引，可反复调用。"""
    driver = GraphDatabase.driver(config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD))
    try:
        with driver.session() as session:
            for label, prop in UNIQUE_CONSTRAINTS:
                q = f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
                session.run(q)
                log.info("constraint ok: %s.%s UNIQUE", label, prop)
            for label, prop in INDEXES:
                q = f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
                session.run(q)
                log.info("index ok: %s.%s", label, prop)
    finally:
        driver.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_schema()
    print("Neo4j schema (constraints + indexes) ready.")

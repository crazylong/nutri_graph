"""把 data/testdata 下的文档导入知识库（eval 租户），并 approve 入库。

用法（在服务器上）：
  python -m scripts.import_testdata                 # 默认只导 .md（快，零 OCR）
  python -m scripts.import_testdata --include-pdf   # 连 .pdf 一起导（要跑 dots.mocr，慢）

- md 解析快；pdf 要走版面 OCR（dots.mocr），大文件逐页很慢，默认跳过。
- 导入到租户 `eval`（隔离正式知识库），分类默认 tech。
- 导入即 approve（解析 + 切片 + 向量化），因为评测需要真实 chunk_id。
- 幂等：按 (tenant, filename) 判断已存在则跳过，重复跑不会重复建。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "data" / "testdata"
TENANT = "eval"

sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from kb.publish import add_document, approve  # noqa: E402


def _ensure_tenant(tenant: str) -> str:
    """建 eval 租户，并把一个 admin 挂成它的成员，返回该 admin 的 user_id。

    add_document 会校验 user 是 tenant_user 里的成员，所以要先建成员关系。
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO tenant (name) VALUES (%s) ON CONFLICT DO NOTHING",
            (tenant,),
        )
        admin = conn.execute(
            "SELECT user_id::text FROM app_user WHERE role='admin' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if admin is None:
            raise SystemExit("没有 admin 用户，无法建立租户成员关系")
        admin_id = admin[0]
        conn.execute(
            "INSERT INTO tenant_user (tenant_name, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tenant, admin_id),
        )
    return admin_id


def _existing_filenames(tenant: str) -> set[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT filename FROM kb_document WHERE tenant_id = %s",
            (tenant,),
        ).fetchall()
    return {row[0] for row in rows}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--include-pdf", action="store_true", help="连同 .pdf 一起导入（OCR 慢）")
    args = p.parse_args()

    admin_id = _ensure_tenant(TENANT)
    suffixes = {".md", ".markdown", ".docx"}
    if args.include_pdf:
        suffixes.add(".pdf")

    files = sorted(
        f for f in TESTDATA.iterdir()
        if f.is_file() and f.suffix.lower() in suffixes
    )
    if not files:
        print(f"{TESTDATA} 下没有可导入的文件")
        return

    existing = _existing_filenames(TENANT)
    imported = 0
    skipped = 0
    for f in files:
        if f.name in existing:
            print(f"  跳过（已存在）：{f.name}")
            skipped += 1
            continue
        raw = f.read_bytes()
        doc = add_document(
            tenant_id=TENANT,
            title=f.stem,
            doc_kind="tech",
            filename=f.name,
            raw=raw,
            user={"user_id": admin_id, "phone_mask": "eval-import"},
        )
        approved = approve(doc["id"])
        imported += 1
        print(f"  已导入并审核：{f.name}  →  {approved['status']}")

    print(f"\n完成：导入 {imported} 篇，跳过 {skipped} 篇（租户 {TENANT}）")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""复现「双正式并存 → 入站命中不稳、版本目录自相矛盾」。

仅用标准库 sqlite3，按修复前 backend/app.py 的原始表结构（无唯一索引）
与原始命中 SQL（ORDER BY id DESC LIMIT 1）在临时库上演示缺陷：

  1. 种子 v3 正式/启用（必填 order_id,amount,currency）；
  2. 套用 fixtures/dual_official_seed.sql，再塞入 v3b 正式/启用（必填 order_id,amount）；
  3. 同一逻辑状态（两版都是正式启用）下，仅改变物理插入顺序，
     命中版本就跟着变 —— 命中不稳；
  4. 版本目录同时挂着两条 正式/启用 —— 自相矛盾。

运行：python3 -I fixtures/repro_dual_official.py
退出码：0 表示缺陷成功复现（这正是要被修复的东西）。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

SQL_FIXTURE = Path(__file__).resolve().parent / "dual_official_seed.sql"

# 修复前 backend/app.py 的原始 DDL（无任何 正式/启用 唯一约束）
OLD_DDL = """
CREATE TABLE contracts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT UNIQUE NOT NULL,
  subject TEXT NOT NULL,
  mode TEXT NOT NULL,
  track TEXT NOT NULL,
  status TEXT NOT NULL,
  required_fields TEXT NOT NULL
);
"""

# 修复前 /api/ingress/validate 的命中 SQL
OLD_HIT_SQL = ("SELECT * FROM contracts WHERE subject=? AND track='正式' "
               "AND status='启用' ORDER BY id DESC LIMIT 1")

SEED_V3 = ("v3", "订单入站", "严格", "正式", "启用", "order_id,amount,currency")


def hit_version(conn, subject="订单入站"):
    row = conn.execute(OLD_HIT_SQL, (subject,)).fetchone()
    return row[1] if row else None


def catalog(conn, subject="订单入站"):
    return conn.execute(
        "SELECT version, track, status FROM contracts WHERE subject=? "
        "AND track='正式' AND status='启用' ORDER BY id", (subject,)).fetchall()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="dual-official-"))
    db = tmp / "repro.db"
    conn = sqlite3.connect(db)
    conn.executescript(OLD_DDL)
    conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
                 " VALUES (?,?,?,?,?,?)", SEED_V3)
    conn.executescript(SQL_FIXTURE.read_text(encoding="utf-8"))
    conn.commit()

    print("== 双正式并存夹具已就位 ==")
    officials = catalog(conn)
    for v, t, s in officials:
        print(f"  目录行: {v}  {t}/{s}")
    assert len(officials) == 2, "夹具应造成两条 正式/启用"
    print(f"  → 版本目录自相矛盾：同主题 正式/启用 共 {len(officials)} 条")

    first_hit = hit_version(conn)
    print(f"\n第一次命中（v3 先于 v3b 插入，id 最大者胜）: {first_hit}")

    # 同一逻辑状态：删掉 v3 再原样插回（id 变最大），其余不动
    conn.execute("DELETE FROM contracts WHERE version='v3'")
    conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
                 " VALUES (?,?,?,?,?,?)", SEED_V3)
    conn.commit()
    second_hit = hit_version(conn)
    print(f"第二次命中（仅把 v3 删后重插，逻辑状态不变）: {second_hit}")

    assert first_hit == "v3b" and second_hit == "v3", "命中应随物理顺序翻转"
    print("\n→ 命中不稳复现成功：同一逻辑状态，命中版本随插入顺序翻转 "
          f"({first_hit} → {second_hit})；两版必填不同，校验口径随之漂移。")
    print("→ 缺陷复现完毕（库文件:", db, "）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

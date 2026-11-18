#!/usr/bin/env python3
"""离线核验「正式启用唯一（乙路径·原子替换）」。

本仓评分环境用 pytest 跑 tests/test_official_uniqueness.py；本脚本用于无
fastapi/pytest 的离线环境：借 fixtures/stubs 最小桩导入真实 backend/app.py，
直接调用端点函数，核验与 pytest 用例相同的不变量。任一失败即非零退出。

运行：python3 -I fixtures/check_uniqueness_offline.py
"""
import sqlite3
import sys
import tempfile
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fixtures" / "stubs"))
sys.path.insert(0, str(ROOT / "backend"))

import app as A  # noqa: E402  真实后端模块

FAILURES = []


def check(name):
    def deco(fn):
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            FAILURES.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
        return fn
    return deco


def fresh_db():
    tmp = Path(tempfile.mkdtemp(prefix="uniq-check-"))
    A.DB_PATH = tmp / "t.db"
    A.init_db()
    return A.DB_PATH


def auth():
    tok = A.login(A.LoginIn(username="admin", password="123456"))["token"]
    return "Bearer " + tok


def add_contract(version, subject="订单入站", mode="严格", track="正式",
                 status="停用", required="order_id,amount"):
    with A.connect() as c:
        c.execute(
            "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
            " VALUES (?,?,?,?,?,?)", (version, subject, mode, track, status, required))


def officials(subject="订单入站"):
    with A.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM contracts WHERE subject=? AND track='正式' AND status='启用'"
            " ORDER BY id", (subject,)).fetchall()]


def row_of(version):
    with A.connect() as c:
        return dict(c.execute("SELECT * FROM contracts WHERE version=?", (version,)).fetchone())


def log_items():
    return A.official_log(authorization=AUTH)["items"]


def enable(version):
    return A.enable_contract(A.EnableIn(version=version), authorization=AUTH)


DB = fresh_db()
AUTH = auth()


@check("乙路径：新启用原子替换现行指针，旧正式进只读历史，必填不丢，命中稳定")
def _():
    add_contract("v4", required="order_id,amount,currency,channel")
    r = enable("v4")
    assert r["official"] == "v4" and r["replaced"] == "v3", r
    assert [o["version"] for o in officials()] == ["v4"]
    old = row_of("v3")
    assert old["status"] == "历史" and old["required_fields"] == "order_id,amount,currency", old
    hit = A.validate(A.IngressIn(subject="订单入站", payload_fields="order_id,amount"),
                     authorization=AUTH)
    assert hit["result"] == "拒收" and hit["version"] == "v4", hit
    assert any(e["event"] == "启用替换" and e["version"] == "v4" for e in log_items())
    n = len(log_items())
    r2 = enable("v4")  # 幂等，当天不改口、不重复记册
    assert r2.get("already") is True and len(log_items()) == n, r2


@check("无现行正式的主题：停用版本可启用，记「正式启用」")
def _():
    r = enable("v2")  # 库存入站·停用
    assert r["replaced"] is None, r
    assert [o["version"] for o in officials("库存入站")] == ["v2"]
    hit = A.validate(A.IngressIn(subject="库存入站", payload_fields="sku,qty"),
                     authorization=AUTH)
    assert hit["result"] == "通过" and hit["version"] == "v2", hit
    assert any(e["event"] == "正式启用" and e["version"] == "v2" for e in log_items())


@check("必填为空拒绝启用并记「唯一偏离」，现行正式不动")
def _():
    add_contract("v5-empty", required="")
    try:
        enable("v5-empty")
        raise AssertionError("应拒绝必填为空的启用")
    except A.HTTPException as e:
        assert e.status_code == 409 and "必填" in str(e.detail), e.detail
    assert [o["version"] for o in officials()] == ["v4"]
    assert row_of("v5-empty")["status"] == "停用"
    assert any(e["event"] == "唯一偏离" and e["version"] == "v5-empty" for e in log_items())


@check("影子轨不得启用为正式（唯一偏离）；版本不存在 404")
def _():
    try:
        enable("v4-shadow")
        raise AssertionError("应拒绝影子轨启用")
    except A.HTTPException as e:
        assert e.status_code == 409 and "唯一偏离" in str(e.detail), e.detail
    assert row_of("v4-shadow")["status"] == "试跑"
    try:
        enable("v-nothing")
        raise AssertionError("应 404")
    except A.HTTPException as e:
        assert e.status_code == 404, e.status_code


@check("同主题并发启用排队，后到只记「启用争用」，正式仍唯一")
def _():
    add_contract("vA", required="order_id,amount,currency")
    add_contract("vB", required="order_id,amount")
    entered, release = threading.Event(), threading.Event()
    A._ENABLE_HOOK = lambda subject: (entered.set(), release.wait(timeout=10))
    A._ENABLE_LOCK_TIMEOUT = 0.3
    winner = {}

    def t1():
        try:
            winner["res"] = enable("vA")
        except Exception as e:  # noqa
            winner["err"] = e

    th = threading.Thread(target=t1)
    th.start()
    try:
        assert entered.wait(timeout=5), "先到启用应已持锁"
        loser = None
        try:
            enable("vB")
        except A.HTTPException as e:
            loser = e
        assert loser is not None and loser.status_code == 409 and "启用争用" in str(loser.detail)
    finally:
        release.set()
        th.join(timeout=10)
        A._ENABLE_HOOK = None
        A._ENABLE_LOCK_TIMEOUT = 2.0
    assert "err" not in winner and winner["res"]["official"] == "vA", winner
    assert [o["version"] for o in officials()] == ["vA"]
    assert row_of("vB")["status"] == "停用"
    events = [e["event"] for e in log_items()]
    assert events.count("启用争用") == 1, events
    assert any(e["event"] == "启用争用" and e["version"] == "vB" for e in log_items())


@check("库级硬约束：直插第二条 正式/启用 被唯一索引拦截")
def _():
    conn = sqlite3.connect(A.DB_PATH)
    try:
        try:
            conn.execute(
                "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
                " VALUES ('v9','订单入站','严格','正式','启用','x')")
            raise AssertionError("唯一索引缺失：第二条 正式/启用 竟可落库")
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()


@check("旧库双正式并存：启动迁移收编唯一，旧版进只读历史并记「唯一修复」")
def _():
    old_db = Path(tempfile.mkdtemp(prefix="uniq-migrate-")) / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(
        "CREATE TABLE contracts(id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT UNIQUE NOT NULL,"
        " subject TEXT NOT NULL, mode TEXT NOT NULL, track TEXT NOT NULL, status TEXT NOT NULL,"
        " required_fields TEXT NOT NULL);")
    for v, req in (("w1", "order_id,amount,currency"), ("w2", "order_id,amount")):
        conn.execute(
            "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
            " VALUES (?,?,?,?,?,?)", (v, "对账入站", "严格", "正式", "启用", req))
    conn.commit()
    conn.close()

    keep = A.DB_PATH
    try:
        A.DB_PATH = old_db
        A.init_db()
    finally:
        A.DB_PATH = keep
    conn = sqlite3.connect(old_db)
    conn.row_factory = sqlite3.Row
    on = [dict(r) for r in conn.execute(
        "SELECT * FROM contracts WHERE subject='对账入站' AND track='正式' AND status='启用'")]
    assert len(on) == 1 and on[0]["version"] == "w2", on
    w1 = dict(conn.execute("SELECT * FROM contracts WHERE version='w1'").fetchone())
    assert w1["status"] == "历史" and w1["required_fields"] == "order_id,amount,currency", w1
    events = conn.execute("SELECT event, version FROM official_events ORDER BY id").fetchall()
    assert ("唯一修复", "w1") in [(e, v) for e, v in events], events
    try:
        conn.execute(
            "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
            " VALUES ('w9','对账入站','严格','正式','启用','x')")
        raise AssertionError("迁移后唯一索引未生效")
    except sqlite3.IntegrityError:
        pass
    conn.close()


@check("启用落库可核：重跑 startup（init_db）后正式仍是指定版本")
def _():
    A.init_db()  # 模拟重启
    assert [o["version"] for o in officials()] == ["vA"]
    items = A.contracts(authorization=AUTH)["items"]
    enabled = [i for i in items if i["subject"] == "订单入站"
               and i["track"] == "正式" and i["status"] == "启用"]
    assert [i["version"] for i in enabled] == ["vA"]


@check("版本目录只留一正式；入站/对账/缓冲/唯一册各页稳定有序")
def _():
    items = A.contracts(authorization=AUTH)["items"]
    per = {}
    for i in items:
        if i["track"] == "正式" and i["status"] == "启用":
            per.setdefault(i["subject"], []).append(i["version"])
    assert per and all(len(v) == 1 for v in per.values()), per
    assert A.ingress(authorization=AUTH)["items"], "入站列表应可读"
    recon = A.reconcile(authorization=AUTH)
    assert recon["aligned"] is True and recon["ingress_total"] >= 1, recon
    A.buffers(authorization=AUTH)
    ids = [e["id"] for e in log_items()]
    assert ids == sorted(ids), ids


print()
if FAILURES:
    print(f"离线核验未过：{len(FAILURES)} 项 -> {FAILURES}")
    sys.exit(1)
print("离线核验全部通过：正式启用唯一（乙路径·原子替换）成立")

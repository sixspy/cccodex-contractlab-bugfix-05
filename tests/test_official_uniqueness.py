"""正式启用唯一（乙路径·原子替换）验收测试。

口径（选定后写入正式唯一册，不再改口）：
  - 每主题 正式/启用 至多一条；新启用一次原子替换现行指针，旧正式进只读历史；
  - 替换不得丢掉必填：必填为空的版本拒绝启用，旧正式的必填字段原样保留；
  - 偏离正式唯一册的请求拒绝并记「唯一偏离」；
  - 同主题并发启用排队，后到只记「启用争用」；
  - 版本目录只留一正式，入站/对账/缓冲/唯一册各页稳定。
"""
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app, init_db

# 修复前表结构（无唯一索引、无 official_events），用于迁移测试
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


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr("app.DB_PATH", db)
    init_db()
    yield db


def _auth(c):
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _add_contract(db, version, subject="订单入站", mode="严格", track="正式",
                  status="停用", required="order_id,amount"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
        " VALUES (?,?,?,?,?,?)", (version, subject, mode, track, status, required))
    conn.commit()
    conn.close()


def _officials(db, subject="订单入站"):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM contracts WHERE subject=? AND track='正式' AND status='启用'"
        " ORDER BY id", (subject,)).fetchall()]
    conn.close()
    return rows


def _row(db, version):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM contracts WHERE version=?", (version,)).fetchone())
    conn.close()
    return row


def test_enable_replaces_official_atomically(_db):
    """乙路径：新启用一次替换现行指针，旧正式进只读历史，必填不丢，命中稳定。"""
    c = TestClient(app)
    h = _auth(c)
    _add_contract(_db, "v4", required="order_id,amount,currency,channel")

    r = c.post("/api/contracts/enable", headers=h, json={"version": "v4"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["official"] == "v4" and body["replaced"] == "v3"

    # 版本目录只留一正式
    officials = _officials(_db)
    assert [o["version"] for o in officials] == ["v4"]
    # 旧正式进只读历史，且必填字段原样保留（替换没丢必填）
    old = _row(_db, "v3")
    assert old["status"] == "历史"
    assert old["required_fields"] == "order_id,amount,currency"
    # 入站命中稳定指向新正式
    r = c.post("/api/ingress/validate", headers=h,
               json={"subject": "订单入站", "payload_fields": "order_id,amount"})
    assert r.json()["result"] == "拒收" and r.json()["version"] == "v4"
    # 正式唯一册记下替换事件
    log = c.get("/api/contracts/official-log", headers=h).json()["items"]
    assert any(e["event"] == "启用替换" and e["version"] == "v4" for e in log)
    # 重复启用同一版本幂等，不再改口、不重复记册
    n = len(log)
    r2 = c.post("/api/contracts/enable", headers=h, json={"version": "v4"})
    assert r2.status_code == 200 and r2.json().get("already") is True
    assert len(c.get("/api/contracts/official-log", headers=h).json()["items"]) == n


def test_enable_first_official_for_subject(_db):
    """无现行正式的主题：停用版本可直接启用，记「正式启用」。"""
    c = TestClient(app)
    h = _auth(c)
    r = c.post("/api/contracts/enable", headers=h, json={"version": "v2"})  # 库存入站·停用
    assert r.status_code == 200 and r.json()["replaced"] is None
    assert [o["version"] for o in _officials(_db, "库存入站")] == ["v2"]
    ok = c.post("/api/ingress/validate", headers=h,
                json={"subject": "库存入站", "payload_fields": "sku,qty"})
    assert ok.json()["result"] == "通过" and ok.json()["version"] == "v2"
    log = c.get("/api/contracts/official-log", headers=h).json()["items"]
    assert any(e["event"] == "正式启用" and e["version"] == "v2" for e in log)


def test_enable_rejects_empty_required_fields(_db):
    """必填为空 → 拒绝并记「唯一偏离」，现行正式不动（禁止替换丢掉必填）。"""
    c = TestClient(app)
    h = _auth(c)
    _add_contract(_db, "v5-empty", required="")
    r = c.post("/api/contracts/enable", headers=h, json={"version": "v5-empty"})
    assert r.status_code == 409 and "必填" in r.json()["detail"]
    assert [o["version"] for o in _officials(_db)] == ["v3"]
    assert _row(_db, "v5-empty")["status"] == "停用"
    log = c.get("/api/contracts/official-log", headers=h).json()["items"]
    assert any(e["event"] == "唯一偏离" and e["version"] == "v5-empty" for e in log)


def test_enable_rejects_shadow_track_and_unknown(_db):
    """影子轨不得启用为正式 → 拒绝并记「唯一偏离」；版本不存在 → 404。"""
    c = TestClient(app)
    h = _auth(c)
    r = c.post("/api/contracts/enable", headers=h, json={"version": "v4-shadow"})
    assert r.status_code == 409 and "唯一偏离" in r.json()["detail"]
    assert _row(_db, "v4-shadow")["status"] == "试跑"
    assert [o["version"] for o in _officials(_db)] == ["v3"]
    log = c.get("/api/contracts/official-log", headers=h).json()["items"]
    assert any(e["event"] == "唯一偏离" and e["version"] == "v4-shadow" for e in log)
    assert c.post("/api/contracts/enable", headers=h,
                  json={"version": "v-nothing"}).status_code == 404


def test_concurrent_enable_same_subject_records_contention(_db, monkeypatch):
    """同主题两名值班同时抢启用：排队，后到只记「启用争用」，正式仍唯一。"""
    c = TestClient(app)
    h = _auth(c)
    _add_contract(_db, "vA", required="order_id,amount,currency")
    _add_contract(_db, "vB", required="order_id,amount")

    entered = threading.Event()
    release = threading.Event()

    def hook(subject):
        entered.set()
        release.wait(timeout=10)

    monkeypatch.setattr(app_module, "_ENABLE_HOOK", hook)
    monkeypatch.setattr(app_module, "_ENABLE_LOCK_TIMEOUT", 0.3)

    winner = {}

    def t1():
        try:
            winner["res"] = app_module.enable_contract(
                app_module.EnableIn(version="vA"), authorization=h["Authorization"])
        except Exception as e:  # pragma: no cover
            winner["err"] = e

    th = threading.Thread(target=t1)
    th.start()
    assert entered.wait(timeout=5), "先到启用应已持锁"

    loser_err = None
    try:
        app_module.enable_contract(
            app_module.EnableIn(version="vB"), authorization=h["Authorization"])
    except app_module.HTTPException as e:
        loser_err = e
    release.set()
    th.join(timeout=10)

    assert "err" not in winner and winner["res"]["official"] == "vA"
    assert loser_err is not None and loser_err.status_code == 409
    assert "启用争用" in loser_err.detail
    # 正式唯一：只有 vA；vB 未启用
    assert [o["version"] for o in _officials(_db)] == ["vA"]
    assert _row(_db, "vB")["status"] == "停用"
    # 争用只记一次，且记的是后到者 vB
    log = c.get("/api/contracts/official-log", headers=h).json()["items"]
    assert [e["event"] for e in log].count("启用争用") == 1
    assert any(e["event"] == "启用争用" and e["version"] == "vB" for e in log)


def test_dual_official_blocked_at_db_level(_db):
    """库级硬约束：绕过接口直插第二条 正式/启用 → 唯一索引拦截。"""
    conn = sqlite3.connect(_db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
            " VALUES ('v9','订单入站','严格','正式','启用','x')")
    conn.close()
    assert [o["version"] for o in _officials(_db)] == ["v3"]


def test_migration_collapses_existing_dual_official(tmp_path, monkeypatch):
    """旧库已双正式并存：启动迁移收编为唯一正式，旧的进只读历史并记「唯一修复」。"""
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript(OLD_DDL)
    for v, req in (("v3", "order_id,amount,currency"), ("v3b", "order_id,amount")):
        conn.execute(
            "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
            " VALUES (?,?,?,?,?,?)", (v, "订单入站", "严格", "正式", "启用", req))
    conn.commit()
    conn.close()

    monkeypatch.setattr(app_module, "DB_PATH", old)
    init_db()

    officials = _officials(old)
    assert len(officials) == 1 and officials[0]["version"] == "v3b"  # 留最新一版
    demoted = _row(old, "v3")
    assert demoted["status"] == "历史" and demoted["required_fields"] == "order_id,amount,currency"

    conn = sqlite3.connect(old)
    events = conn.execute("SELECT event, version FROM official_events ORDER BY id").fetchall()
    assert ("唯一修复", "v3") in events
    with pytest.raises(sqlite3.IntegrityError):  # 迁移后索引生效
        conn.execute(
            "INSERT INTO contracts(version,subject,mode,track,status,required_fields)"
            " VALUES ('v9','订单入站','严格','正式','启用','x')")
    conn.close()


def test_enable_persists_across_restart(_db):
    """启用落库可核：重开连接、重启应用（重跑 startup）后正式仍是指定版本。"""
    c = TestClient(app)
    h = _auth(c)
    _add_contract(_db, "v4", required="order_id,amount,currency,channel")
    assert c.post("/api/contracts/enable", headers=h, json={"version": "v4"}).status_code == 200

    assert [o["version"] for o in _officials(_db)] == ["v4"]  # 新连接直查库
    with TestClient(app) as c2:  # 重启：startup 重跑 init_db，状态不丢
        items = c2.get("/api/contracts", headers=_auth(c2)).json()["items"]
    enabled = [i for i in items if i["subject"] == "订单入站"
               and i["track"] == "正式" and i["status"] == "启用"]
    assert [i["version"] for i in enabled] == ["v4"]


def test_catalog_and_ledgers_stable(_db):
    """版本目录只留一正式；入站列表、对账账册、缓冲、正式唯一册各页稳定有序。"""
    c = TestClient(app)
    h = _auth(c)
    _add_contract(_db, "v4", required="order_id,amount,currency,channel")
    c.post("/api/contracts/enable", headers=h, json={"version": "v4"})
    c.post("/api/ingress/validate", headers=h,
           json={"subject": "订单入站", "payload_fields": "order_id,amount,currency,channel"})

    items = c.get("/api/contracts", headers=h).json()["items"]
    per_subject = {}
    for i in items:
        if i["track"] == "正式" and i["status"] == "启用":
            per_subject.setdefault(i["subject"], []).append(i["version"])
    assert all(len(v) == 1 for v in per_subject.values())

    assert c.get("/api/ingress", headers=h).status_code == 200
    recon = c.get("/api/reconcile", headers=h).json()
    assert recon["aligned"] is True and recon["ingress_total"] >= 1
    assert c.get("/api/buffers", headers=h).status_code == 200

    log = c.get("/api/contracts/official-log", headers=h).json()["items"]
    assert [e["id"] for e in log] == sorted(e["id"] for e in log)  # 册按序可核

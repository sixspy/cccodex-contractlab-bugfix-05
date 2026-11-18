
import hashlib, secrets, sqlite3, threading
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "入站契约"
SKELETON = "Schema 契约版本约束入站校验，破坏性变更进缓冲或拒收，并与落库结果对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS contracts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          subject TEXT NOT NULL,
          mode TEXT NOT NULL,
          track TEXT NOT NULL,
          status TEXT NOT NULL,
          required_fields TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingress_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          payload_fields TEXT NOT NULL,
          contract_version TEXT,
          result TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS buffers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL,
          reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS official_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          version TEXT,
          event TEXT NOT NULL,
          actor TEXT,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"] == 0:
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v3","订单入站","严格","正式","启用","order_id,amount,currency"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v4-shadow","订单入站","兼容","影子","试跑","order_id,amount,currency,channel"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v2","库存入站","严格","正式","停用","sku,qty"))
        collapse_dual_officials(conn)
        # 库级硬约束：同一主题 正式/启用 至多一条，从根上禁止双正式并存
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uniq_official_enabled_per_subject
                        ON contracts(subject) WHERE track='正式' AND status='启用'""")

def collapse_dual_officials(conn):
    """存量双正式并存收编：每主题只留最新一版正式启用，其余进只读历史并记「唯一修复」。"""
    dup = conn.execute("""SELECT subject FROM contracts
                          WHERE track='正式' AND status='启用'
                          GROUP BY subject HAVING COUNT(*)>1""").fetchall()
    for row in dup:
        subject = row["subject"]
        rows = conn.execute("""SELECT id, version FROM contracts
                               WHERE subject=? AND track='正式' AND status='启用'
                               ORDER BY id""", (subject,)).fetchall()
        keep = rows[-1]
        for old in rows[:-1]:
            conn.execute("UPDATE contracts SET status='历史' WHERE id=?", (old["id"],))
            record_event(conn, subject, old["version"], "唯一修复", "系统",
                         f"收编双正式并存：{old['version']} 进只读历史，现行正式留 {keep['version']}")

def record_event(conn, subject, version, event, actor, detail):
    conn.execute("INSERT INTO official_events(subject,version,event,actor,detail,created_at)"
                 " VALUES (?,?,?,?,?,?)",
                 (subject, version, event, actor, detail,
                  datetime.now().isoformat(timespec="seconds")))

@app.on_event("startup")
def _s():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "contracts": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/contracts")
def contracts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM contracts ORDER BY id").fetchall()]}

# ---- 正式启用唯一（乙路径·原子替换）----
# 口径：每主题 正式/启用 至多一条；新启用以一次事务替换现行指针，旧正式进只读历史；
# 替换不得丢掉必填；偏离正式唯一册的请求拒绝并记「唯一偏离」；
# 同主题并发启用按主题加锁排队，后到只记「启用争用」。选定此径，不再改口。
_ENABLE_LOCKS: dict[str, threading.Lock] = {}
_ENABLE_LOCKS_GUARD = threading.Lock()
_ENABLE_LOCK_TIMEOUT = 2.0   # 排队上限（秒），超时视为并发争用
_ENABLE_HOOK = None          # 测试挂钩：持锁后、事务前调用

class EnableIn(BaseModel):
    version: str

def _lock_for(subject: str) -> threading.Lock:
    with _ENABLE_LOCKS_GUARD:
        return _ENABLE_LOCKS.setdefault(subject, threading.Lock())

@app.post("/api/contracts/enable")
def enable_contract(body: EnableIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with connect() as c:
        target = c.execute("SELECT * FROM contracts WHERE version=?", (body.version,)).fetchone()
    if not target:
        raise HTTPException(404, "版本不存在")
    subject = target["subject"]

    lock = _lock_for(subject)
    if not lock.acquire(timeout=_ENABLE_LOCK_TIMEOUT):
        # 同主题另有启用进行中：后到者不启用，只记启用争用
        with connect() as c:
            record_event(c, subject, body.version, "启用争用", user,
                         "同主题并发启用未获锁，后到仅记争用")
        raise HTTPException(409, "启用争用：同主题另有启用进行中，本次仅记录")
    try:
        hook = _ENABLE_HOOK
        if hook:
            hook(subject)
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("SELECT * FROM contracts WHERE version=?", (body.version,)).fetchone()
            if not cur:
                conn.rollback()
                raise HTTPException(404, "版本不存在")
            if cur["track"] != "正式":
                record_event(conn, subject, body.version, "唯一偏离", user,
                             f"{body.version} 属{cur['track']}轨，不得启用为正式")
                conn.commit()
                raise HTTPException(409, "唯一偏离：非正式轨不得启用为正式")
            if not (cur["required_fields"] or "").strip():
                record_event(conn, subject, body.version, "唯一偏离", user,
                             f"{body.version} 必填字段为空，禁止替换丢掉必填")
                conn.commit()
                raise HTTPException(409, "唯一偏离：必填字段为空，拒绝启用")
            if cur["status"] == "启用":
                conn.commit()  # 已是现行正式：幂等，当天不改口、不重复记册
                return {"ok": True, "already": True, "subject": subject,
                        "official": body.version, "replaced": None}
            old = conn.execute("""SELECT version FROM contracts
                                  WHERE subject=? AND track='正式' AND status='启用'""",
                               (subject,)).fetchone()
            # 一次事务内完成指针替换：旧正式进只读历史，新版启用
            conn.execute("""UPDATE contracts SET status='历史'
                            WHERE subject=? AND track='正式' AND status='启用'""", (subject,))
            conn.execute("UPDATE contracts SET status='启用' WHERE version=?", (body.version,))
            if old:
                record_event(conn, subject, body.version, "启用替换", user,
                             f"乙路径·原子替换：{old['version']} → {body.version}，旧正式进只读历史")
            else:
                record_event(conn, subject, body.version, "正式启用", user,
                             f"乙路径·原子替换：{body.version} 为本主题首个正式启用")
            conn.commit()
            return {"ok": True, "subject": subject, "official": body.version,
                    "replaced": old["version"] if old else None}
        except HTTPException:
            if conn.in_transaction:
                conn.rollback()
            raise
        except sqlite3.IntegrityError:
            if conn.in_transaction:
                conn.rollback()
            with connect() as c2:
                record_event(c2, subject, body.version, "唯一偏离", user,
                             "正式启用唯一索引拦截双正式并存")
            raise HTTPException(409, "唯一偏离：正式启用唯一约束拦截")
        finally:
            conn.close()
    finally:
        lock.release()

@app.get("/api/contracts/official-log")
def official_log(authorization: str | None = Header(default=None)):
    """正式唯一册：启用/替换/争用/偏离/修复全量按序可查。"""
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM official_events ORDER BY id").fetchall()]}

class IngressIn(BaseModel):
    subject: str
    payload_fields: str  # comma fields present

@app.post("/api/ingress/validate")
def validate(body: IngressIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    fields = {x.strip() for x in body.payload_fields.split(",") if x.strip()}
    with connect() as c:
        ctr = c.execute("SELECT * FROM contracts WHERE subject=? AND track='正式' AND status='启用' ORDER BY id DESC LIMIT 1", (body.subject,)).fetchone()
        if not ctr:
            raise HTTPException(404, "无启用正式契约")
        need = {x.strip() for x in ctr["required_fields"].split(",") if x.strip()}
        missing = sorted(need - fields)
        now = datetime.now().isoformat(timespec="seconds")
        if missing:
            if ctr["mode"] == "兼容":
                cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                                (body.subject, body.payload_fields, ctr["version"], "缓冲", "缺字段:"+ ",".join(missing), now))
                c.execute("INSERT INTO buffers(batch_id,reason) VALUES (?,?)", (cur.lastrowid, "缺字段进双写缓冲"))
                return {"result":"缓冲", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
            cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                            (body.subject, body.payload_fields, ctr["version"], "拒收", "缺字段:"+ ",".join(missing), now))
            return {"result":"拒收", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
        cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                        (body.subject, body.payload_fields, ctr["version"], "通过", "字段齐全", now))
        return {"result":"通过", "version": ctr["version"], "batch_id": cur.lastrowid}

@app.get("/api/ingress")
def ingress(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM ingress_batches ORDER BY id DESC").fetchall()]}

@app.get("/api/buffers")
def buffers(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT b.*, i.subject, i.payload_fields FROM buffers b JOIN ingress_batches i ON i.id=b.batch_id ORDER BY b.id DESC""").fetchall()
        return {"items":[dict(r) for r in rows]}

@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        total = c.execute("SELECT COUNT(*) c FROM ingress_batches").fetchone()["c"]
        passed = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='通过'").fetchone()["c"]
        buffered = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='缓冲'").fetchone()["c"]
        rejected = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='拒收'").fetchone()["c"]
        buf_rows = c.execute("SELECT COUNT(*) c FROM buffers").fetchone()["c"]
    return {"ingress_total": total, "passed": passed, "buffered": buffered, "rejected": rejected, "buffer_rows": buf_rows, "aligned": buffered == buf_rows}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

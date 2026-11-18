# 入站契约

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

Schema 契约版本约束入站校验，破坏性变更进缓冲或拒收，并与落库结果对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 入站契约 |
| 目录 | `05-contractlab` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10730 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

字段增删改类型、宽严模式、影子契约、缓冲窗、生产者消费者双视角

## 避开

别做成质检签发换皮；别做成纯表单 CRUD

## 互斥切口候选

- 破坏性变更直接拒收 vs 双写缓冲
- 影子只读试跑 vs 双轨分开展示
- 契约回滚整版原子切换 vs 按生产者摘流

## 正式启用唯一（乙路径·原子替换）

同一主题 `正式/启用` 至多一条：新启用以一次事务替换现行指针，旧正式进只读历史；
必填为空或影子轨启用被拒并记「唯一偏离」；同主题并发启用排队，后到只记「启用争用」。
库级部分唯一索引兜底，存量双正式启动时自动收编（记「唯一修复」）。
接口：`POST /api/contracts/enable`、`GET /api/contracts/official-log`（正式唯一册）。
复现与核验见 `fixtures/README.md`。

## 启动（一键）

`powershell
cd projects/05-contractlab
docker compose up -d --build
`

浏览器：http://127.0.0.1:10730/  
健康检查：GET http://127.0.0.1:10730/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10602
python -m pytest -q
`

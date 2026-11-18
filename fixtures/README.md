# 双正式并存夹具与正式唯一核验

## 缺陷

同一主题两版 `正式/启用` 并存时：版本目录自相矛盾；`/api/ingress/validate`
靠 `ORDER BY id DESC LIMIT 1` 悄悄挑一条，命中随物理插入顺序翻转（命中不稳）。

## 复现（修复前行为）

```bash
python3 -I fixtures/repro_dual_official.py
```

脚本按修复前表结构（无唯一索引）建临时库，套用 `dual_official_seed.sql`
塞入第二个 `正式/启用` 版本 v3b，演示：同一逻辑状态下仅改变插入顺序，
命中版本在 v3b ↔ v3 间翻转，且目录同时挂两条正式。

## 修复口径（选定乙路径，写入正式唯一册，不再改口）

**乙路径·原子替换**：新启用以一次事务替换现行指针，旧正式进只读历史（`status='历史'`）。
不混用甲路径（拒绝第二次）。

- 库级硬约束：部分唯一索引 `uniq_official_enabled_per_subject`
  （`subject` 唯一，`WHERE track='正式' AND status='启用'`），从根上禁止双正式并存；
- 旧库迁移：启动时收编存量双正式，留最新一版，其余进只读历史并记「唯一修复」；
- 禁止替换丢掉必填：必填为空的版本拒绝启用并记「唯一偏离」，旧正式必填原样保留；
- 偏离正式唯一册（如影子轨启用为正式）→ 拒绝并记「唯一偏离」；
- 同主题并发启用按主题加锁排队，后到者只记「启用争用」；
- 正式唯一册 `official_events`：`GET /api/contracts/official-log` 按序可查；
  启用接口 `POST /api/contracts/enable`（body: `{"version": "..."}`）。

## 核验

```bash
python -m pytest -q                                  # 正式用例（需 fastapi/pytest）
python3 -I fixtures/check_uniqueness_offline.py      # 离线核验（仅标准库+最小桩）
```

`check_uniqueness_offline.py` 借 `fixtures/stubs/` 的最小 fastapi/pydantic 桩导入
真实 `backend/app.py` 直接调端点函数，核验与 pytest 用例相同的不变量；
`fixtures/stubs/` 不是正式依赖，不参与部署。

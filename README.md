# Curtainclock（幕窗钟）

骨架：**批窗定义 → 算力槽/执行器资源依赖就绪 → 开窗租约占用 → 关窗释放与冲突水位互证**

- 前端：http://127.0.0.1:6100 （React + Vite）
- API：http://127.0.0.1:11100

## 并发口径（全局唯一）：开窗写锁内一次裁落

主槽冲突后走**候补降级**（本题默认允许降级占用，不裁决硬拒绝/降级总开关）。
候补防并发双占采用 **写锁臂**：

- 不设预占表。所有占用类写入都经 `app.db.transaction()` 的 `BEGIN IMMEDIATE` 全局写锁。
- `open_lease` 在同一把写锁、同一连接、同一事务内完成：读批窗 → **类型门禁裁决** →
  就绪复检 → 按优先级（`standby_priority` 升序、`capacity` 降序、`code` 升序）扫同主题候补 →
  直接插入正式 `open` 租约。**检查与插入同锁；锁外不先读后写。**
- 后到者在自己的 `BEGIN IMMEDIATE` 拿到锁时，必然看到先到者已提交的 open，
  候补扫描会将其排除，因此并发两路降级抢同一候补**不会双 open**：
  后到者改选下一个空闲候补，候补耗尽则失败（409，可查原因）。
- 数据库层兜底（第二道防线）：`ux_leases_one_open_per_window` 唯一索引 +
  `trg_lease_slot_overlap` / `trg_lease_executor_overlap` 触发器，
  即便应用层退化也不会留下双开。
- 水位与实槽同源互证：`open_leases`/`conflict_count` 水位应恒等于实际计数，
  且 `live_sessions` 与 open 租约数一致；`/api/dashboard` 返回
  `watermarks_consistent` 供页面显示。

候补池在 seed 中给出：`S-ETL-SB1`（优先级 10）、`S-ETL-SB2`（20）、`S-BI-SB1`（10）。

## 类型门禁口径（全局唯一）：以类型裁决回执为准

两架构取其一贯彻，本仓库选定 **「以类型裁决回执为准」**（另一架构「以已落库占用为准」
不采用）。类型裁决与占用落库同窗同事务，禁止回执与占用索引各说各话：

- **能力集合** = 与批窗同 `subject` 且启用中的执行器（`code` 升序，确定性落点）。
  裁决函数 `ready.type_adjudication` 全局唯一：类型预检 API
  （`GET /api/windows/{code}/type-check`，纯读不落库）与开窗写锁事务共用同一口径。
- **拒绝则不得留成功腿**：指定执行器不在能力集合（类型不匹配/未启用）→ 409 硬拒；
  指定被占且集合内无空闲替身 → 409。拒绝只落冲突回执（`conflicts` 行 +
  `conflict_count` 水位），**不得**留下 open 租约、执行器/槽位占用、
  `open_leases` 水位加、批窗 live 态；插入期异常由事务整体回滚，已泄漏必回滚。
- **成功则落点必在集合内**：指定执行器被占时走**类型链替身**——取集合内第一个
  空闲执行器落点；落点写进租约可查（`executor_code`=落点、
  `designated_executor_code`=指定、`exec_substituted`=1），租约页执行器列同刷。
- **交错不得半截双态**：裁决、候补扫描、open 落库在同一写锁事务内一次完成，
  成功腿与回执同真同假；并发双跑被 `BEGIN IMMEDIATE` 串行化，一拒一漏必收敛。
- **差异桶对账**：`GET /api/type-gate/differences` 实时核对「拒绝回执与占用并存 /
  落点越出能力集合 / 批窗态与租约态撕裂 / 水位不一致」；冲突页展示差异桶，
  **差异未清前总览类型门禁徽章不得显示「通过」**（`/api/dashboard` 的
  `type_gate.status`）。
- 能力矩阵页（`GET /api/capability-matrix`）与执行器占用索引
  （`GET /api/executors`，含 open 租约）同源于租约表，互证可核。

## 本地冒烟（推荐）

```powershell
cd D:\Asolo11\claude-workspace\curtainclock\source
# 本机若无 3.11：py -V:Astral/CPython3.11.15 -m venv .venv
py -V:Astral/CPython3.11.15 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
$env:PYTHONPATH = "$PWD\backend"
Start-Process .\.venv\Scripts\uvicorn.exe -ArgumentList 'app.main:app','--host','127.0.0.1','--port','11100' -WorkingDirectory "$PWD\backend"
cd frontend
# 前端需 Node ≥18；不足时用 docker compose 起 web
npm install
npm run dev
```

浏览器打开 http://127.0.0.1:6100 ：开一节课 → 同算力槽冲突 → 关窗释放。

### 自检脚本

- `_smoke_api.py`：主槽开窗 → 同算力槽冲突**降级到候补** → 关窗，水位一致。
- `_concurrency_check.py`：多路并发降级抢同一候补池，验证「成功数=空闲候补数、
  失败可查、无双重占、同窗至多一笔 open」。
- `_stress_check.py`：多轮高并发压测，验证无 5xx、水位始终一致。
- `_type_gate_check.py`：类型门禁四夹具——409 不留 open、替身落点不越出
  能力集合、交错无半截双态、并发一拒一漏收敛；全程差异桶为空。

四脚本都只依赖标准库，对运行中的 API 发请求：

```bash
python _smoke_api.py                      # 默认连 11100
python _concurrency_check.py http://127.0.0.1:11100/api
python _stress_check.py      http://127.0.0.1:11100/api
python _type_gate_check.py   http://127.0.0.1:11100/api
```

可用环境变量 `CURTAINCLOCK_DB` 把后端指向独立库文件，便于并行自检互不干扰。

## Docker

```powershell
docker compose up -d --build
```

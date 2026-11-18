# 途迹（Hoptrace）

轨迹点装载 → 运单时间线物化 → 迟到旁路与脏点隔离 → 状态证明互证。

**不是** Feed 归并、CDC 游标 Sink、质检签发、数仓分层、交班过户、正冲净额。  
**也不是** 电商订单 / 仓库库存 / 外卖工单 CRUD——去皮后是事件装载与有序物化。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10800

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5800 |
| 接口 | http://127.0.0.1:10800 |

## UI

三栏测绘板：顶品牌条 + 中竖轴时间线 + 右检查器 + 底 hop 点阵。  
不使用左轨编号导航、不使用顶栏流程 ribbon。

## 冷热分层（hot_window_cold_archive）

- **热窗口**：按「最大保留点数」限定——每条运单正式有效轴（`channel='primary' AND retracted=0`）只留最新 N 点在热段，溢出点入冷归档；未配置运单默认 50。正式轴查询默认只含热段（`?include_cold=true` 可只读带出冷点）。
- **落库**：`hops.is_cold` + `hops.archive_hash` 同表冷标记/段哈希列，`cold_segments` 切窗台账，`hot_windows` 窗口配置；SQLite 触发器锁死冷点，禁止装载/更正改写。
- **归档路径**：装载批次与调窗接口均在**同事务**内切窗，失败整体回滚，不留「标冷未写哈希」或「热轴含应归档点」中间态。
- **接口**：`GET /api/waybills/{id}/hotcold-proof`（冷热证明：窗口参数/热点数/冷点数/冷段哈希）、`PUT /api/waybills/{id}/hot-window`（调窗切窗）、`GET /api/hot-windows`。
- **验证**：测绘板检查器展示热/冷计数与冷段哈希；调小窗口使夹具点溢出，冷段不可变、热段仍可追加。`cd backend && python -m pytest tests/`。

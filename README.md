# 套书包装单位换算 API

同一套书换包装后，出版社汇总印厂发运流水时，需要按发运当天的有效规则，
把一件货折成若干盒或套。本服务用 **FastAPI + SQLite** 实现换算规则的
批量报送、核定与精确倍率查询。

## 业务规则

- 按**套书品项（item_code）** 批量报送规则，每条规则包含：
  编号、有效起止日（首尾日均生效）、等量关系（如 `1 件 = 8 盒`）。
- 等量关系**双向可换算**：既能查 1 件等于多少盒，也能查 1 盒等于多少件。
- 同一天、同一对包装单位，沿任意有效路径（可多跳，如 件→盒→套）
  必须得到**同一个精确倍率**。倍率用整数分数（分子/分母）存储与比较，
  不使用浮点数。
- 新规则与**在用规则一起核定**；发现冲突则**整批退回**，返回：
  - 最早冲突日；
  - 两条换算路径各自经过的规则编号与倍率。
- 核定**不逐日展开**：按规则起止日做事件扫描（sweep line），
  只在有效规则集合发生变化的时间区间上检查一次。
- 两批**同时送审**：写事务以 `BEGIN IMMEDIATE` 串行化，
  结果与按某个顺序依次提交完全一致（一批接受、另一批基于最新数据核定）。
- 按品项、发运日、源单位、目标单位查询已生效的精确倍率；
  无可用换算路径时返回 `available=false` 和中文提示。

## 目录结构

```
app/
  rationals.py    # 精确有理数（约分、乘、倒数）
  validation.py   # 扫描线核定 + 带权并查集 + BFS 路径重建
  graph.py        # 查询日有效规则的图搜索（BFS 最短路径）
  database.py     # SQLite 连接、表结构、事务、数据访问
  schemas.py      # Pydantic 请求/响应模型（含输入校验）
  services.py     # 报送、核定、查询的业务编排
  main.py         # FastAPI 路由与 /docs
tests/            # pytest：19 个用例（单元 + API + 并发）
scripts/
  e2e_smoke.sh    # 对运行中服务的端到端冒烟脚本
Dockerfile
docker-compose.yml
```

## 运行方式

### 方式一：Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8000
# 交互式文档：http://localhost:8000/docs
```

SQLite 数据文件保存在命名卷 `conversion-data` 的 `/data/conversion.db`。

### 方式二：本地 Python（3.11+）

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
DB_PATH=data/conversion.db .venv/bin/uvicorn app.main:app --reload
```

## 接口

启动后在 `/docs`（Swagger UI）可直接填写并调用。

### 1. 批量报送并核定 `POST /api/v1/batches`

```bash
curl -X POST http://localhost:8000/api/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "item_code": "SET-001",
    "rules": [
      {"rule_code": "R1", "from_unit": "件", "to_unit": "盒",
       "from_amount": 1, "to_amount": 8,
       "start_date": "2026-01-01", "end_date": "2026-12-31"},
      {"rule_code": "R2", "from_unit": "盒", "to_unit": "套",
       "from_amount": 1, "to_amount": 6,
       "start_date": "2026-01-01", "end_date": "2026-12-31"}
    ]
  }'
```

冲突时整批退回，响应示例（节选）：

```json
{
  "status": "rejected",
  "conflict": {
    "conflict_date": "2026-03-01",
    "from_unit": "件", "to_unit": "套",
    "path_a": {"units": ["件", "盒", "套"],
               "rule_codes": ["R1", "R2"], "factor": "48/1"},
    "path_b": {"units": ["件", "套"],
               "rule_codes": ["R3"], "factor": "12/1"}
  }
}
```

### 2. 查询精确倍率 `GET /api/v1/conversion`

```bash
curl -G http://localhost:8000/api/v1/conversion \
  --data-urlencode "item_code=SET-001" \
  --data-urlencode "shipping_date=2026-06-01" \
  --data-urlencode "from_unit=件" \
  --data-urlencode "to_unit=套"
# factor = "48/1"，反向查询 套→件 得 "1/48"
```

### 3. 其他

- `GET /api/v1/rules?item_code=...`：列出某品项全部已生效规则。
- `GET /api/v1/submissions?item_code=...`：送审批次审计记录（含退回原因）。
- `GET /health`：健康检查（容器 HEALTHCHECK 也用它）。

## 核定算法要点

1. **事件扫描线**：把每条规则的起日、止日次日作为事件点，
   时间轴切成若干区间，每区间内有效规则集合不变，只核定一次。
2. **带权并查集（DSU）**：边权为精确分数。新规则两端若已连通，
   比较已知倍率与新规则倍率；不同即冲突。
3. **路径重建**：维护邻接表，冲突时用 BFS 还原老路径经过的单位与规则编号，
   新规则本身作为第二条单跳路径一并返回。
4. **并发**：每个 HTTP 请求独立连接；写操作 `BEGIN IMMEDIATE`
   立即取 SQLite 写锁（WAL 模式 + busy_timeout），两批并发送审被串行化，
   后到者基于已提交数据核定，不存在丢更新。

## 测试

```bash
.venv/bin/python -m pytest          # 19 个单元/API/并发用例
./scripts/e2e_smoke.sh              # 对运行中的服务做端到端冒烟
```

覆盖的重点场景：

- 基础报送与双向换算、多跳路径（件→盒→套 = 48/1，反向 1/48）；
- **有效期首尾相碰**：新规则起日恰为旧规则止日，当天两条同时有效 → 冲突日就是当天；
  若止日与起日相邻不重叠，则允许不同倍率；
- 冲突整批退回、最早冲突日、两条路径规则编号；
- **同时送审冲突**：并发下始终恰有一批接受、一批退回（含 15 轮真实 HTTP 并发）；
- 无可用换算的中文提示、编号重复退回、非法请求 422、等价写法（2件=16盒）放行。

## 已知限制 / 未完成项

- 本开发环境没有 Docker 守护进程，`Dockerfile` 与 `docker-compose.yml`
  已编写并通过 YAML/语法校验，但**未能在本机实际 `docker compose build` 运行**；
  容器化路径需在有 Docker 的环境验证一次。
- 规则一经接受不提供修改/废止接口；只能通过新批次（新编号、新区间）表达变更。
- 规则编号重复按整批退回处理，不做 upsert。
- 查询返回 BFS 最短跳数路径；当存在多条等倍率路径时不保证固定展示哪一条，
  但倍率精确一致。

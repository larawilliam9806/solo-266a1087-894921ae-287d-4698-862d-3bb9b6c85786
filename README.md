# 浓度汇总 API

汇编河段水质报告用的浓度汇总服务。两份汇总可能收录同一组测量，不同样组也可能
共用空白样；本服务按样组登记原始读数汇总，按当前空白样数据生成校正结果，
并为每次汇总保存当时采用的空白样数据快照。

技术栈：FastAPI + SQLite（Python 标准库 `sqlite3`，无额外 ORM），支持 Docker Compose 启动。

## 业务规则

- **只处理同一指标、同一单位的读数**：一次汇总涉及的所有样组必须指标
  （`analyte`）与单位（`unit`）一致，否则返回 409。
- **样组登记后固定**：按唯一编号登记，提交读数总和、独立测量方差之和、
  各空白样的使用次数；不可修改，重复登记返回 409。
- **每次测量只用一份空白样**：因此一个样组（或一次汇总）的空白样使用次数
  之和即测量总数。
- **空白样可另行登记、更正**：扣减值与方差通过 `POST /blanks` 登记、
  `PUT /blanks/{id}` 更正。样组登记时不要求空白样已存在。
- **汇总生成**：凭样组号、已有汇总号或两者组合生成新汇总；重叠样组只计一次。
  引用旧汇总时展开为其样组集合，按**当前**空白样数据重新计算。
- **续算优化**：请求只引用一份汇总时，直接读取该底稿的成员编号与存量聚合
  （读数总和、独立测量方差之和、空白样用量），逐组统计只从去重后**新增**的
  样组读取，不展开底稿样组；引用多份汇总时仍按实际样组展开去重。存量聚合以
  精确有理数分量保存，续算与“同一批样组从头核算”**位级一致**，读数正负抵消时
  亦然。升级前生成的汇总首次成功被引用时按其原组补读一次并落盘，重启后复用。
- **查询旧汇总**：返回原结果与当时采用的空白样数据（快照），不随后续更正改变。
- **资料缺失**：样组号 / 汇总号 / 空白样任一缺失时，返回 404 并列出所缺编号，
  本次不生成汇总。

## 计算公式

设汇总去重后收录若干样组，空白样 `b` 的总使用次数为 `n_b`（各样组使用次数之和），
测量总数 `N = Σ n_b`：

```
校正均值 = ( Σ 读数总和 − Σ_b 扣减值_b × n_b ) / N
总方差   = Σ_b 方差_b × n_b²  +  Σ 独立测量方差之和
标准误   = sqrt(总方差) / N
```

所有求和与乘积以精确有理数（Python `fractions.Fraction`，按浮点登记值的二进制
位值精确表示）合并，仅在写库/返回前正确舍入为 `float`。因此续算结果与用同一批
样组从头核算的结果完全相等，不受分组顺序或读数正负抵消影响。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，交互式文档见 `http://localhost:8000/docs`。
SQLite 数据保存在命名卷 `app-data`（容器内 `/data/app.db`）。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # 默认数据库 ./data/app.db，可用 DB_PATH 覆盖
```

### 测试

```bash
pip install -r requirements-dev.txt
python3 tests/test_api.py          # 原有端到端测试
python3 -m pytest tests/           # 全部测试（含续算优化专项）
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/groups` | 登记样组（登记后固定；重复编号 409） |
| GET | `/groups/{id}` / `/groups` | 查询 / 列出样组 |
| POST | `/blanks` | 登记空白样（重复编号 409） |
| PUT | `/blanks/{id}` | 更正空白样的扣减值与方差 |
| GET | `/blanks/{id}` / `/blanks` | 查询 / 列出空白样当前数据 |
| POST | `/summaries` | 生成新汇总（样组号、汇总号或两者组合） |
| GET | `/summaries/{id}` / `/summaries` | 查询旧汇总（原结果 + 当时空白样数据） |
| GET | `/health` | 健康检查 |

### 登记样组

```bash
curl -X POST localhost:8000/groups -H 'Content-Type: application/json' -d '{
  "group_id": "SG-1", "analyte": "cod", "unit": "mg/L",
  "reading_sum": 105.0, "variance_sum": 4.0,
  "blank_usages": {"BLK-A": 10}
}'
```

### 登记 / 更正空白样

```bash
curl -X POST localhost:8000/blanks -H 'Content-Type: application/json' -d \
  '{"blank_id": "BLK-A", "deduction": 0.5, "variance": 0.01}'

curl -X PUT localhost:8000/blanks/BLK-A -H 'Content-Type: application/json' -d \
  '{"deduction": 0.8, "variance": 0.02}'
```

### 生成汇总

```bash
# 仅凭样组号
curl -X POST localhost:8000/summaries -H 'Content-Type: application/json' -d \
  '{"group_ids": ["SG-1", "SG-2"]}'

# 凭已有汇总号 + 新样组继续合并（按当前空白样数据重算，重叠样组只计一次；
# 只引用一份汇总时底稿不展开，逐组统计只读取新增样组）
curl -X POST localhost:8000/summaries -H 'Content-Type: application/json' -d \
  '{"summary_ids": [1], "group_ids": ["SG-3"]}'
```

响应示例：

```json
{
  "summary_id": 1,
  "analyte": "cod", "unit": "mg/L",
  "group_ids": ["SG-1"],
  "source_group_ids": ["SG-1"], "source_summary_ids": [],
  "total_measurements": 10,
  "reading_sum": 105.0, "variance_sum": 4.0,
  "blank_deduction_total": 5.0,
  "corrected_mean": 10.0,
  "standard_error": 0.223606797749979,
  "blanks": [
    {"blank_id": "BLK-A", "usage_count": 10, "deduction": 0.5, "variance": 0.01}
  ],
  "created_at": "2026-11-21T23:12:23+00:00"
}
```

其中 `blanks` 为生成该汇总时采用的空白样数据快照；之后更正空白样不影响
`GET /summaries/{id}` 的返回，但凭该汇总号继续合并时会按当前数据重算。

### 错误返回

- **404 资料缺失**（不生成汇总）：

```json
{"detail": {"error": "missing_data", "message": "资料缺失，本次不生成汇总。",
            "missing": {"group_ids": ["SG-9"], "blank_ids": ["BLK-X"]}}}
```

- **409 指标 / 单位不一致**：

```json
{"detail": {"error": "inconsistent_analyte_or_unit",
            "message": "只能汇总同一指标、同一单位的读数。",
            "combinations": [{"analyte": "lead", "unit": "ug/L", "group_ids": ["G1"]},
                             {"analyte": "lead", "unit": "mg/L", "group_ids": ["G3"]}]}}
```

- **409 重复登记**（样组 / 空白样编号已存在）；**422 入参校验失败**
  （如方差为负、空白样使用次数为空、汇总请求两项皆空）。

## 项目结构

```
app/
  main.py        FastAPI 应用与全部接口（含续算快路径与精确聚合）
  db.py          SQLite 连接、建表与缓存表迁移
tests/
  test_api.py          端到端测试（覆盖全部原有业务规则）
  test_incremental.py  续算优化专项测试（位级一致、补读、重启复用等）
Dockerfile
docker-compose.yml
requirements.txt
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |

# 机床工位量检规范下发 API

FastAPI + SQLite。检验项目的标称尺寸与上下限限一律以**整数微米**存储与传输。
所有修改都走「批次」:量检员把规范和工位配置放进同一批,先**核对**再**发布**;
工位终端按工位取数,每次拿到**一整份**生效的下发规范。

## 快速开始

### Docker Compose(推荐)

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

SQLite 数据库落在命名卷 `gauge_data`(`/data/specs.db`)。

### 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
# 自测
.venv/bin/python -m pytest tests/ -q
# 演示脚本(服务启动后)
bash scripts/demo.sh
```

## 概念与规则

| 概念 | 说明 |
| --- | --- |
| 检验项目 `Item` | `code`、`name`、`nominal_um`、`upper_um`、`lower_um`(整数微米,且 `lower ≤ nominal ≤ upper`) |
| 工位 `Workstation` | `code`、`name`、终端代际 `terminal_generation`(`new` / `old`) |
| 领用 `Claim` | 工位领用检验项目;按工位下发的规范 = 其领用项目的生效值 |
| 批次 `Batch` | 一组**有序**操作,状态机 `draft → verified → published`,核对不通过为 `rejected` |

**终端代际与下发格式**

- 新终端(`new`):接收标称值 + 上下限(`nominal_um / upper_um / lower_um`),可为非对称公差。
- 旧终端(`old`):只认标称值 + 对称公差,下发为 `nominal_um / tolerance_um`。
- 系统保证:任何时刻的生效状态里,旧终端领用的项目都是对称公差
  (`upper - nominal == nominal - lower`)。

**批次操作(同一批可混放规范与工位配置,按顺序应用)**

| op | 含义 |
| --- | --- |
| `upsert_item` | 新建/更新检验项目规范 |
| `upsert_workstation` | 登记/更新工位(含终端代际) |
| `claim_item` / `unclaim_item` | 工位领用 / 取消领用项目 |

**核对(POST /batches/{id}/verify)**

- 在当前生效状态的副本上模拟应用整批,给出**相关工位改完后将领用的规范预览**;
- 若改完后仍有旧终端领用非对称公差 → 列出(工位, 项目)清单,**整批退回**(`rejected`);
- 否则记录所涉实体的版本指纹,批次进入 `verified`。

**发布(POST /batches/{id}/publish)**

- 仅 `verified` 可发布;发布前比对指纹:
  核对后**所涉规范或工位配置变过、或有人增删过待改项目的领用,即使改回原样**
  (版本号为单调计数器,改回也算变过)→ `409 verification_stale`,批次打回 `draft`,需重新核对;
- **无关改动不影响发布**;
- 发布时还有一道安全网复检,保证生效状态永不出现「旧终端领用非对称公差」;
- 发布事务性落库并**联动换版**:规范/工位配置 `version`、领用关系 `claims_version` 递增。

**按工位取数**:`GET /workstations/{code}/spec` 每次返回一整份生效规范,
`revision` 为内容指纹(内容不变则不变),便于终端比对换版。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 新建批次(可带初始 ops) |
| GET | `/batches` `/batches/{id}` | 批次列表 / 详情(含 ops、违规、所涉实体) |
| PUT | `/batches/{id}/ops` | 整体替换 ops(已核对的会被打回 `draft`) |
| POST | `/batches/{id}/verify` | 核对:违规则整批退回,否则记录指纹 |
| POST | `/batches/{id}/publish` | 发布:指纹失效 `409`;安全网拦截 `422` |
| DELETE | `/batches/{id}` | 作废未发布批次 |
| GET | `/items` `/items/{code}` | 生效规范查询(含版本号、领用工位) |
| GET | `/workstations` `/workstations/{code}` | 工位查询(含代际、领用清单) |
| GET | `/workstations/{code}/spec` | **按工位下发一整份生效规范** |

## /docs 演示步骤

1. `docker compose up --build` 后打开 `http://localhost:8000/docs`。
2. `POST /batches` 用示例报文(已内置 example)建批:登记新/旧两个工位、
   两个项目(一个对称 ±8µm、一个个非对称 +10/-5µm)并领用。
3. `POST /batches/{id}/verify` → 看 `preview`(相关工位将领用的规范)。
4. `POST /batches/{id}/publish` → 生效并联动换版。
5. 再建一批让旧工位领用非对称项目 → `verify` 返回 `rejected` 和违规清单。
6. `GET /workstations/WS-01/spec`(新终端,上下限)与
   `GET /workstations/WS-02/spec`(旧终端,对称公差)对比下发格式。
7. 交错提交演示:两个批次改同一项目,都核对;先发 B,再发 A 得
   `409 verification_stale`;重新核对 A 后发布成功。

也可以直接跑 `bash scripts/demo.sh` 看完整流程。

## 自测结果

- `pytest`:17 个用例全部通过,覆盖:
  基本流程与两种下发格式、发布前不生效、旧终端领用非对称被拒与修复后发布、
  终端代际翻旧被退回、核对预览、**联动换版**(版本号与 `revision` 同步变化)、
  领用变更只动 `claims_version`、**交错提交**(A 核对后 B 发布 → A 被 409 打回 →
  重新核对后发布成功)、**改回原样也算变过**(取消再恢复领用仍使核对失效)、
  无关改动不阻塞、发布安全网(核对时合法、发布时才出现的违规被 422 兜底)、
  已发布批次不可变、参数校验与引用错误、查询端点与 404。
- 真实 HTTP 冒烟(uvicorn 直跑):`/`、`/docs`、`/openapi.json` 正常;
  `scripts/demo.sh` 全流程通过;交错提交场景实测返回
  `409 verification_stale` → 重新核对 → 发布成功,工位规范联动换版。

## 余项 / 说明

- 本环境无 Docker,`docker compose up` 未实际执行;Dockerfile/compose 已静态核对,
  并以同一命令(`uvicorn app.main:app`)在本地等价验证通过。
- 核对/发布的串行化依赖进程内锁,部署为**单 uvicorn worker**(compose 默认如此);
  如需多 worker/多副本,应改用数据库级乐观锁或 `SELECT ... FOR UPDATE` 等价机制。
- 规范与工位只增改、不删除(无删除 op);如需要可再加 `delete_item` 等操作及相应核对规则。
- 无鉴权;接入生产前需加认证与操作审计。

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
  预览工位 = 批次直接涉及的工位 + 改完后领用了批次直接涉及项目的工位,
  预览内容 = 这些工位改完后领用的**全部**项目(含批次没改、顺带列出的项目);
- 若改完后仍有旧终端领用非对称公差 → 列出(工位, 项目)清单,**整批退回**(`rejected`);
- 否则记录**预览范围**内全部实体的版本指纹,批次进入 `verified`。

**发布(POST /batches/{id}/publish)**

- 仅 `verified` 可发布;发布前比对指纹(版本号为单调计数器,**改回原样也算变过**):
  - 预览所列工位的配置或领用清单变过 → `409 verification_stale`;
  - **预览中列出的项目**规范变过(即使只是顺带列出、批次并未直接修改)→ 同上;
  - 批次**直接涉及的项目**,任何工位增删了它的领用 → 同上;
  - 预览**之外**的工位,只增删了「顺带列出项目」的领用 → **不影响**,原批次仍可发布;
  - 与预览范围无关的改动 → 不影响发布。
- 指纹命中变化时:批次打回 `draft`、清空核对记录,需重新核对;**不落库任何改动**,
  按工位取数继续返回现行生效规范。
- 指纹带格式版本号;升级前生成的核对记录(旧格式/无版本号)一律视为失效,
  必须重新核对后才能发布。
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
7. 交错提交演示(可只用 `/docs`):
   - 建批 A:只改 `ITEM-DIA-40`(主轴孔径),`verify` → 预览里 WS-01 同时列出
     顺带项目 `ITEM-LEN-120`(定位孔径);
   - 建批 B:改 `ITEM-LEN-120`,`verify` 后 `publish`;
   - 再 `publish` A → `409 verification_stale`(`changed_entities` 含
     `ITEM-LEN-120:核对预览中的规范已变更`),A 打回 `draft`;
     期间 `GET /workstations/WS-01/spec` 仍返回 B 的现行规范;
   - 重新 `verify` A 后再 `publish` 成功。
8. 也可用「预览外顺带项目」对照:外部工位只增删 `ITEM-LEN-120` 的领用,A 仍可发布。

也可以直接跑 `bash scripts/demo.sh` 看完整流程(含上述交错演示)。

## 自测结果

- `pytest`:**26 个用例全部通过**,覆盖:
  基本流程与两种下发格式、发布前不生效、旧终端领用非对称被拒与修复后发布、
  终端代际翻旧被退回、核对预览、**联动换版**(版本号与 `revision` 同步变化)、
  领用变更只动 `claims_version`、**交错提交**(A 核对后 B 发布 → A 被 409 打回 →
  重新核对后发布成功)、**改回原样也算变过**(取消再恢复领用/规范改回仍使核对失效)、
  无关改动不阻塞、发布安全网(绕过指纹仍由 422 兜底)、已发布批次不可变、
  参数校验与引用错误、查询端点与 404;以及本次修复的发布判定:
  - 预览中顺带列出的项目(定位孔径)被别批改动 → 409 打回 `draft`,现行规范继续下发;
  - 顺带项目改回原样仍失效;预览所列但批次未直接涉及的工位,配置或领用清单变更 → 409;
  - 批次直接涉及的项目被任意工位增删领用 → 409;
  - **预览外**工位只增删顺带项目的领用 → 原批次仍可发布;
  - 旧格式(升级前已核对未发布)指纹 → 发布 409,重新核对后才可发布。
- 真实 HTTP 冒烟(uvicorn 直跑,容器同款启动命令):`/`、`/docs`、`/openapi.json` 正常;
  `scripts/demo.sh` 全流程通过;交错发布实测
  `409 verification_stale`(含 `ITEM-LEN-120:核对预览中的规范已变更`)→
  重新核对 → 发布成功,期间工位规范继续按现行值下发。
- Docker Compose:**本环境无法实际启动**(无 Docker 守护进程/无 root 或容器权限,
  seccomp 禁止创建 user namespace,镜像仓库不可达)。已对 `docker-compose.yml`
  与 `Dockerfile` 做静态校验,并用 compose 中完全相同的启动命令
  (`uvicorn app.main:app --host 0.0.0.0 --port 8000`,独立 SQLite 文件)
  跑通全套 HTTP 冒烟与演示脚本。

## 余项 / 说明

- **本环境无 Docker/容器权限**,`docker compose up --build` 未实际执行;compose/Dockerfile
  已静态校验,并以同一启动命令在本地等价验证通过(详见上方「自测结果」)。
  在有 Docker 的机器上请复核一次 `docker compose up --build` 与 `/docs` 演示。
- 核对/发布的串行化依赖进程内锁,部署为**单 uvicorn worker**(compose 默认如此);
  如需多 worker/多副本,应改用数据库级乐观锁或 `SELECT ... FOR UPDATE` 等价机制。
- 规范与工位只增改、不删除(无删除 op);如需要可再加 `delete_item` 等操作及相应核对规则。
- 无鉴权;接入生产前需加认证与操作审计。
- 指纹里「预览中顺带列出的项目」只比对规范版本,不比对领用版本:这是按规则刻意放宽
  (预览外工位增删顺带项目的领用不应阻塞原批次)。若日后希望更严格,可在
  `build_fingerprint` 中把 `preview_items` 换成含 `claims_version` 的快照。

# 操作手册（OPERATIONS）

本文给出本地双副本对账的标准操作流程、失败处置、台账与备份说明，以及只读复核服务的使用方法。

---

## 1. 前置条件

- Python 3.10+。
- 当前目录为项目根目录。
- 已存在可写的工作目录（示例为 `./work`）。
- 主副本目录只读访问；备副本位于工作目录内。
- 运行期不需要网络、云服务、外部数据库或第三方包。

推荐先跑：

```bash
python -m unittest discover -s tests -v
python tools/check_file_length.py .
python tools/check_no_network.py lakemirror
```

---

## 2. 首次演练

### 2.1 生成 fixture

```bash
python -m lakemirror fixtures --scenario drift_basic --seed 42 --workdir ./work
```

输出 JSON，包含场景、seed、主备根路径、每个表的快照 ID 与文件数量。相同 seed 重跑会覆盖 `work/lake` 并生成相同内容。

### 2.2 扫描

```bash
python -m lakemirror scan \
  --config configs/reconcile-basic.json \
  --workdir ./work \
  --report ./work/reports/scan.html
```

输出摘要：

```json
{
  "counts_by_code": { "FILE_MISSING_IN_REPLICA": 3, "...": "..." },
  "db": ".../work/ledger.db",
  "report": ".../work/reports/scan.html",
  "scan_id": "scan-...",
  "stats": { "active_diffs": 14, "...": "..." }
}
```

扫描会：

1. 加载并校验配置。
2. 读取每张表的主备 `_manifest.json`。
3. 构建主备快照链并读指针。
4. 逐表比较 schema、分区规格、文件事实、分区覆盖。
5. 应用忽略规则并生成稳定差异 ID。
6. 写入 SQLite 的 `runs`、`diffs`、`snapshots`、`settings_snapshot` 与 `ledger`。

### 2.3 生成计划

```bash
python -m lakemirror plan \
  --scan-id latest \
  --workdir ./work \
  --out ./work/plan.json
```

输出计划摘要。计划文件包含：

- `plan_id`、`plan_fingerprint`、`kind`、`executable`。
- 配置指纹、配置路径、工作目录、扫描 ID。
- 操作数与人工复核数。
- 每个操作的 `op_id`、类型、表、分区、路径、源/目标相对路径、前置条件、期望事实、影响、是否自动、回滚说明、对应差异码。

操作类型：

| 类型 | 作用 |
| --- | --- |
| `copy_file` | 从主副本复制缺失文件。 |
| `replace_file` | 用主副本文件替换大小/SHA/行数不一致的文件。 |
| `delete_file` | 删除备副本多余文件（需 `allow_delete`）。 |
| `copy_manifest` | 文件操作完成后复制主清单。 |
| `update_replica_pointer` | 更新备副本指针到已验证的最新快照。 |
| `manual_review` | 永不执行，只作为人工任务。 |

### 2.4 dry-run

```bash
python -m lakemirror apply --plan ./work/plan.json --workdir ./work
```

- 不创建备份目录。
- 不修改备副本。
- 输出 `status: dry_run`，统计 `would_execute`、`skipped_already_satisfied`、`skipped_manual`。
- dry-run 批次也会写入 `batches` 与 `ledger`，但 `--batch-id latest` 默认跳过 dry-run。

执行前应检查：

- 操作数是否合理。
- 是否有 `manual_review`。
- 删除范围是否符合预期。
- 计划指纹与配置指纹是否对应当前环境。

### 2.5 执行

```bash
python -m lakemirror apply --plan ./work/plan.json --workdir ./work --execute
```

执行器会再次：

1. 校验计划结构与指纹。
2. 校验配置路径、配置指纹、工作目录。
3. 校验 `--execute` 与计划 `executable`。
4. 校验操作上限。
5. 对每个非人工操作校验表存在、源/目标路径安全、源文件事实、删除开关、指针目标快照。
6. 逐操作执行；替换/删除前备份；新增文件记录 `original_exists=false`。
7. 每一步后回读目标校验；失败立即停止。
8. 写 `changes.json`，更新批次摘要与操作状态。

成功输出 `status: completed`，包含 `batch_id`、`backup_dir`、`executed`、`skipped`、`manual`、`failed`。

### 2.6 校验

```bash
python -m lakemirror verify --batch-id latest --workdir ./work
```

- 只接受 `completed` 批次。
- 会重新扫描当前主备状态，而不是复用旧扫描。
- 输出 `auto_residual`、`manual_remaining`、`planned_manual_count`、`manual_preserved_count`、`failed_operations`。
- 无自动可修复残留且无失败操作时，状态为 `verified`。

人工项本身不会导致 `verify_failed`；但被表级安全门阻断而未执行的自动类差异会留在 `auto_residual`，此时校验会失败，需要人工处理后重新计划。

### 2.7 回滚

预览：

```bash
python -m lakemirror rollback --batch-id <batch_id> --workdir ./work
```

执行：

```bash
python -m lakemirror rollback --batch-id <batch_id> --workdir ./work --execute
```

回滚规则：

- 按 `changes.json` 逆序处理。
- `original_exists=true`：从批次备份恢复原文件。
- `original_exists=false`：删除本次新建的目标文件。
- 恢复后写 `rollback.json`，批次状态改为 `rolled_back`。
- 拒绝 dry-run 批次、已回滚批次、备份目录越界、缺失或损坏的 `changes.json`、缺失的备份文件。
- 回滚失败会停止，并把批次标记为 `rollback_failed`。

**注意**：`--batch-id latest` 只跳过 dry-run，不跳过后续的空重跑批次。若执行后又做了幂等重跑，最新批次可能是“0 变更”的批次；回滚时建议显式使用最初执行的 `batch_id`。

---

## 3. 场景化操作

### 3.1 `clean_pair`

```bash
python -m lakemirror fixtures --scenario clean_pair --workdir ./work
python -m lakemirror scan --config configs/reconcile-basic.json --workdir ./work
```

预期 0 差异；`plan` 生成 0 操作的计划。

### 3.2 `drift_basic`

预期 14 个活跃差异，均可由宽松模式自动修复；计划 11 个操作。按第 2 节流程 dry-run、执行、verify、回滚。

### 3.3 `schema_drift`

```bash
python -m lakemirror fixtures --scenario schema_drift --workdir ./work
python -m lakemirror scan --config configs/reconcile-basic.json --workdir ./work
```

预期扫描包含 schema/分区规格差异及文件/分区差异。由于表级安全门，计划为 14 个 `manual_review`，不会执行任何写入。人工处理 schema 后重新生成 fixture 或更新清单，再重新扫描。

### 3.4 `snapshot_lag`

```bash
python -m lakemirror fixtures --scenario snapshot_lag --workdir ./work
python -m lakemirror scan --config configs/reconcile-basic.json --workdir ./work
```

预期 2 个 `SNAPSHOT_LAG` 与 2 个 `POINTER_STALE`。滞后为人工项，导致表级安全门；计划全部人工，不能自动把备副本指针跳到主副本快照。

### 3.5 严格模式

```bash
python -m lakemirror fixtures --scenario drift_basic --workdir ./work
python -m lakemirror scan --config configs/reconcile-strict.json --workdir ./work
```

SHA/行数差异使两张表都阻断自动化；计划全部人工。严格模式适合需要先人工确认内容差异的环境。

### 3.5 本地快照分叉的权威双亲合并

当同一祖先快照（如 `S0`）派生出两个已核镜像分支（`S1`、`S2`）时，普通对账会因 `SNAPSHOT_FORK` 阻断修复。湖镜对账台支持在**独立沙箱**内、依据两份事前授权合同评估并发布保留双亲血缘的权威合并快照，全程离线、主/备原快照保持只读。

先核查分叉证据与两份合同的独立结论（合同之间不得互相借用判定）：

```bash
python -m lakemirror fork-evidence --contract fixtures/fork_merge/contract.json
```

两份合同语义：

- `file_strict`：同一逻辑文件（按分区对齐）至多允许一侧变更；双侧改同一物理文件即阻断（`FILE_STRICT_BOTH_SIDES_CHANGED`）。
- `field_disjoint`：按稳定业务键（`order_id`）做三方字段合并；同一字段仅一侧变化则保留该变化，两侧改成不同值、删除对更新、主键重复、缺文件、哈希失配、未知祖先均阻断。

先 dry-run，确认 `would_write` 后再执行：

```bash
python -m lakemirror fork-merge --contract fixtures/fork_merge/contract.json \
  --contract-name field_disjoint --sandbox ./work/fork-sandbox            # 计划，不落盘
python -m lakemirror fork-merge --contract fixtures/fork_merge/contract.json \
  --contract-name field_disjoint --sandbox ./work/fork-sandbox --execute # 原子发布
```

发布顺序为合并 CSV → 双亲合并 manifest → 当前头 manifest → 指针，全部走临时文件原子替换；指针最后更新，崩溃或重试不会让指针指向未核字节。计划指纹与快照 ID 记录在 `_merge_plans/`：同计划重放幂等（返回 `already_published`），同 ID 的不同计划被拒（`FORK_PLAN_CONFLICT`），不会产生两个不同的 `S3`。合并 manifest 的 `merge` 块保存 `primary`/`replica` 双亲与共同祖先的身份、manifest 摘要与文件证据；`parent_snapshot_id` 保持单亲兼容并指向第一双亲。

发布后链上仍保留历史 `SNAPSHOT_FORK`，但 `forked=false`、`fork_resolved=true`。可用对账裁决确认新权威头可读：

```python
from lakemirror.reconcile import reconcile_fork_table
reconcile_fork_table("./work/fork-sandbox/orders", "orders")  # decision: readable / blocked
```

阻断合同时不写任何字节，并返回带稳定指纹的阻断收据。

---

## 4. 备份与变更记录

目录：

```text
<workdir>/<repair.safety.backup_dir>/<batch_id>/
├── tables/<table>/_manifest.json
├── tables/<table>/_pointer.json
├── tables/<table>/data/...
├── changes.json
└── rollback.json        # 回滚成功后
```

`changes.json`：

```json
{
  "batch_id": "batch-...",
  "changes": [
    {
      "op_id": "op-0001-...",
      "table": "orders",
      "target_rel": "data/dt=2024-01-01/part-0001.csv",
      "original_exists": false,
      "backup_rel": null
    },
    {
      "op_id": "op-0002-...",
      "table": "orders",
      "target_rel": "_manifest.json",
      "original_exists": true,
      "backup_rel": "tables/orders/_manifest.json"
    }
  ]
}
```

- 新增文件不产生备份内容，但会记录，以便回滚删除。
- 覆盖/删除会产生备份。
- 备份路径也经过安全路径校验。
- 批次目录不会被自动清理；按保留期人工归档或删除。

---

## 5. SQLite 台账

默认路径 `work/ledger.db`，由配置或 `--db` 指定，且必须位于工作目录内。

| 表 | 内容 |
| --- | --- |
| `runs` | 扫描运行摘要、配置指纹、统计 JSON。 |
| `snapshots` | 每侧、每表、每个快照节点。 |
| `diffs` | 稳定差异 ID、类别、表、分区、路径、期望/实际、证据、忽略标志。 |
| `plans` | 计划体、指纹、类型、操作数、人工数。 |
| `operations` | 计划内每个操作的当前状态与证据。 |
| `batches` | 执行批次、备份目录、状态、摘要。 |
| `ledger` | 追加式事件流。 |
| `settings_snapshot` | 每次扫描对应的配置快照。 |

常用事件：

```text
scan_completed
scan_failed
batch_started
batch_finished
operation_would_execute
operation_skipped_already_satisfied
operation_skipped_manual
operation_succeeded
operation_failed
verify_verified / verify_verify_failed
batch_rolled_back
batch_rollback_failed
```

操作状态与批次状态都可用于自动化判断，但当前 CLI 不提供直接 SQL 查询子命令；可用标准库 `sqlite3` 只读检查。

---

## 6. 报告

```bash
python -m lakemirror report --workdir ./work --run-id latest --format json
python -m lakemirror report --workdir ./work --run-id latest --format md
python -m lakemirror report --workdir ./work --run-id latest --format html --out ./work/reports/latest.html
```

报告内容：

- 运行摘要与配置指纹。
- 差异总数、活跃数、可自动数、人工数、忽略数。
- 按类别与严重级别统计。
- 活跃冲突证据、忽略规则与已忽略证据。
- 快照链节点。
- 该扫描对应的计划与操作。
- 相关批次。
- 台账事件。
- 安全边界清单。

HTML 报告：

- 全部 CSS 内联。
- 无脚本、无外部图片、无 CDN。
- 设置 CSP、`nosniff`、`DENY`、`no-referrer`。
- 对动态内容进行 HTML 转义。

---

## 7. 只读复核服务

```bash
python -m lakemirror serve --workdir ./work --host 127.0.0.1 --port 8765
```

本地访问 `http://127.0.0.1:8765/`。

允许的监听地址：

```text
127.0.0.1
localhost
::1
```

其他地址返回构造错误，服务不会启动。端口默认为 `8765`，`0` 可用于系统分配临时端口（测试场景）。

路由：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/HEAD | `/` | 最近运行、差异、计划、批次概览。 |
| GET/HEAD | `/healthz` | `{"read_only":true,"status":"ok"}`。 |
| GET/HEAD | `/api/runs.json` | 最近 100 次运行。 |
| GET/HEAD | `/api/batches.json` | 最近 100 个批次。 |
| GET/HEAD | `/api/run/<run_id>.json` | 指定运行的完整 JSON。 |
| GET/HEAD | `/report/<run_id>.<fmt>` | 下载 JSON/Markdown/HTML 报告。 |

防护：

- `Host` 头必须是回环名称（带端口也可）。
- `Content-Length` 超过 4096 返回 `413`。
- POST/PUT/DELETE/PATCH 返回 `405` 与 `Allow: GET, HEAD`。
- 所有响应带安全头；错误信息不回显内部堆栈。
- 服务只读，不提供上传、删除、执行或配置修改端点。
- SQLite 路径必须位于工作目录内。

---

## 8. 失败处置

| 现象 | 常见原因 | 处置 |
| --- | --- | --- |
| `WORKDIR_MISMATCH` | CLI `--workdir` 与配置 `workdir` 不一致 | 使用配置中的工作目录。 |
| `CONFIG_FINGERPRINT_MISMATCH` | 配置在计划后变化 | 重新扫描并重新生成计划。 |
| `PLAN_FINGERPRINT_MISMATCH` | 计划 JSON 被修改 | 删除旧计划，重新生成。 |
| `SOURCE_CHANGED` | 计划后主副本文件变化 | 重新扫描/计划；不要手工绕过。 |
| `PLAN_NOT_EXECUTABLE` | 对 `kind=dry-run` 的计划使用 `--execute` | 生成 `kind=executable` 的计划。 |
| `PLAN_LIMIT_EXCEEDED` | 操作数超过 `max_operations` | 缩小范围或调整配置，并重新计划。 |
| `DELETE_FORBIDDEN` | `allow_delete=false` 但仍存在删除操作 | 不应出现；检查计划来源或配置。 |
| `POINTER_TARGET_MISSING` | 指针目标快照不存在 | 先修复快照链，或人工处理指针。 |
| `ROLLBACK_DRY_RUN` | 对 dry-run 批次回滚 | 只回滚实际执行批次。 |
| `ROLLBACK_ALREADY_DONE` | 重复回滚 | 使用正确批次 ID 或检查台账。 |
| `ROLLBACK_PATH_UNSAFE` | 备份目录越界或含符号链接 | 人工审查备份目录，不要绕过。 |
| `SERVER_HOST_FORBIDDEN` | 监听非回环地址 | 使用 127.0.0.1 / localhost / ::1。 |
| `SERVER_PATH_FORBIDDEN` | 台账不在工作目录 | 使用工作目录内的 `--db`。 |

失败扫描会写入 `runs.status=failed` 和 `scan_failed` 事件；失败执行会保留已完成步骤的备份，批次标记为 `failed`，可按批次回滚。回滚优先使用显式 `batch_id`，不要依赖“最新”。

---

## 9. 并发与安全建议

- 同一工作目录不要同时运行两个 `apply --execute`；SQLite 只提供本地事务，不提供跨进程执行互斥。
- 扫描与报告可并行，但执行期间建议暂停扫描以保持台账语义清晰。
- 主副本应由本工具之外的系统维护；本工具只读主副本。
- 不要把真实密钥、令牌或个人信息放入 fixture、清单、路径或报告。
- 备份目录应按保留期管理，避免磁盘增长。
- 忽略规则必须有原因，并定期复核；忽略不是删除，证据仍保留。
- 不要手工修改 `plan.json`、`changes.json` 或台账来“修复”失败；应重新扫描、计划或回滚。

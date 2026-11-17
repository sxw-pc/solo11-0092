# 配置参考（CONFIG）

配置文件是一个本地 JSON 对象。加载时会执行严格的结构、路径与安全校验；任何未知字段都会报错，避免拼写错误被静默忽略。

示例：

- `configs/reconcile-basic.json`：宽松模式，允许自动修复缺失/多余/大小/SHA/行数/分区/指针。
- `configs/reconcile-strict.json`：严格模式，SHA-256 与行数差异不自动修复，并给出一个不会命中真实 fixture 路径的忽略规则示例。

---

## 1. 路径解析

- 所有相对路径以**配置文件所在目录**为基准解析，而不是当前工作目录。
- 加载时统一使用 `Path.resolve()` 归一化。
- `primary_root`、`replica_root`、`workdir` 必须是已存在目录，且不能是符号链接。
- `replica_root` 必须位于 `workdir` 内。
- `ledger` 必须位于 `workdir` 内；若已存在则不能是符号链接。
- 主副本与备副本必须是分离的目录树，不能相同、互为父子。
- `backup_dir` 必须位于 `workdir` 内。
- 每个表的 `path` 必须是安全相对路径，且在主、备两个根目录下都已存在。

安全相对路径拒绝：绝对路径、Windows 盘符、NUL、空组件、`.`、`..`、包含冒号的组件。规范化使用 `/` 作为清单中的逻辑分隔符。

---

## 2. 顶层结构

```json
{
  "schema_version": 1,
  "primary_root": "../work/lake/primary",
  "replica_root": "../work/lake/replica",
  "workdir": "../work",
  "ledger": "../work/ledger.db",
  "tables": [],
  "checks": {},
  "repair": {},
  "ignore_rules": []
}
```

| 字段 | 必需 | 类型 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | 可选 | 整数 | 缺省为 `1`；当前仅接受 `1`，其他值（含布尔、字符串）直接报错。 |
| `primary_root` | 是 | 字符串 | 只读主副本根目录。 |
| `replica_root` | 是 | 字符串 | 可受控修改的备副本根目录，必须在 `workdir` 内。 |
| `workdir` | 是 | 字符串 | 工作目录；备份、台账和副本沙箱的边界。 |
| `ledger` | 否 | 字符串 | SQLite 台账路径，默认 `ledger.db`，必须在 `workdir` 内。 |
| `tables` | 是 | 数组 | 非空表配置数组，见第 3 节。 |
| `checks` | 是 | 对象 | 检查开关，见第 4 节。 |
| `repair` | 是 | 对象 | 修复策略与安全设置，见第 5 节。 |
| `ignore_rules` | 否 | 数组 | 审计型忽略规则，默认为空。 |

除以上字段外的顶层字段会被拒绝。

---

## 3. 表配置 `tables[]`

```json
{
  "name": "orders",
  "path": "tables/orders",
  "business_key": "order_id",
  "partition_columns": ["dt"]
}
```

| 字段 | 必需 | 类型 | 约束 |
| --- | --- | --- | --- |
| `name` | 是 | 字符串 | 匹配 `^[A-Za-z][A-Za-z0-9_.-]{0,63}$`，唯一。 |
| `path` | 是 | 字符串 | 安全相对路径，唯一；主、备目录必须都存在。 |
| `business_key` | 是 | 字符串 | 非空；用于配置快照与人工复核说明。 |
| `partition_columns` | 否 | 字符串数组 | 默认 `[]`；不允许重复；会与主清单 `partition_spec` 对比。 |

表的逻辑目录布局：

```text
<root>/<table.path>/
├── _manifest.json
├── _pointer.json
├── _snapshots/<snapshot_id>.json
└── data/...                # 路径由清单 files[].path 指定
```

注意：`partition_columns` 是配置侧的期望值；主清单声明的分区规格会与它比较，主备清单之间也会比较。任何不一致都会成为诊断差异，不会触发 DDL。

---

## 4. 检查开关 `checks`

```json
{
  "file_size": true,
  "sha256": true,
  "row_count": true,
  "partition_coverage": true,
  "snapshot_chain": true,
  "schema_compat": true,
  "partition_spec": true
}
```

| 开关 | 默认 | 关闭后的效果 |
| --- | --- | --- |
| `file_size` | `true` | 不产生 `FILE_SIZE_MISMATCH`。 |
| `sha256` | `true` | 不产生 `FILE_SHA256_MISMATCH`。 |
| `row_count` | `true` | 不产生 `ROW_COUNT_MISMATCH`。 |
| `partition_coverage` | `true` | 不产生 `PARTITION_MISSING` / `PARTITION_EXTRA`。 |
| `snapshot_chain` | `true` | 不加载快照链，不产生滞后/断链/分叉/指针差异。 |
| `schema_compat` | `true` | 不产生 schema 类差异。 |
| `partition_spec` | `true` | 不产生分区规格/分区列差异。 |

`checks` 必须存在且为对象；允许只写部分字段，未写字段采用默认 `true`。未知字段会被拒绝，非布尔值会被拒绝。

关闭检查只会减少差异；不会改变路径安全、执行边界、dry-run 或回滚机制。

---

## 5. 修复策略 `repair`

```json
{
  "mode": "lenient",
  "auto_fix": ["FILE_MISSING_IN_REPLICA"],
  "manual_review": ["PATH_OUTSIDE_ROOT"],
  "safety": {
    "allow_delete": true,
    "max_operations": 200,
    "backup_dir": "backups",
    "dry_run_default": true
  }
}
```

### 5.1 `mode`

- `"lenient"`：默认值。按 `auto_fix` 列表允许自动修复。
- `"strict"`：即使 `FILE_SHA256_MISMATCH` 或 `ROW_COUNT_MISMATCH` 出现在 `auto_fix` 中，`Config.can_auto()` 仍返回 false；这两个类别必须人工复核。

`mode` 必须是 `lenient` 或 `strict`。

### 5.2 `auto_fix` 与 `manual_review`

- 两者都必须是已知冲突码数组。
- 同一类别不能同时出现在两边。
- 未列入任何一边的类别在计划中按人工项处理（文件类差异也不会自动执行）。
- 即使列入 `auto_fix`，以下类别在 planner 中也永远不自动执行：

```text
DUPLICATE_LOGICAL_PATH
PATH_OUTSIDE_ROOT
INVALID_PARTITION_VALUE
SNAPSHOT_LAG
SNAPSHOT_CHAIN_BROKEN
SNAPSHOT_FORK
SCHEMA_ADDITIVE_OK
SCHEMA_TYPE_MISMATCH
SCHEMA_FIELD_MISSING
SCHEMA_FIELD_ORDER_MISMATCH
SCHEMA_NULLABILITY_MISMATCH
PARTITION_SPEC_MISMATCH
PARTITION_COLUMN_MISSING
```

`FILE_EXTRA_IN_REPLICA` 还受 `allow_delete` 约束。

完整冲突码见 [CONFLICTS.md](CONFLICTS.md)。

### 5.3 `safety`

`safety` 是必需对象，允许字段如下：

| 字段 | 必需 | 类型/约束 | 说明 |
| --- | --- | --- | --- |
| `allow_delete` | 是 | 布尔 | 是否允许计划删除多余的备副本文件。 |
| `max_operations` | 是 | 1–10000 的整数 | 计划操作数上限；超过则拒绝生成/执行。 |
| `backup_dir` | 否 | 安全路径字符串 | 默认 `backups`；相对 `workdir` 解析且必须留在其内。 |
| `dry_run_default` | 是 | 布尔，必须为 `true` | 防止配置侧关闭 dry-run 默认值。 |

执行开关只有 CLI 的 `apply --execute` 与 `rollback --execute`。配置不能把默认执行打开。

---

## 6. 忽略规则 `ignore_rules[]`

```json
{
  "code": "FILE_EXTRA_IN_REPLICA",
  "table": "events",
  "path_prefix": "data/day=2099",
  "reason": "Example retention exclusion; no real fixture path matches."
}
```

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `code` | 是 | 已知冲突码，或 `*`。 |
| `table` | 否 | 已配置表名，或 `*`；默认 `*`。 |
| `path_prefix` | 否 | 安全相对路径前缀；空字符串表示不限制。 |
| `reason` | 是 | 非空审计原因。 |

匹配逻辑是：`code` 匹配且 `table` 匹配且（未设置前缀或差异 `path` 以该前缀开头）。差异的 `path` 对分区级差异可能为空，因此只有 `path_prefix` 为空时才能匹配。

忽略结果：

- 差异仍会保存在数据库与报告中。
- `ignored=true`，保留 `ignore_reason`。
- `auto_fixable` 被强制为 false。
- 不进入可执行计划；仍可能作为人工证据展示。
- 统计中计入 `ignored_count`，不计入活跃差异。

忽略规则本身也会进入配置快照与报告，便于审计。

---

## 7. 配置指纹

配置加载后会计算 `fingerprint`，覆盖：

- 归一化后的主根、备根、工作目录、台账路径。
- 表名、路径、业务键、分区列。
- 检查开关。
- 修复模式、auto/manual 列表、删除开关、操作上限、备份目录。
- 忽略规则。
- dry-run 默认值。

计划会保存该指纹；执行前再次比对。配置、路径或策略发生变化后，旧计划会被拒绝，必须重新扫描并生成计划。

---

## 8. 配置错误示例

```text
config error [tables[0].path]: unsafe path: '../outside'
config error [repair]: categories cannot be both auto and manual: FILE_MISSING_IN_REPLICA
config error [replica_root]: replica_root must stay inside workdir
config error [repair.safety.dry_run_default]: dry_run_default must be true; --execute is the only opt-in
```

CLI 会将 `ConfigError` 转为退出码 `2`，并输出稳定错误码 `CONFIG_INVALID`。

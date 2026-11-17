# 冲突码与判定（CONFLICTS）

本文列出扫描产生的全部冲突码、严重级别、判定依据、默认自动化归属与修复计划映射。所有代码都是稳定字符串，可直接用于配置、忽略规则、报表与自动化。

自动化归属取决于配置：

- **宽松模式**：`auto_fix` 列表中的类别可自动修复。
- **严格模式**：`FILE_SHA256_MISMATCH` 与 `ROW_COUNT_MISMATCH` 即使出现在 `auto_fix` 中也不自动修复。
- **永不自动**：下表中标为「永不自动」的类别不受配置影响。
- **表级安全门**：只要同一张表存在一个不可有效自动修复的活跃差异，整张表的所有自动化都被抑制，全部差异转为 `manual_review`。唯一例外是非阻塞的 `SCHEMA_ADDITIVE_OK`：它不触发安全门，也不生成操作。
- **忽略规则**：命中的差异保留证据，但 `ignored=true`、`auto_fixable=false`，不进入计划。

严重级别：`critical`、`high`、`medium`、`info`。

---

## 1. 文件与分区

### `FILE_MISSING_IN_REPLICA` — high

- **判定**：以 `(partition, path)` 为逻辑键，主清单有、备清单没有。
- **证据**：`expected` 为主清单文件事实（路径、分区、大小、行数、SHA-256），`actual=null`，`evidence.logical_key`。
- **默认**：宽松/严格均可自动修复。
- **计划**：`copy_file`，从主副本复制到备副本，复制后校验大小/SHA/行数。
- **人工场景**：表被表级安全门阻断、源文件已变化、路径越界。

### `FILE_EXTRA_IN_REPLICA` — medium

- **判定**：以 `(partition, path)` 为逻辑键，备清单有、主清单没有。
- **证据**：`expected=null`，`actual` 为备清单文件事实。
- **默认**：仅在 `repair.safety.allow_delete=true` 时可自动修复。
- **计划**：`delete_file`，删除前备份原文件。
- **人工场景**：`allow_delete=false`、表被阻断、路径越界。

### `FILE_SIZE_MISMATCH` — high

- **判定**：同一逻辑键同时存在于两边，但 `size` 不同。
- **证据**：`expected`/`actual` 为整数大小。
- **计划**：`replace_file`，用主副本文件覆盖，覆盖前备份。
- **说明**：同一文件可能同时产生大小、SHA、行数差异；计划会合并为一个 `replace_file`。

### `FILE_SHA256_MISMATCH` — critical

- **判定**：同一逻辑键两边 SHA-256 不同。
- **证据**：双方 64 位十六进制摘要。
- **默认**：宽松模式可自动修复；严格模式**永不自动**。
- **计划**：`replace_file`（宽松且未被阻断时）。
- **人工场景**：严格模式、表级安全门、数据库要求先确认内容差异。

### `ROW_COUNT_MISMATCH` — high

- **判定**：同一逻辑键两边 `row_count` 不同。
- **证据**：双方整数行数。
- **默认**：宽松模式可自动修复；严格模式**永不自动**。
- **计划**：`replace_file`（宽松且未被阻断时）。
- **说明**：行数来自清单；fixture 生成时与文件解析结果一致。

### `PARTITION_MISSING` — high

- **判定**：主清单存在某分区，备清单没有该分区的任何文件。
- **证据**：`expected` 包含该分区文件数与总字节，`evidence.partition`。
- **计划**：若该分区的文件产生复制操作，则分区差异被文件操作覆盖；否则转 `manual_review`。
- **说明**：分区级差异是覆盖范围提示，不是独立的数据写入操作。

### `PARTITION_EXTRA` — medium

- **判定**：备清单存在某分区，主清单没有该分区的任何文件。
- **计划**：若该分区的多余文件产生删除操作，则被覆盖；否则转 `manual_review`。

### `EMPTY_TABLE` — high

- **判定**：备清单 `files` 为空。
- **证据**：主文件数与备文件数。
- **默认**：永不自动。
- **计划**：`manual_review`。
- **原因**：空表可能是合法状态，也可能是复制失败；直接删除主副本或批量复制都需要人工确认。

### `DUPLICATE_LOGICAL_PATH` — critical

- **判定**：同一清单内出现重复逻辑路径，包括仅大小写不同的碰撞。
- **证据**：`path` 字段为清单 JSON 路径（如 `$.files[1]`），`evidence` 包含源文件与字段。
- **默认**：永不自动。
- **计划**：`manual_review`。
- **说明**：语义问题不会导致整份清单解析失败；问题文件被排除在有效文件集合外。

### `PATH_OUTSIDE_ROOT` — critical

- **判定**：清单文件路径不是安全相对路径，或计划/执行阶段发现路径越界。
- **证据**：`path` 为清单 JSON 路径，`evidence.message` 给出具体原因。
- **默认**：永不自动。
- **计划**：`manual_review`；执行器在任何落盘前再次拒绝。
- **说明**：路径问题也可能导致整份清单加载失败；此时是结构性 `ManifestError`，直接阻断扫描。

### `INVALID_PARTITION_VALUE` — critical

- **判定**：分区值不是字符串、为空、缺少 `=`、键重复、键集合与 `partition_spec` 不一致，或未分区表使用了非空值。
- **证据**：清单 JSON 路径与具体原因。
- **默认**：永不自动。
- **计划**：`manual_review`。

---

## 2. 快照链

### `SNAPSHOT_LAG` — high

- **判定**：备副本最新快照序号小于主副本最新快照序号，且备链未判定为断链。
- **证据**：双方最新快照节点、快照数量差、序号差、时间差、文件数差、总字节差、分区数差。
- **默认**：永不自动。
- **计划**：`manual_review`。
- **说明**：滞后本身不代表可以直接复制清单；需要按受控修复流程处理，且表级安全门会阻断同表自动操作。

### `SNAPSHOT_CHAIN_BROKEN` — critical

- **判定**：主或备快照链出现重复快照 ID、父节点缺失、序号回退、重复序号、多根或指针目标缺失。
- **证据**：包含最新快照、数量差、序号差及链问题列表（备侧）。
- **默认**：永不自动。
- **计划**：`manual_review`。

### `SNAPSHOT_FORK` — critical

- **判定**：同一父节点有多个子节点；或备副本最新快照 ID 不在主副本历史集合中。
- **证据**：同滞后证据；备侧分叉还包含链问题列表。
- **默认**：永不自动。
- **计划**：`manual_review`。

### `POINTER_STALE` — medium

- **判定**：备副本 `_pointer.json` 的 `snapshot_id` 不等于备副本链的最新快照 ID。
- **证据**：`expected.snapshot_id` 为最新快照，`actual.snapshot_id` 为当前指针。
- **默认**：配置中通常允许自动修复。
- **计划**：`update_replica_pointer`，仅当：
  - 表未被表级安全门阻断；
  - 指针目标在备副本历史或当前清单中存在；
  - 目标 ID 通过安全字符校验。
- **执行**：写入前备份旧指针，写入后回读校验。

### 内部链问题码

这些码用于快照链内部诊断，不直接作为顶层冲突码：

| 码 | 含义 |
| --- | --- |
| `DUPLICATE_SNAPSHOT_ID` | 同一快照 ID 出现多次。 |
| `SNAPSHOT_PARENT_MISSING` | 父快照 ID 不在链中。 |
| `SNAPSHOT_SEQUENCE_REGRESSION` | 子快照序号不递增。 |
| `MULTIPLE_ROOTS` | 出现多个无父节点。 |
| `SNAPSHOT_FORK` | 某父节点有多个子节点。 |
| `DUPLICATE_SEQUENCE` | 多个快照共享同一序号。 |
| `POINTER_TARGET_MISSING` | 指针指向不存在的快照。 |

---

## 3. Schema 与分区规格

以下全部为**诊断**，永不执行 DDL。

### `SCHEMA_ADDITIVE_OK` — info

- **判定**：备副本额外字段全部可空，且主副本没有同名字段。
- **阻塞**：否。
- **默认**：永不自动。
- **计划**：若所在表未被阻断，计划中跳过（无需修复）；若表被阻断，则作为人工证据保留。

### `SCHEMA_TYPE_MISMATCH` — critical

- **判定**：同名字段类型不同；或备副本独有字段为非可空。
- **计划**：`manual_review`。

### `SCHEMA_FIELD_MISSING` — critical

- **判定**：主副本有、备副本没有的字段。
- **计划**：`manual_review`。

### `SCHEMA_FIELD_ORDER_MISMATCH` — high

- **判定**：两边共享字段的物理顺序不同。
- **计划**：`manual_review`。

### `SCHEMA_NULLABILITY_MISMATCH` — high

- **判定**：同名字段可空性不同。
- **计划**：`manual_review`。

### `PARTITION_SPEC_MISMATCH` — critical

- **判定**：主备清单分区列或顺序不同；或配置的 `partition_columns` 与主清单不一致。
- **证据**：`expected`/`actual` 为分区列数组。
- **计划**：`manual_review`。

### `PARTITION_COLUMN_MISSING` — critical

- **判定**：某侧分区规格声明了列，但该侧 schema 不包含该字段。
- **计划**：`manual_review`。

---

## 4. 清单结构错误

这些是 `ManifestError`，不是 SQLite 差异码。扫描无法建立该表的事实集合时，整个 `scan` 失败、退出码为 `2`，并记录失败运行。

| 码 | 含义 |
| --- | --- |
| `MANIFEST_MISSING` | 清单文件不存在。 |
| `MANIFEST_INVALID_JSON` | JSON 解析失败，错误含行列位置。 |
| `MANIFEST_FIELD_MISSING` | 缺少必需顶层字段。 |
| `MANIFEST_FIELD_TYPE` | 字段类型错误（含负数、布尔冒充整数、非法 SHA 等）。 |
| `MANIFEST_UNKNOWN_FIELD` | 顶层或文件条目的未知字段。 |
| `DUPLICATE_SCHEMA_FIELD` | schema 内字段名重复。 |
| `MANIFEST_INVALID` | 上述错误的统一包装码。 |

语义问题（`PATH_OUTSIDE_ROOT`、`DUPLICATE_LOGICAL_PATH`、`INVALID_PARTITION_VALUE`）默认不使解析失败，而是作为差异进入对账；`parse_manifest(..., strict=True)` 可将它们升级为 `ManifestError`。

---

## 5. 策略组合示例

### 宽松模式

```text
auto_fix: FILE_MISSING_IN_REPLICA, FILE_EXTRA_IN_REPLICA, FILE_SIZE_MISMATCH,
          FILE_SHA256_MISMATCH, ROW_COUNT_MISMATCH, PARTITION_MISSING,
          PARTITION_EXTRA, POINTER_STALE
```

`drift_basic` 的 14 个差异全部可自动处理，计划 11 个操作：

- 3 个 `copy_file`
- 2 个 `replace_file`（合并 SHA/大小/行数差异）
- 2 个 `delete_file`
- 2 个 `copy_manifest`
- 2 个 `update_replica_pointer`

### 严格模式

```text
auto_fix 不含 FILE_SHA256_MISMATCH、ROW_COUNT_MISMATCH
```

`drift_basic` 的 3 个 checksum/行数差异使两张表都触发安全门，最终计划全部为人工复核，不执行任何自动写入。

### Schema 漂移

`schema_drift` 中任一 schema 阻塞差异会阻断同表自动化。原扫描的 8 个文件/分区类差异也不会被自动处理，而是与 6 个 schema/规格差异一起进入 14 个 `manual_review`。

### 忽略规则

忽略一个 `FILE_EXTRA_IN_REPLICA` 后：

- 该差异仍在报告和台账中；
- `ignored=true`，`ignore_reason` 保留；
- 不再生成对应删除操作；
- 若因此留下同分区 `PARTITION_EXTRA` 没有文件操作覆盖，该分区差异会转人工复核。

---

## 6. 分诊顺序建议

1. 先看 `PATH_OUTSIDE_ROOT`、`DUPLICATE_LOGICAL_PATH`、`INVALID_PARTITION_VALUE`：修复清单可信度。
2. 再看 schema 与分区规格：确定表是否应整体冻结自动化。
3. 再看快照链断链/分叉/滞后：确认主备版本关系。
4. 再看文件缺失/多余/大小/SHA/行数。
5. 最后看指针过期；只有表未被阻断、目标快照存在时才可自动更新。
6. 所有忽略规则必须带原因，并在复核报告中定期审计。

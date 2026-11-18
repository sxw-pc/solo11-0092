# 湖镜对账台（Lake Mirror）

**本地双副本数据湖一致性对账与可回滚修复编排工具。**

湖镜对账台在同一个工作目录内，对比一份只读的「主副本」与一份允许受控修复的「备副本」：读取两边的清单、快照指针、表结构、分区规格和文件事实，生成可解释的差异清单与确定性修复计划；默认只做 dry-run，只有显式 `--execute` 才会在备副本沙箱内落盘，并留下备份、台账、校验和回滚记录。

项目完全离线，运行期只使用 Python 3.10+ 标准库，测试使用 `unittest`，数据存储为本地文件系统与 SQLite。

---

## 1. 它是什么，不是什么

**它解决：**

- 主副本与备副本之间本地文件的缺失、多余、大小、SHA-256、行数不一致。
- 分区覆盖差异，以及清单中的重复逻辑路径、越界路径、非法分区值。
- 快照链滞后、断链、分叉、重复快照、指针过期。
- Schema 与分区规格漂移的诊断：只诊断、只阻塞，不执行任何 DDL。
- 确定性、可审计、可先看后做的修复计划。
- 带本地备份、逐步台账、执行后复扫校验、逆序回滚的受控修复。
- 自包含 JSON / Markdown / HTML 报告，以及仅监听回环地址的只读复核页面。

**它刻意不是：**

- 不是湖仓快照回滚工具，也不是 Iceberg/Delta/Hudi 的提交协议实现。
- 不是表维护、compaction、小文件合并或冷热分层工具。
- 不是 schema evolution 自动准入或自动改表工具；冲突只进入人工复核。
- 不是分区裁剪、查询优化或数据跳过工具。
- 不是摄入水位、流式 checkpoint 或消息队列消费管理工具。
- 不是 SCD2 / 维度建模质量校验平台。
- 不扫描外部集群，不连接云服务，不读取凭据，不做远程复制，不绕过系统权限。

---

## 2. 快速开始

以下命令均在项目根目录执行。`configs/*.json` 中的相对路径以配置文件所在目录为基准解析，因此默认示例指向项目根下的 `work/`。

### 2.1 生成离线 fixture

```bash
python -m lakemirror fixtures --scenario drift_basic --seed 42 --workdir ./work
```

生成内容：

```text
work/
└── lake/
    ├── primary/
    │   └── tables/{events,orders}/...
    ├── replica/
    │   └── tables/{events,orders}/...
    └── samples/
        ├── path_escape_manifest.json
        ├── partition_spec_primary.json
        └── partition_spec_replica.json
```

同样输入与 seed 生成完全相同的内容；修改 seed 会改变合成数据内容。

### 2.2 对账

```bash
python -m lakemirror scan \
  --config configs/reconcile-basic.json \
  --workdir ./work \
  --report ./work/reports/scan.html
```

输出 JSON 到标准输出，同时写入 SQLite 台账；`--report` 可按后缀 `.json`、`.md`、`.html` 生成报告。

### 2.3 生成确定性修复计划

```bash
python -m lakemirror plan \
  --scan-id latest \
  --workdir ./work \
  --out ./work/plan.json
```

### 2.4 默认 dry-run

```bash
python -m lakemirror apply --plan ./work/plan.json --workdir ./work
```

不会改动备副本。输出会统计 `would_execute` 与 `skipped_already_satisfied`。

### 2.5 显式执行

```bash
python -m lakemirror apply --plan ./work/plan.json --workdir ./work --execute
```

执行前会重新校验计划指纹、配置指纹、工作目录、当前源文件事实、目标路径边界和指针目标；每一步成功后写台账与备份。

### 2.6 执行后校验

```bash
python -m lakemirror verify --batch-id latest --workdir ./work
```

成功时输出 `"status": "verified"`。校验会重新扫描，而不是信任计划里的旧状态。

### 2.7 回滚

```bash
python -m lakemirror rollback --batch-id latest --workdir ./work --execute
```

回滚默认也是 dry-run；不带 `--execute` 只展示逆序恢复动作。显式执行后按批次 `changes.json` 逆序恢复原文件、删除新复制文件、还原清单与指针。

### 2.8 报告

```bash
python -m lakemirror report --workdir ./work --run-id latest --format md
python -m lakemirror report --workdir ./work --run-id latest --format html --out ./work/reports/latest.html
```

### 2.9 只读复核服务

```bash
python -m lakemirror serve --workdir ./work --host 127.0.0.1 --port 8765
```

然后访问本地页面 `http://127.0.0.1:8765/`。

服务规则：

- 只允许 `127.0.0.1`、`localhost`、`::1`；其他监听地址直接拒绝。
- 只接受 `GET` / `HEAD`；写方法返回 `405` 与 `Allow: GET, HEAD`。
- 校验 `Host` 头，只接受回环名称；请求体上限 4096 字节。
- 响应带 CSP、`nosniff`、`DENY`、`no-referrer`、`no-store` 等安全头。
- 页面与报告全内联，无脚本、无 CDN、无外部资源。
- 路由仅：`/`、`/healthz`、`/api/runs.json`、`/api/batches.json`、`/api/run/<id>.json`、`/report/<run_id>.<json|md|html>`。

### 2.10 合成湖报告来源裁决

```bash
python -m lakemirror provenance-evidence \
  --manifest fixtures/report_provenance/manifest.json

python -m lakemirror provenance-adjudicate \
  --manifest fixtures/report_provenance/manifest.json \
  --out-dir ./work/provenance-release
```

`provenance-evidence` 只核字节与结构：manifest/header/目标 SHA、每个有序片段的全部候选、片段内候选字节一致、跨片段禁配引用合法，以及 `header + 三个片段` 能逐字节重建目标；输出 `publication_decisions: null`。

`provenance-adjudicate` 把“文件字节可重建”和“来源权利可用”分开裁决，并按来源 ID（而不是相同 SHA）保留候选身份：

- `reproducible_authorized`：只要存在一条全部 `external_allowed`、无禁配、逐字节复现目标的组合，就生成**新报告实例**，并写出来源收据；该收据声明 `original_producer_receipt: false`。
- `origin_safe`：必须证明**所有**与现有模糊回填证据相容的历史来源组合都可外发，才允许发布**原回填实例**；只找到一条干净链不够。存在受限历史候选或禁配历史组合时返回 `ORIGIN_NOT_PROVABLY_SAFE`，并附反例组合。
- 两份合同可能对同一报告字节给出不同结论；退出码为 `0` 当且仅当所选合同全部放行，`2` 表示至少一份合同拒绝。
- 坏 SHA、路径逃逸、缺少片段、重复候选 ID、片段内候选字节不一致、禁配引用不存在、同摘要来源替换、快照/合同错绑、收据冒充原件，都会阻断。

默认 fixture 中，`reproducible_authorized` 选择 `name-public + value-public-b + footer-public` 并放行；简单按片段顺序取第一个 `external_allowed` 会选中被禁配的 `value-public-a`。`origin_safe` 因 `name-restricted` / `value-restricted` / `footer-restricted` 与禁配组合仍然逐字节相容而拒绝原件发布。正反例 fixture 位于 `fixtures/report_provenance/all_public/` 与 `fixtures/report_provenance/restricted_blocked/`。

这是纯本地合成来源策略，不联网，也不做真实许可证解释。

### 2.11 运行全部测试

```bash
python -m unittest discover -s tests -v
```

或：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

### 2.12 静态守卫

```bash
python tools/check_file_length.py .
python tools/check_no_network.py lakemirror
```

第一条检查所有 Python 源文件不超过 300 行；第二条检查运行时代码没有外部网络客户端或内嵌外部 URL。

---

## 3. 四个内置场景

fixture 是确定性的合成数据，不对应任何真实公司、主体、账号或数据集。

| 场景 | 生成内容 | 扫描结果 | 计划结果 |
| --- | --- | --- | --- |
| `clean_pair` | 主备清单、文件、快照、指针完全一致 | 0 个差异 | 0 个操作 |
| `drift_basic` | 缺文件、多文件、分区缺失/多余、大小/SHA/行数漂移、指针过期 | 14 个可自动修复差异 | 11 个操作：3 copy、2 replace、2 delete、2 manifest、2 pointer |
| `schema_drift` | 类型、可空性、字段缺失、可加字段、分区规格与分区列漂移 | 14 个活跃差异（8 个文件/分区类，6 个 schema/规格类） | 表级安全门触发，14 个全部转入人工复核 |
| `snapshot_lag` | 主副本 3 个快照、备副本 2 个快照，备副本指针落后 | 4 个差异（2 个滞后、2 个指针过期） | 表级安全门触发，4 个全部转入人工复核 |

**表级安全门**：只要某张表存在至少一个「按当前策略不可自动修复」的活跃差异，该表的全部自动化操作都会被抑制，所有差异转为 `manual_review`。这是为了避免在 schema 或快照链已经异常时，仍然对同表文件做半自动修改。非阻塞的 `SCHEMA_ADDITIVE_OK` 不触发安全门（无需修复，计划中直接跳过）。关闭表级自动化的判断是保守的、可解释的、确定性的。

严格模式示例：

```bash
python -m lakemirror fixtures --scenario drift_basic --workdir ./work
python -m lakemirror scan --config configs/reconcile-strict.json --workdir ./work
```

在 `reconcile-strict.json` 中，SHA-256 与行数不一致不在 `auto_fix` 内；因此这两类差异会把相关表整体转入人工复核。严格模式不会自动执行 checksum/行数修复。

场景元数据见 `fixtures/scenarios/<name>/scenario.json`；实际数据由 `fixtures` 命令生成，不把生成物提交进仓库。

---

## 4. 项目结构

```text
.
├── README.md
├── pyproject.toml
├── configs/
│   ├── reconcile-basic.json
│   └── reconcile-strict.json
├── fixtures/scenarios/
│   ├── clean_pair/scenario.json
│   ├── drift_basic/scenario.json
│   ├── schema_drift/scenario.json
│   └── snapshot_lag/scenario.json
├── docs/
│   ├── CONFIG.md
│   ├── CONFLICTS.md
│   └── OPERATIONS.md
├── lakemirror/
│   ├── __init__.py / __main__.py
│   ├── cli.py              # 子命令与退出码
│   ├── config.py           # 配置加载、路径与安全校验
│   ├── config_types.py     # 配置值对象
│   ├── fixtures.py         # 确定性离线 fixture
│   ├── manifest.py         # 清单解析与结构校验
│   ├── snapshot.py         # 快照链构建、滞后/分叉分析
│   ├── schema.py           # schema 与分区规格漂移诊断
│   ├── reconcile.py        # 文件/分区/schema/快照对账
│   ├── reconcile_types.py  # Diff / ScanResult
│   ├── planner.py          # 确定性修复计划
│   ├── plan_io.py          # 计划指纹与结构校验
│   ├── executor.py         # dry-run / 执行 / 备份 / 台账
│   ├── rollback.py         # 逆序回滚
│   ├── verify.py           # 执行后复扫校验
│   ├── ledger.py           # SQLite 台账
│   ├── report.py           # JSON / Markdown 报告
│   ├── report_html.py      # 自包含 HTML
│   ├── provenance_evidence.py  # 合成来源证据与字节重建校验
│   ├── provenance_chain.py     # 重建组合、禁配与收据来源解析
│   ├── provenance_release.py   # 按合同裁决发布权利
│   ├── provenance_receipts.py  # 收据复核与本地发布物
│   ├── server.py           # 回环只读 HTTP 服务
│   └── util.py             # 哈希、路径安全、原子写、行数统计
├── fixtures/report_provenance/ # 合成报告来源正反例
├── tests/
│   ├── support.py
│   ├── test_*.py
│   └── golden/report.{json,md,html}
└── tools/
    ├── check_file_length.py
    └── check_no_network.py
```

---

## 5. 安全边界

1. **默认不落盘。** 配置中的 `repair.safety.dry_run_default` 必须为 `true`；唯一执行开关是 CLI 的 `--execute`。
2. **写入范围。** 执行器只能写配置工作目录下的备副本根目录；主副本只读。
3. **路径防护。** 拒绝绝对路径、盘符、`..`、空组件、NUL、冒号组件、符号链接逃逸和越界删除。清单、计划、执行、回滚每一层都重复校验，不只依赖入口。
4. **先校验后修改。** 执行前重新校验计划指纹、配置指纹、工作目录、源文件大小/SHA/行数、目标边界、删除开关和指针目标快照。
5. **有备份才修改。** 替换/删除前把原文件复制到备份批次目录；新增文件也被记录，以便回滚时删除。
6. **失败即停。** 任一步失败立即停止，已完成步骤仍可通过批次回滚；失败状态写入台账。
7. **人工项不执行。** `manual_review` 操作永远不会被 executor 落盘。
8. **无 DDL、无远程动作。** schema 漂移只诊断；没有改表、远程复制、集群连接、凭据读取、密钥管理或权限绕过。
9. **离线。** 运行时代码不导入外部 HTTP 客户端，不访问网络；HTML 无脚本、无外部 CSS/JS/图片。
10. **确定性。** fixture 由显式 seed 控制；计划按固定排序生成稳定 ID；报告 JSON 使用规范化排序。

详细规则见：

- [配置参考](docs/CONFIG.md)
- [冲突码与判定](docs/CONFLICTS.md)
- [操作手册](docs/OPERATIONS.md)

---

## 6. 台账与审计

SQLite 台账包含以下表：

`runs`、`snapshots`、`diffs`、`plans`、`operations`、`batches`、`ledger`、`settings_snapshot`。

- 每次扫描保存运行摘要、表统计、快照链、差异、配置快照。
- 每次计划保存计划体与每个操作的初始状态。
- 每次执行建立批次，逐步记录操作状态，并追加 `ledger` 事件。
- dry-run 批次也会记录，但 `--batch-id latest` 默认跳过 dry-run 批次。
- 回滚成功会写入 `rollback.json` 并把批次更新为 `rolled_back`。
- 报告只从台账读取，不重新推断历史。

备份目录示例：

```text
work/backups/<batch_id>/
├── tables/orders/_manifest.json       # 被覆盖文件的备份
├── tables/orders/data/...             # 被替换/删除文件的备份
├── changes.json                       # 每步的 original_exists / backup_rel
└── rollback.json                      # 回滚完成后生成
```

---

## 7. 确定性

- `fixtures --seed` 控制全部随机抽样。
- 计划操作按「人工优先、表、分区、操作优先级、路径、差异码」稳定排序。
- 每个操作有稳定 `op_id`；计划有内容指纹 `plan_fingerprint` 和派生 `plan_id`。
- 计划、配置、执行前状态任一变化都会导致执行被拒绝，必须重新生成计划。
- 报告 JSON 使用 `sort_keys` 的规范化输出；HTML/Markdown 不使用时间戳之外的运行时随机值。
- 测试使用临时目录，不依赖仓库内 `work/` 生成物。

---

## 8. 测试覆盖

测试套件使用标准库 `unittest`，覆盖：

- 配置加载、未知字段、重复表、路径逃逸、工作目录约束、dry-run 强制、忽略规则。
- 清单 JSON 错误定位、必填字段、未知字段、重复 schema 字段、路径越界、重复逻辑路径、非法分区、大小写碰撞。
- 快照链线性、重复 ID、缺失父节点、序号回退、多根、分叉、指针目标缺失、滞后证据。
- schema 加法兼容、类型/可空性/字段缺失/顺序、分区规格与分区列缺失。
- 文件与分区九类差异、严格/宽松策略、忽略规则审计、空表、稳定 diff ID。
- 计划操作类型、稳定排序、表级安全门、删除开关、操作上限、计划指纹防篡改、路径越界。
- dry-run 不落盘、执行、备份、幂等重跑、源变化拒绝、配置指纹拒绝、verify、rollback、重复回滚拒绝。
- SQLite 八张表、扫描/计划/操作/批次/事件往返、忽略标志与字段归一化。
- 报告结构、JSON/Markdown/HTML 黄金文件、自包含与 XSS 转义、确定性。
- CLI 全流程、fixture 可复现、错误码与退出码。
- 回环服务页面、API、报告下载、HEAD、Host 校验、请求体上限、写方法拒绝、构造参数防护。
- 两个静态守卫工具自身与仓库策略。

运行：

```bash
python -m unittest discover -s tests -v
```

---

## 9. 验收清单

- [x] `fixtures` 一条命令生成 `clean_pair`、`drift_basic`、`schema_drift`、`snapshot_lag`。
- [x] `scan` 生成差异、SQLite 台账和 JSON/Markdown/HTML 报告。
- [x] `plan` 生成确定性、带指纹、带前置条件与回滚说明的计划。
- [x] 默认 `apply` 为 dry-run，备副本字节不变。
- [x] `apply --execute` 仅在备副本沙箱内修改，生成备份和逐步台账。
- [x] `verify` 复扫后输出 `verified`。
- [x] 同一计划再次执行全部 `skipped_already_satisfied`。
- [x] `rollback --execute` 逆序恢复，复扫回到执行前差异。
- [x] `report` 输出 JSON/Markdown/HTML，HTML 自包含。
- [x] `serve` 仅回环、只读、有安全头与请求体上限。
- [x] 全部 Python 文件不超过 300 行。
- [x] 运行时代码无外部网络客户端、无外部资源引用。
- [x] 所有 fixture 主体、表名、分区、路径均为合成数据。

---

## 10. 已知限制

- 只处理本地文件系统上的一对主/备副本；不实现远端复制或对象存储协议。
- 对账以清单为事实来源；不解析业务字段内容，不做行级主键对账。
- 行数由本地文件解析得出，支持 CSV、JSONL、JSON 数组和普通文本行。
- 不自动执行 schema 变更、快照合并、分区重写或任何 DDL。
- 计划中的删除必须显式开启 `allow_delete`；否则相关表整体进入人工复核。
- 备份会占用本地磁盘；批次清理需由运维在保留期后手工完成。
- 回滚依赖批次目录中的 `changes.json` 与备份文件完整存在。

---

## 11. 退出码

| 退出码 | 含义 |
| ---: | --- |
| `0` | 命令成功；`verify` 为 `verified`；dry-run 计划有效 |
| `2` | 配置/路径/计划/执行错误；`verify` 未通过；命令拒绝执行 |
| `130` | 用户中断 |

错误输出格式为 `error [CODE]: message`，稳定错误码便于自动化。

# 审计核查策略合同（AUDIT_POLICY）

本文说明离线湖镜对合成镜像冲突的核查资源分配：每个审计窗口只能在四种核查中实际执行一种，策略必须在窗口开始前以概率分布形式发布，并落入本地审计收据。

校准数据 `fixtures/audit_game/calibration.json` 是**虚构 fixture 给定的合同收益表**，不等于真实攻击检出概率的统计保证。三种合成攻击模式取自现有湖镜冲突类型：`FILE_SHA256_MISMATCH`、`SCHEMA_TYPE_MISMATCH`、`SNAPSHOT_FORK`。

---

## 1. 两份事前合法合同

同一 fixture 支持两份合同，二者对同一窗口会给出不同结论：

| 合同 | 目标 | 结论 |
| --- | --- | --- |
| `worst_case_guarantee` | 最大化三列攻击中最低的合成检出收益 | 混合策略 `[0, 4/17, 10/17, 3/17]`，三列均为 `43/85`，超过 `1/2` 门槛 |
| `prior_weighted_yield` | 只最大化 `[7,2,1]` 先验下的平均命中收益 | 纯 `byte_sha_recheck`，平均 `37/50`，但 `SCHEMA_TYPE_MISMATCH` 列检出为 `0` |

规则与限制：

- 每窗口只能选择四种核查之一；校准样本不得视为可在同一窗口同时执行四种核查。
- 不得由先验高频直接删去低频但高损的攻击分支；最坏情形合同对全部三列求精确极大极小。
- 业务门槛为「最低检出超过 1/2」，用精确有理数严格判定（`43/85` 超出门槛 `1/170`）；均匀四动作只有 `2/5`，在门槛下。
- `preview_audit_policy` 只输出内部诊断数值（`publishable=false`），不寻找最优策略、不给对偶证明，也不做任何业务裁决。

## 2. 精确有理数保证

`worst_case_guarantee` 输出：

- 精确有理数策略与每列收益、最坏列（并列时全部列出）；
- 对手在三种攻击模式上的分布构成的对偶上界（本 fixture 为 `[9/34, 6/17, 13/34]`）；
- 原始值与对偶值相等证明：任何策略的最小列收益都不超过对偶分布下的最大行收益 `43/85`，因此不存在更优策略。

求解器对任意列变更稳定：逐顶点枚举精确求解，最优并列时按固定规范选择，不硬编码任何分数。

## 3. 本地审计收据

```bash
python -m lakemirror audit-policy --calibration fixtures/audit_game/calibration.json \
  --contract worst_case_guarantee --window 2026-W47 --seed <秘密种子> --out receipt.json
python -m lakemirror audit-draw   --receipt receipt.json \
  --calibration fixtures/audit_game/calibration.json --seed <秘密种子>
python -m lakemirror audit-verify --receipt receipt.json \
  --calibration fixtures/audit_game/calibration.json --contract worst_case_guarantee
```

- 发布时只写入种子承诺（SHA-256），不写种子；抽取时揭示种子，按承诺重放，实际只抽取一个动作。
- 收据包含：窗口、合同、快照 ID、fixture SHA-256、策略分布、每列收益、承诺、揭示的种子、实际执行动作与内容指纹。
- 校准被改动后，旧收据不得复用：`audit-draw` / `audit-verify` 以 `AUDIT_CALIBRATION_STALE` 拒绝。
- 收据内容被篡改（含换合同）以 `AUDIT_RECEIPT_TAMPERED` 拒绝；跨合同复用以 `AUDIT_CONTRACT_MISMATCH` 拒绝；重复抽取以 `AUDIT_RECEIPT_REUSED` 拒绝。

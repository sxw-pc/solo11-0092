# Lake Mirror reconciliation report

Local dual-replica lake consistency, with a deterministic repair audit trail.

- Run: `scan-demo`
- Status: **completed**
- Started: `2024-04-01T00:00:00Z`
- Finished: `2024-04-01T00:00:02Z`
- Config fingerprint: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`

## Summary

- Tables: 1
- Conflicts: 2 active / 3 total
- Auto-fixable: 1
- Manual review: 1
- Ignored: 1

| Conflict code | Count |
| --- | ---: |
| `FILE_EXTRA_IN_REPLICA` | 1 |
| `FILE_MISSING_IN_REPLICA` | 1 |
| `SCHEMA_TYPE_MISMATCH` | 1 |

## Ignore rules

| Code | Table | Path prefix | Reason |
| --- | --- | --- | --- |
| `FILE_EXTRA_IN_REPLICA` | `orders` | `data/dt=2024-01-02/part-9999.csv` | owner is investigating |

## Active conflicts

| Severity | Code | Table | Partition | Path | Expected | Actual |
| --- | --- | --- | --- | --- | --- | --- |
| high | `FILE_MISSING_IN_REPLICA` | `orders` | `dt=2024-01-01` | `data/dt=2024-01-01/part-0001.csv` | `{'size': 42, 'sha256': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'}` | `None` |
| high | `SCHEMA_TYPE_MISMATCH` | `orders` | `` | `amount` | `DECIMAL` | `STRING` |

## Ignored conflicts (audited, not repaired)

- `FILE_EXTRA_IN_REPLICA` on `orders` `dt=2024-01-02` `data/dt=2024-01-02/part-9999.csv`: owner is investigating

## Snapshot chain

| Side | Table | Snapshot | Sequence | Parent | Files | Bytes |
| --- | --- | --- | ---: | --- | ---: | ---: |
| primary | `orders` | `snap-orders-0001` | 1 | `-` | 2 | 120 |
| replica | `orders` | `snap-orders-0002` | 2 | `snap-orders-0001` | 2 | 115 |

## Repair plan

Plan `plan-cccccccccccccccc` (executable), operations: 2, manual: 1.

| # | Type | Table | Partition | Path | Auto | Reason |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | `copy_file` | `orders` | `dt=2024-01-01` | `data/dt=2024-01-01/part-0001.csv` | yes |  |
| 2 | `manual_review` | `orders` | `` | `amount` | no | Schema drift is blocking and needs owner review. |

## Execution batches

| Batch | Status | Backups | Started | Finished |
| --- | --- | --- | --- | --- |
| `batch-1` | completed | `/sandbox/work/backups/batch-1` | `2024-04-01T00:01:00Z` | `2024-04-01T00:01:02Z` |

## Operations

Dry-run: `python -m lakemirror apply --plan plan.json --workdir ./work`

Execute: `python -m lakemirror apply --plan plan.json --workdir ./work --execute`

Verify: `python -m lakemirror verify --batch-id latest --workdir ./work`

Rollback: `python -m lakemirror rollback --batch-id latest --workdir ./work --execute`

## Safety boundaries

- The executor writes only inside the configured replica sandbox under workdir.
- No network, cloud, database, or foreign-cluster connection is used.
- Manual-review operations are never executed automatically.
- Every mutating batch has a local backup directory and a rollback record.
- The primary lake directory is read-only to this tool.

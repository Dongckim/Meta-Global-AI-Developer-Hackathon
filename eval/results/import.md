
## Protocol import + voice edits (live muse-spark-1.3 minimal)

> Superseded run: strict rejected the free-key groups map (400). Fixed by
> array-form groups; the passing run is below.

Accuracy: 0/4. Fresh record per case.

| turn | total_ms | pass | reply |
|---|---|---|---|
| import demo PDF | 296 | n |  |
| move the controls one row down | 1525 | n | - :: No run-plan draft is open — want me to open one to |
| make drug B 6 replicates | 1165 | n | - :: No run-plan draft is open — want me to open one to |
| change drug A to row E | 1057 | n | - :: No draft is open right now — want me to open one t |

## Protocol import + voice edits (live muse-spark-1.3 minimal)

Accuracy: 4/4. Fresh record per case.

| turn | total_ms | pass | reply |
|---|---|---|---|
| import demo PDF | 4470 | y | Draft ready: 3 groups, 5 steps. |
| move the controls one row down | 1727 | y | edit_draft :: Updated control wells. Heads up: C2–C8 overlaps co |
| make drug B 6 replicates | 1195 | y | edit_draft :: Updated drug b count. |
| change drug A to row E | 1499 | y | edit_draft :: Updated drug a wells. |

## Protocol import v2 + voice edits + Start (live muse-spark-1.3 minimal)

Accuracy: 6/9. Fresh record per case.

| turn | total_ms | pass | reply |
|---|---|---|---|
| import v1 PDF | 7095 | y | Draft ready: 3 conditions, 5 steps. 3 to review. |
| import v2 user PDF | 13773 | y | Draft ready: 3 conditions, 5 steps. 5 to review. |
| import wash PDF | 5631 | n | Draft ready: 2 conditions, 5 steps. |
| import table PDF | 5752 | y | Draft ready: 2 conditions, 3 steps. |
| move the controls one row down | 1734 | y | edit_draft :: Updated control wells. Heads up: C2–C8 overlaps co |
| make drug B 6 replicates | 1181 | y | edit_draft :: Updated drug b count. |
| change drug A to row E | 1350 | y | edit_draft :: Updated drug a wells. |
| change the first step to 50 microliters | 1207 | n | edit_draft :: I couldn't edit the draft: unknown path steps.[0]. |
| start wash (repeat x2) | 2 | n | 6 live steps |

## Protocol import v2 + voice edits + Start (live muse-spark-1.3 minimal)

Accuracy: 9/9. Fresh record per case.

| turn | total_ms | pass | reply |
|---|---|---|---|
| import v1 PDF | 9709 | y | Draft ready: 3 conditions, 5 steps. 3 to review. |
| import v2 user PDF | 8375 | y | Draft ready: 3 conditions, 5 steps. 7 to review. |
| import wash PDF | 5895 | y | Draft ready: 2 conditions, 4 steps. |
| import table PDF | 5236 | y | Draft ready: 2 conditions, 3 steps. |
| move the controls one row down | 1614 | y | edit_draft :: Updated control wells. Heads up: C2–C8 overlaps co |
| make drug B 6 replicates | 926 | y | edit_draft :: Updated drug b count. |
| change drug A to row E | 1620 | y | edit_draft :: Updated drug a wells. |
| change the first step to 50 microliters | 1297 | y | edit_draft :: Updated step 1 volume. |
| start wash (repeat x2) | 2 | y | 5 live steps |

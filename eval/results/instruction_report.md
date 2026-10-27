
## Instruction vs report (live muse-spark-1.3 minimal)

Accuracy: 10/10. Fresh record per turn.

| turn | kind | total_ms | calls | pass | reply |
|---|---|---|---|---|---|
| wash the remaining wells right now | instruction | 1776 | - | y | How long should I set for washing C2–C8? |
| add drug A to C2 through C8 | instruction | 2789 | show | y | Viewing: plate C2-C8 |
| go do the next wash step | instruction | 3232 | read_record,answer | y | Nice — you're on step 3 drug A C2–C8, then drug B and 30 min incubatio |
| start the next incubation | instruction | 1967 | set_timer | y | 30 minutes on C2–C8, as planned? |
| pipette the samples now | instruction | 1429 | - | y | Got it — pipette C2 through C8 for step 3. |
| C2 to C8 done | report | 74 | mark_done | y | Nice, C2–C8 are in. |
| finished adding drug A to C2 through C5 | report | 839 | mark_done | y | Nice, C2–C5 are in. |
| I put the samples in D2 to D8 | report | 1182 | mark_done | y | Wait — D2–D8 isn't in this step. Still log it? |
| washed C6 to C8 | report | 806 | mark_done | y | Nice, C6–C8 are in. |
| C6, C7 and C8 are finished | report | 868 | mark_done | y | Nice, C6–C8 are in. |

## Conversational timing (<=2000ms, zero writes)

| turn | total_ms | pass | reply |
|---|---|---|---|
| got it, thanks | 1335 | y | Got it! |
| cool | 1113 | y | Nice, got it! |
| okay perfect | 535 | y | Nice! |

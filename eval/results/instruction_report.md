
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

## Instruction vs report (live muse-spark-1.3 minimal)

Accuracy: 10/10. Fresh record per turn.

| turn | kind | total_ms | calls | pass | reply |
|---|---|---|---|---|---|
| wash the remaining wells right now | instruction | 1441 | - | y | How many minutes for this wash? |
| add drug A to C2 through C8 | instruction | 2148 | - | y | How many minutes for drug A on C2-C8? |
| go do the next wash step | instruction | 3943 | read_record | y | Hmm, I don't see a wash step in the plan — which wells should I guide  |
| start the next incubation | instruction | 1159 | - | y | How many minutes for this incubation? |
| pipette the samples now | instruction | 1533 | - | y | I didn't log anything — want me to? |
| C2 to C8 done | report | 77 | mark_done | y | Nice, C2–C8 are in. |
| finished adding drug A to C2 through C5 | report | 616 | mark_done | y | Nice, C2–C5 are in. |
| I put the samples in D2 to D8 | report | 1158 | mark_done | y | Wait — D2–D8 isn't in this step. Still log it? |
| washed C6 to C8 | report | 529 | mark_done | y | Nice, C6–C8 are in. |
| C6, C7 and C8 are finished | report | 696 | mark_done | y | Nice, C6–C8 are in. |

## Conversational timing (<=2000ms, zero writes)

| turn | total_ms | pass | reply |
|---|---|---|---|
| got it, thanks | 701 | y | Got it, anytime! |
| cool | 866 | y | Nice! |
| okay perfect | 552 | y | Got it — nice! |

## Instruction vs report (live muse-spark-1.3 minimal)

Accuracy: 10/10. Fresh record per turn.

| turn | kind | total_ms | calls | pass | reply |
|---|---|---|---|---|---|
| wash the remaining wells right now | instruction | 1252 | - | y | How long should I set the wash timer for C2–C8? |
| add drug A to C2 through C8 | instruction | 2366 | - | y | I didn't log anything — want me to? |
| go do the next wash step | instruction | 1521 | - | y | How long for this wash, and which wells? |
| start the next incubation | instruction | 1353 | set_timer | y | 30 minutes on C2–C8, as planned? |
| pipette the samples now | instruction | 2324 | - | y | Are you pipetting C2–C8 now — want me to start the 30m timer? |
| C2 to C8 done | report | 48 | mark_done | y | Nice, C2–C8 are in. |
| finished adding drug A to C2 through C5 | report | 1087 | mark_done | y | Nice, C2–C5 are in. |
| I put the samples in D2 to D8 | report | 1021 | mark_done | y | Wait — D2–D8 isn't in this step. Still log it? |
| washed C6 to C8 | report | 1424 | mark_done | y | Nice, C6–C8 are in. |
| C6, C7 and C8 are finished | report | 1035 | mark_done | y | Nice, C6–C8 are in. |

## Conversational timing (<=2000ms, zero writes)

| turn | total_ms | pass | reply |
|---|---|---|---|
| got it, thanks | 430 | y | Got it! |
| cool | 590 | y | Cool, let's keep it rolling. |
| okay perfect | 911 | y | Got it! |

## Instruction vs report (live muse-spark-1.3 minimal)

Accuracy: 10/10. Fresh record per turn.

| turn | kind | total_ms | calls | pass | reply |
|---|---|---|---|---|---|
| wash the remaining wells right now | instruction | 2026 | - | y | Got it — how many minutes for this wash so I can time C2–C8? |
| add drug A to C2 through C8 | instruction | 3356 | - | y | Add Drug A to C2-C8 now, then say done? |
| go do the next wash step | instruction | 2607 | read_record,answer | y | Hmm, no wash step in this run — just media, vehicle, drug A, drug B, t |
| start the next incubation | instruction | 3422 | read_record,set_timer | y | 30 minutes on C2. I'll ping you. Timer's on: 30 minutes for C3. Got it |
| pipette the samples now | instruction | 1562 | - | y | Got it — go ahead with C2–C8 for step 3. |
| C2 to C8 done | report | 72 | mark_done | y | Nice, C2–C8 are in. |
| finished adding drug A to C2 through C5 | report | 841 | mark_done | y | Nice, C2–C5 are in. |
| I put the samples in D2 to D8 | report | 1057 | mark_done | y | Wait — D2–D8 isn't in this step. Still log it? |
| washed C6 to C8 | report | 723 | mark_done | y | Nice, C6–C8 are in. |
| C6, C7 and C8 are finished | report | 828 | mark_done | y | Nice, C6–C8 are in. |

## Conversational timing (<=2000ms, zero writes)

| turn | total_ms | pass | reply |
|---|---|---|---|
| got it, thanks | 729 | y | Got it — happy to help! |
| cool | 576 | y | Nice, we're set on C2–C8. |
| okay perfect | 659 | y | Got it! |

## Instruction vs report (live muse-spark-1.3 minimal)

Accuracy: 10/10. Fresh record per turn.

| turn | kind | total_ms | calls | pass | reply |
|---|---|---|---|---|---|
| wash the remaining wells right now | instruction | 1348 | - | y | Got it! |
| add drug A to C2 through C8 | instruction | 4566 | - | y | Let me know when drug A is added to C2-C8? |
| go do the next wash step | instruction | 2834 | read_record | y | Got it — checking the plan for that wash. |
| start the next incubation | instruction | 1603 | set_timer | y | 30 minutes on C2–C8, as planned? |
| pipette the samples now | instruction | 2877 | - | y | How long should I set the timer for these? |
| C2 to C8 done | report | 69 | mark_done | y | Nice, C2–C8 are in. |
| finished adding drug A to C2 through C5 | report | 1685 | mark_done | y | Nice, C2–C5 are in. |
| I put the samples in D2 to D8 | report | 2334 | mark_done | y | Wait — D2–D8 isn't in this step. Still log it? |
| washed C6 to C8 | report | 2480 | mark_done | y | Nice, C6–C8 are in. |
| C6, C7 and C8 are finished | report | 1824 | mark_done | y | Nice, C6–C8 are in. |

## Conversational timing (<=2000ms, zero writes)

| turn | total_ms | pass | reply |
|---|---|---|---|
| got it, thanks | 689 | y | Got it — happy to help! |
| cool | 806 | y | Nice, I'm here when you need me. |
| okay perfect | 904 | y | Nice! |

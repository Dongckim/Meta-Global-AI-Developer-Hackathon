
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

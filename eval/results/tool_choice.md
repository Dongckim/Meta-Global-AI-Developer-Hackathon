
## Write-vs-view tool choice (live muse-spark-1.3 minimal)

Accuracy: 10/10. One session, fresh record.

| turn | total_ms | calls | pass | reply |
|---|---|---|---|---|
| Note C8, sticky edges | 81 | add_note | y | Noted on C8. |
| I want to make notes on C8 | 1683 | - | y | What should I note on C8? |
| Show me C8 | 816 | open | y | Opened C8. |
| C8 looks weird | 2471 | add_note | y | Got it — noted on C8. |
| Open C8 and note bubbles | 2162 | open,add_note | y | Opened C8. Written down for C8. |
| Let me see the timers | 1104 | show | y | Viewing: timer |
| C8 is contaminated, exclude it | 917 | exclude_well | y | Drop C8? |
| What is on C8 | 2806 | read_record,exclude_well | y | Exclude C8? |
| Mark C8 done | 706 | mark_done | y | Nice, C8 are in. |
| Go to the robot preview | 1725 | navigate | y | Showing Robot preview. |

## Write-vs-view tool choice (live muse-spark-1.3 minimal)

Accuracy: 10/10. One session, fresh record.

| turn | total_ms | calls | pass | reply |
|---|---|---|---|---|
| Note C8, sticky edges | 80 | add_note | y | Noted on C8. |
| I want to make notes on C8 | 1711 | - | y | Got it — what should I note on C8? |
| Show me C8 | 705 | open | y | Opened C8. |
| C8 looks weird | 1124 | add_note | y | Got it — noted on C8. |
| Open C8 and note bubbles | 2710 | open,add_note | y | Opened C8. Written down for C8. |
| Let me see the timers | 5078 | read_record | y | Got it — which looked-up data should I continue with for C2–C8? |
| C8 is contaminated, exclude it | 722 | exclude_well | y | Drop C8? |
| What is on C8 | 2451 | read_record | y | C8 is planned. Memos: sticky edges; looks weird; bubbles. |
| Mark C8 done | 965 | mark_done | y | Nice, C8 are in. |
| Go to the robot preview | 784 | navigate | y | Showing Robot preview. |

## Write-vs-view tool choice (live muse-spark-1.3 minimal)

Accuracy: 10/10. One session, fresh record.

| turn | total_ms | calls | pass | reply |
|---|---|---|---|---|
| Note C8, sticky edges | 89 | add_note | y | Noted on C8. |
| I want to make notes on C8 | 782 | - | y | What should I write on C8? |
| Show me C8 | 782 | open | y | Opened C8. |
| C8 looks weird | 933 | add_note | y | Got it — noted on C8. |
| Open C8 and note bubbles | 1593 | open,add_note | y | Opened C8. Written down for C8. |
| Let me see the timers | 738 | show | y | Viewing: timer |
| C8 is contaminated, exclude it | 688 | exclude_well | y | Drop C8? |
| What is on C8 | 2290 | read_record,answer | y | C8 is drug_A, planned, notes: sticky edges, looks weird, bubbles. |
| Mark C8 done | 613 | mark_done | y | Nice, C8 are in. |
| Go to the robot preview | 556 | navigate | y | Showing Robot preview. |

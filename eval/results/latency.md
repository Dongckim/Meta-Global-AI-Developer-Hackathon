# Latency: legacy prompt vs compact prompt

5 sentences, fresh store + session each, live muse-spark-1.3 minimal. Legacy = reconstructed pre-B prompt (long instructions + 14 tools incl. web_search + full state). ms = server-measured spark time.

| sentence | arm | ms | in_tok | cached | out_tok | calls |
|---|---|---|---|---|---|---|
| mark C2 and C3 done please | before | 1062 | 2410 | 0 | 103 | mark_done |
| mark C2 and C3 done please | after | 1856 | 1716 | 0 | 235 | mark_done |
| D5 looks cloudy, drop it from the run | before | 1674 | 2412 | 0 | 126 | exclude_well |
| D5 looks cloudy, drop it from the run | after | 754 | 1718 | 1649 | 77 | exclude_well |
| start a timer for 25 minutes for C4 | before | 1498 | 2412 | 2289 | 114 | set_timer |
| start a timer for 25 minutes for C4 | after | 2983 | 1718 | 0 | 383 | set_timer |
| how many wells have I finished | before | 1180 | 2408 | 2289 | 119 | where_am_i |
| how many wells have I finished | after | 1097 | 1714 | 0 | 128 | answer |
| remind me to wash when the timer ends, 10 minutes for C6 | before | 1956 | 2418 | 0 | 233 | set_timer |
| remind me to wash when the timer ends, 10 minutes for C6 | after | 2127 | 1724 | 0 | 358 | set_timer |

before median: ms=1498 in_tok=2412 cached=0 out_tok=119

after median: ms=1856 in_tok=1718 cached=0 out_tok=235

Goals: spark ms median <= 2000, in_tok <= 2000.

Note: first run had two after-arm outliers (5.0s no-call ramble, 14.3s
get_well misuse on a count question). Fixed with one SYSTEM line ("use get_well
only for memo text, never for counts"); rerun above shows all-correct paths.
cached>0 on some rows shows cross-request prefix caching works.
Read: in_tok -29% (2412→1718); ms medians both <2s with no significant
change at n=5 (1498→1856 is within run variance). out_tok median rose
(119→235): less context seems to cost more reasoning tokens.

## Re-measure: flat calls (no propose_plan, 14 tools)

Same 5 sentences, fresh store + session each, live muse-spark-1.3 minimal.

| sentence | ms | in_tok | cached | out_tok | calls | ph |
|---|---|---|---|---|---|---|
| mark C2 and C3 done please | 903 | 1730 | 0 | 98 | mark_done | d6f3a753 |
| D5 looks cloudy, drop it from the run | 911 | 1732 | 0 | 104 | exclude_well | d6f3a753 |
| start a timer for 25 minutes for C4 | 2627 | 1732 | 0 | 344 | set_timer | d6f3a753 |
| how many wells have I finished | 2776 | 1728 | 1649 | 178 | answer | d6f3a753 |
| remind me to wash when the timer ends, 10 minutes for C6 | 2369 | 1738 | 0 | 301 | set_timer | d6f3a753 |

flat median: ms=2369 in_tok=1732 cached=0 out_tok=178

## Spark settings (before): cache key+24h, strict tools

Same 5 sentences, fresh store + session each, live muse-spark-1.3 minimal. viol = model args breaking the fixed semantic rule (missing always-required, wrong JSON type, unknown key).

| sentence | ms | in_tok | cached | out_tok | viol | calls |
|---|---|---|---|---|---|---|
| mark C2 and C3 done please | 1687 | 1855 | 0 | 130 | 0 | mark_done |
| D5 looks cloudy, drop it from the run | 1783 | 1857 | 0 | 241 | 0 | exclude_well |
| start a timer for 25 minutes for C4 | 3305 | 1857 | 113 | 530 | 0 | set_timer |
| how many wells have I finished | 1284 | 1853 | 0 | 125 | 0 | answer |
| remind me to wash when the timer ends, 10 minutes for C6 | 2338 | 1863 | 0 | 373 | 0 | set_timer |

before median: ms=1783 in_tok=1857 cached=0 out_tok=241 viol_total=0

## Spark settings (after): cache key+24h, strict tools

Same 5 sentences, fresh store + session each, live muse-spark-1.3 minimal. viol = model args breaking the fixed semantic rule (missing always-required, wrong JSON type, unknown key).

| sentence | ms | in_tok | cached | out_tok | viol | calls |
|---|---|---|---|---|---|---|
| mark C2 and C3 done please | 1367 | 1926 | 1905 | 128 | 0 | mark_done |
| D5 looks cloudy, drop it from the run | 1291 | 1928 | 1905 | 126 | 0 | exclude_well |
| start a timer for 25 minutes for C4 | 2168 | 1928 | 1905 | 329 | 0 | set_timer |
| how many wells have I finished | 1145 | 1924 | 1905 | 136 | 0 | answer |
| remind me to wash when the timer ends, 10 minutes for C6 | 2014 | 1934 | 1905 | 364 | 0 | set_timer |

after median: ms=1367 in_tok=1928 cached=1905 out_tok=136 viol_total=0

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 1566 | 1453 | spark | mark_done | ✓ C2, C3 done |
| note C3, big bubbles near the edge | 72 | 0 | grammar | add_note | Note saved (C3) |
| start a 25 minute timer for C4 | 1954 | 1902 | spark | - | How many minutes for the timer? |
| how many wells have I finished | 1534 | 1455 | spark | answer | 2 wells finished (C2-C3) |
| D5 looks cloudy, drop it from the run | 1850 | 1767 | spark | exclude_well | Exclude D5? |
| yes(confirm) | 6 | 0 | confirm | - | D5 excluded (looks cloudy) |
| C6 has a bubble, redo it in a backup well | 4187 | 4104 | spark | find_free_wells | I didn't catch a command. |
| what is on C6 | 1436 | 1347 | spark | get_well | C6 is planned. |
| cancel the C4 timer | 83 | 0 | grammar | manage_timers | I couldn't find a C4 timer. No timers running. |
| mark C4 through C8 done | 938 | 885 | spark | mark_done | ✓ C4, C5, C6, C7, C8 done |

all_under_3s: False

## A/B: chain vs summary (5-turn conversation, live)

Same 5 turns, fresh store each arm. (a) previous_response_id chain,
(b) store:false + recent-6-turn summary injection.

| turn | a ms | a in | a cached | b ms | b in | b cached |
|---|---|---|---|---|---|---|
| mark C2 and C3 done | 1588 | 1926 | 1905 | 2066 | 1926 | 1905 |
| note those, bubbles | 9640 | 2067 | 113 | 1749 | 1997 | 241 |
| 25 min timer C4 | 10516 | 2240 | 241 | 1914 | 2062 | 241 |
| how many finished | 8858 | 2424 | 241 | 919 | 2135 | 241 |
| D5 cloudy, drop it | 11488 | 2558 | 241 | 1221 | 2190 | 369 |

Chain turns 2-5 take 9-11s each (fixed overhead per chained call);
summary stays under 2.1s with identical outcomes ("those" resolved to
C2-C3 in both arms). Summary is now the default; chain remains behind
CHAIN_ENABLED=true.

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 891 | 792 | spark | mark_done | ✓ C2, C3 done |
| note C3, big bubbles near the edge | 81 | 0 | grammar | add_note | Note saved (C3) |
| start a 25 minute timer for C4 | 1583 | 1532 | spark | - | How many minutes for the timer? |
| how many wells have I finished | 3609 | 3515 | spark | read_record | C2 is done. C3 is done. Memos: big bubbles near the edge. C4 |
| D5 looks cloudy, drop it from the run | 902 | 812 | spark | exclude_well | Exclude D5? |
| yes(confirm) | 5 | 0 | confirm | - | D5 excluded (looks cloudy) |
| C6 has a bubble, redo it in a backup well | 5830 | 5742 | spark | find_free_wells | I didn't catch a command. |
| what is on C6 | 2173 | 2098 | spark | read_record | C6 is planned. |
| cancel the C4 timer | 82 | 0 | grammar | manage_timers | I couldn't find a C4 timer. No timers running. |
| mark C4 through C8 done | 715 | 664 | spark | mark_done | ✓ C4, C5, C6, C7, C8 done |

all_under_3s: False

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 682 | 591 | 1 | spark | mark_done | ✓ C2, C3 done |
| command | note C4, slight edge effect | 69 | 0 | 0 | grammar | add_note | Note saved (C4) |
| command | start the timer for C5, five minutes | 1619 | 1570 | 1 | spark | set_timer | Timer started: drug A · C5 · 5 min |
| question | what is on C6 | 4271 | 4195 | 2 | spark | read_record,answer | Step 3 of 5 drug A C2 to C8: C2 C3 done, C4 to C8 remaining, |
| question | read all my notes | 2864 | 2783 | 2 | spark | read_record,answer | Step 3 of 5 drug A C2 to C8: C2 C3 done, C4 to C8 remaining, |
| question | how many wells have I finished | 3785 | 3716 | 2 | spark | read_record,answer | Step 3 of 5 drug A C2 to C8: C2 C3 done, C4 to C8 remaining, |

questions_under_4s: False, commands_under_3s: True

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 766 | 690 | spark | mark_done | Nice, C2–C3 are in. |
| note C3, big bubbles near the edge | 71 | 0 | grammar | add_note | Noted on C3. |
| start a 25 minute timer for C4 | 912 | 856 | spark | set_timer | 25 minutes on C4. I'll ping you. |
| how many wells have I finished | 2288 | 2197 | spark | read_record | C2 is done. C3 is done. Memos: big bubbles near the edge. C4 |
| D5 looks cloudy, drop it from the run | 752 | 669 | spark | exclude_well | Drop D5? |
| yes(confirm) | 6 | 0 | confirm | - | D5 excluded (looks cloudy) |
| C6 has a bubble, redo it in a backup well | 3751 | 3668 | spark | find_free_wells | Sorry, say that again? |
| what is on C6 | 2553 | 2466 | spark | read_record | C6 is planned. |
| cancel the C4 timer | 79 | 0 | grammar | manage_timers | Okay, C4 timer's off. |
| mark C4 through C8 done | 540 | 486 | spark | mark_done | Got it — C4–C8 done. |

all_under_3s: False

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 843 | 766 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 75 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 705 | 653 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 2283 | 2193 | 2 | spark | read_record | C6 is planned. |
| question | read all my notes | 2049 | 1958 | 2 | spark | read_record | Sorry, say that again? |
| question | how many wells have I finished | 2871 | 2780 | 2 | spark | read_record | C2 is done. C3 is done. C4 is planned. Memos: slight edge ef |

questions_under_4s: True, commands_under_3s: True

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 1007 | 919 | spark | mark_done | Nice, C2–C3 are in. |
| note C3, big bubbles near the edge | 79 | 0 | grammar | add_note | Noted on C3. |
| start a 25 minute timer for C4 | 908 | 857 | spark | set_timer | 25 minutes on C4. I'll ping you. |
| how many wells have I finished | 3926 | 3838 | spark | read_record,answer | Nice, C2–C3 done, C3 has big bubbles noted, C4–C8 still plan |
| D5 looks cloudy, drop it from the run | 1123 | 1038 | spark | exclude_well | Drop D5? |
| yes(confirm) | 5 | 0 | confirm | - | D5 excluded (cloudy) |
| C6 has a bubble, redo it in a backup well | 2735 | 2640 | spark | find_free_wells,exclude_well,record_redo | Exclude C6? Redo C9? |
| what is on C6 | 5592 | 5502 | spark | read_record | Sorry, say that again? |
| cancel the C4 timer | 80 | 0 | grammar | manage_timers | Okay, C4 timer's off. |
| mark C4 through C8 done | 709 | 651 | spark | mark_done | Got it — C4–C8 done. |

all_under_3s: False

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 1247 | 1169 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 70 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 564 | 511 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 3291 | 3204 | 2 | spark | read_record,exclude_well,record_redo | Drop C4? Redo C6? |
| question | read all my notes | 2531 | 2447 | 2 | spark | read_record | Sorry, say that again? |
| question | how many wells have I finished | 2336 | 2243 | 2 | spark | read_record | I couldn't read that: unknown section. |

questions_under_4s: True, commands_under_3s: True

> **NOTE: eval scripts run in-process via TestClient with a fresh temp store — they always test the
> working tree, never the port-8000 server. A stale-server worry was raised and retracted; the tables
> above are valid for the tree state at run time. (Only direct-HTTP probes like /tmp/r2cap.py hit the
> live server.)

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 640 | 545 | spark | mark_done | Nice, C2–C3 are in. |
| note C3, big bubbles near the edge | 72 | 0 | grammar | add_note | Noted on C3. |
| start a 25 minute timer for C4 | 651 | 598 | spark | set_timer | 25 minutes on C4. I'll ping you. |
| how many wells have I finished | 2482 | 2396 | spark | read_record,answer | Nice, C2-C3 done, C3 has big bubbles, C4-C8 still planned. |
| D5 looks cloudy, drop it from the run | 667 | 578 | spark | exclude_well | Drop D5? |
| yes(confirm) | 5 | 0 | confirm | - | D5 excluded (looks cloudy) |
| C6 has a bubble, redo it in a backup well | 3729 | 3637 | spark | find_free_wells,exclude_well,record_redo | Exclude C6? Redo C9? |
| what is on C6 | 2024 | 1937 | spark | read_record | Got it — that lookup hiccuped, let me retry the backup searc |
| cancel the C4 timer | 76 | 0 | grammar | manage_timers | Okay, C4 timer's off. |
| mark C4 through C8 done | 894 | 843 | spark | mark_done | Got it — C4–C8 done. |

all_under_3s: False

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 858 | 758 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 79 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 1282 | 1232 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 4657 | 4564 | 2 | spark | read_record,exclude_well,record_redo | Drop C4? Redo C6? |
| question | read all my notes | 3150 | 3058 | 2 | spark | read_record,answer | Hmm, C4 just has slight edge effect noted, nothing on C6 in  |
| question | how many wells have I finished | 2222 | 2140 | 2 | spark | read_record,answer | Hmm, that lookup didn't come through, still just slight edge |

questions_under_4s: False, commands_under_3s: True

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 795 | 701 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 107 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 1013 | 957 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 1433 | 1344 | 2 | spark | read_record | I'll check the record for C6. |
| question | read all my notes | 1968 | 1881 | 2 | spark | read_record | Got it — pulling all your notes now. |
| question | how many wells have I finished | 1857 | 1765 | 2 | spark | read_record | Got it — checking how many you've finished., but i couldn't  |

questions_under_4s: True, commands_under_3s: True

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 5475 | 5379 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 74 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 765 | 712 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 2604 | 2536 | 2 | spark | read_record,answer | C6 is drug_A, planned, nothing noted yet. |
| question | read all my notes | 1846 | 1768 | 2 | spark | read_record,answer | Got it — just one note: C4, slight edge effect. |
| question | how many wells have I finished | 2232 | 2140 | 2 | spark | read_record,answer | Got it — 2 finished, C2–C3. |

questions_under_4s: True, commands_under_3s: False

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 801 | 709 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 78 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 668 | 618 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 1519 | 1430 | 2 | spark | read_record,answer | Got it — C6 is drug_A, planned. |
| question | read all my notes | 1258 | 1168 | 2 | spark | read_record,answer | Got it — C4: slight edge effect. |
| question | how many wells have I finished | 1795 | 1716 | 2 | spark | read_record,answer | Nice — 2 done, C2–C3. |

questions_under_4s: True, commands_under_3s: True

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 845 | 741 | spark | mark_done | Nice, C2–C3 are in. |
| note C3, big bubbles near the edge | 72 | 0 | grammar | add_note | Noted on C3. |
| start a 25 minute timer for C4 | 994 | 936 | spark | set_timer | 25 minutes on C4. I'll ping you. |
| how many wells have I finished | 1688 | 1595 | spark | read_record,answer | Nice, 2 done — C2–C3. |
| D5 looks cloudy, drop it from the run | 724 | 634 | spark | exclude_well | Drop D5? |
| yes(confirm) | 6 | 0 | confirm | - | D5 excluded (looks cloudy) |
| C6 has a bubble, redo it in a backup well | 2141 | 2054 | spark | find_free_wells,exclude_well,record_redo | Exclude C6? Redo C9? |
| what is on C6 | 1450 | 1361 | spark | read_record,answer | Nice, C6 is drug_A, planned. |
| cancel the C4 timer | 76 | 0 | grammar | manage_timers | Okay, C4 timer's off. |
| mark C4 through C8 done | 1029 | 946 | spark | mark_done | Got it — C4–C8 done. |

all_under_3s: True

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 855 | 767 | spark | mark_done | Nice, C2–C3 are in. |
| note C3, big bubbles near the edge | 79 | 0 | grammar | add_note | Noted on C3. |
| start a 25 minute timer for C4 | 1144 | 1086 | spark | set_timer | 25 minutes on C4. I'll ping you. |
| how many wells have I finished | 1641 | 1551 | spark | read_record,answer | 2 wells done — C2 and C3. |
| D5 looks cloudy, drop it from the run | 676 | 589 | spark | exclude_well | Drop D5? |
| yes(confirm) | 6 | 0 | confirm | - | D5 excluded (cloudy) |
| C6 has a bubble, redo it in a backup well | 2144 | 2055 | spark | find_free_wells,exclude_well,record_redo | Exclude C6? Redo C9? |
| what is on C6 | 2051 | 1960 | spark | read_record,answer | Got it — C6 is drug_A, still planned. |
| cancel the C4 timer | 76 | 0 | grammar | manage_timers | Okay, C4 timer's off. |
| mark C4 through C8 done | 606 | 552 | spark | mark_done | Got it — C4–C8 done. |

all_under_3s: True

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 874 | 795 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 74 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 1101 | 1047 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 1616 | 1521 | 2 | spark | read_record,answer | Got it — C6 is drug_A, planned. |
| question | read all my notes | 1747 | 1661 | 2 | spark | read_record,answer | Got it — C4: slight edge effect. |
| question | how many wells have I finished | 1499 | 1411 | 2 | spark | read_record,answer | Got it — 2 wells finished, C2–C3. |

questions_under_4s: True, commands_under_3s: True

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 741 | 672 | spark | mark_done | Nice, C2–C3 are in. |
| note C3, big bubbles near the edge | 46 | 0 | grammar | add_note | Noted on C3. |
| start a 25 minute timer for C4 | 867 | 822 | spark | set_timer | 25 minutes on C4. I'll ping you. |
| how many wells have I finished | 1702 | 1656 | spark | read_record,answer | 2 done — C2 and C3. |
| D5 looks cloudy, drop it from the run | 1101 | 1056 | spark | exclude_well | Drop D5? |
| yes(confirm) | 1 | 0 | confirm | - | D5 excluded (cloudy) |
| C6 has a bubble, redo it in a backup well | 2004 | 1952 | spark | find_free_wells,exclude_well,record_redo | Exclude C6? Redo C9? |
| what is on C6 | 1464 | 1417 | spark | read_record,answer | Hmm, C6 is planned for drug A, no notes. |
| cancel the C4 timer | 46 | 0 | grammar | manage_timers | Okay, C4 timer's off. |
| mark C4 through C8 done | 842 | 796 | spark | mark_done | Got it — C4–C8 done. |

all_under_3s: True

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 836 | 766 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 45 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 648 | 603 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 1517 | 1471 | 2 | spark | read_record,answer | Got it — C6 is planned for Drug A, not done yet. |
| question | read all my notes | 1594 | 1548 | 2 | spark | read_record,answer | Got it — C4: slight edge effect. |
| question | how many wells have I finished | 2584 | 2536 | 2 | spark | read_record,answer | Got it — 2 done, C2–C3. |

questions_under_4s: True, commands_under_3s: True

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 638 | 551 | spark | mark_done | Nice, C2–C3 are in. |
| note C3, big bubbles near the edge | 76 | 0 | grammar | add_note | Noted on C3. |
| start a 25 minute timer for C4 | 636 | 584 | spark | set_timer | 25 minutes on C4. I'll ping you. |
| how many wells have I finished | 1087 | 1000 | spark | read_record,answer | Got it — 2 done, C2–C3. |
| D5 looks cloudy, drop it from the run | 906 | 821 | spark | exclude_well | Drop D5? |
| yes(confirm) | 4 | 0 | confirm | - | D5 excluded (cloudy) |
| C6 has a bubble, redo it in a backup well | 2755 | 2674 | spark | find_free_wells,exclude_well,record_redo | Exclude C6? Redo C9? |
| what is on C6 | 1387 | 1327 | spark | read_record,answer | Got it — C6 is planned Drug A. |
| cancel the C4 timer | 71 | 0 | grammar | manage_timers | Okay, C4 timer's off. |
| mark C4 through C8 done | 672 | 620 | spark | mark_done | Got it — C4–C8 done. |

all_under_3s: True

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 650 | 551 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 74 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 822 | 770 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 2560 | 2467 | 2 | spark | read_record,answer | Nice — C6 has Drug A, still to do. |
| question | read all my notes | 1720 | 1641 | 2 | spark | read_record,answer | Nice — 1 note: C4, slight edge effect. |
| question | how many wells have I finished | 1423 | 1332 | 2 | spark | read_record,answer | Nice — 2 finished: C2–C3. |

questions_under_4s: True, commands_under_3s: True

## Read vs command latency (goals: questions <= 4000ms, commands <= 3000ms)

One session, live muse-spark-1.3 minimal. rounds = spark calls this turn.

| kind | turn | total_ms | spark_ms | rounds | route | calls | reply |
|---|---|---|---|---|---|---|---|
| command | mark C2 and C3 done please | 2193 | 2076 | 1 | spark | mark_done | Nice, C2–C3 are in. |
| command | note C4, slight edge effect | 74 | 0 | 0 | grammar | add_note | Noted on C4. |
| command | start the timer for C5, five minutes | 1417 | 1361 | 1 | spark | set_timer | 5 minutes on C5. I'll ping you. |
| question | what is on C6 | 1715 | 1622 | 2 | spark | read_record | Got it — C6 is Drug A, still planned. |
| question | read all my notes | 2031 | 1940 | 2 | spark | read_record | Got it — one note on C4: slight edge effect. |
| question | how many wells have I finished | 2005 | 1904 | 2 | spark | read_record | Nice — 2 done, C2–C3. |

questions_under_4s: True, commands_under_3s: True

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 2060 | 1945 | spark | mark_done | Nice, C2–C3 are in. |
| note C3, big bubbles near the edge | 77 | 0 | grammar | add_note | Noted on C3. |
| start a 25 minute timer for C4 | 1510 | 1457 | spark | set_timer | 25 minutes on C4. I'll ping you. |
| how many wells have I finished | 1494 | 1407 | spark | read_record | Got it — 2 done, C2 and C3. |
| D5 looks cloudy, drop it from the run | 1588 | 1515 | spark | exclude_well | Drop D5? |
| yes(confirm) | 2 | 0 | confirm | - | D5 excluded (looks cloudy) |
| C6 has a bubble, redo it in a backup well | 3024 | 2955 | spark | read_record,exclude_well | Exclude C6? |
| what is on C6 | 2588 | 2503 | spark | read_record | Got it — C6 is planned for Drug A, still to do. |
| cancel the C4 timer | 81 | 0 | grammar | cancel_timer | Okay, C4 timer's off. |
| mark C4 through C8 done | 2570 | 2518 | spark | mark_done | Got it — C4–C8 done. |

all_under_3s: False

## 10-turn conversation (summary mode, goal: every turn <= 3000ms)

One session, live muse-spark-1.3 minimal. total = HTTP round trip, spark = model time inside it.

| turn | total_ms | spark_ms | route | calls | reply |
|---|---|---|---|---|---|
| mark C2 and C3 done please | 1775 | 1691 | spark | mark_done | Nice, C2–C3 are in. |
| note C3, big bubbles near the edge | 64 | 0 | grammar | add_note | Noted on C3. |
| start a 25 minute timer for C4 | 1512 | 1462 | spark | set_timer | 25 minutes on C4. I'll ping you. |
| how many wells have I finished | 3277 | 3195 | spark | read_record | Nice, you've got 2 in — C2–C3. |
| D5 looks cloudy, drop it from the run | 1454 | 1388 | spark | exclude_well | Drop D5? |
| yes(confirm) | 5 | 0 | confirm | - | D5 excluded (looks cloudy) |
| C6 has a bubble, redo it in a backup well | 3054 | 2956 | spark | read_record,exclude_well | Exclude C6? |
| what is on C6 | 1901 | 1831 | spark | read_record | Got it — C6 is planned Drug A. |
| cancel the C4 timer | 77 | 0 | grammar | cancel_timer | Okay, C4 timer's off. |
| mark C4 through C8 done | 2155 | 2101 | spark | mark_done | Got it — C4–C8 done. |

all_under_3s: False

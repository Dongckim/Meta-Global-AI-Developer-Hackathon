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

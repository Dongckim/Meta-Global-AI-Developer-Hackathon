# Effort comparison (2026-10-27 05:44 UTC)

4 sentences x 3 configs x 3 repeats = 36 live calls. Tools-ok = expected tool names present, no truncation.
Code: model muse-spark-1.3, effort minimal, max_output_tokens 1024.

| config | correct/12 | ms median | out_tok median | reason_tok median |
|---|---|---|---|---|
| low | 10/12 | 2575 | 662 | 502 |
| minimal | 11/12 | 1800 | 334 | 174 |
| contributor | 12/12 | 3253 | 348 | 189 |

## misses

### low (2)
- cancel the wash timer and extend the rest timer by five minutes -> got []
- mark D2 done and list my timers -> got ['mark_done']

### minimal (1)
- mark D2 done and list my timers -> got ['mark_done']

### contributor (0)

# Muse Glimmer on Modal

Private mode's model: llama.cpp with DFlash and `--reasoning-budget 0`, behind
the guard in `glimmer_modal.py`. There is no key, so the URL is held by the
guard, `max_containers=1` and the Modal workspace budget.

**Do not redeploy during a demo or filming.** A request that lands while the
container is replaced can fail with a 500.

Run every command from the repo root. Each deploy reads both settings, and a
setting left out goes back to its default, so name both when they aren't
default.

## Change the GPU

```bash
.venv/bin/modal deploy server/private/glimmer_modal.py                        # L40S (default)
GLIMMER_GPU=H100 .venv/bin/modal deploy server/private/glimmer_modal.py
GLIMMER_GPU=A100_80 .venv/bin/modal deploy server/private/glimmer_modal.py
GLIMMER_GPU=A10 .venv/bin/modal deploy server/private/glimmer_modal.py
```

## Keep warm

One container stays on: no cold start, but billed all day.

```bash
GLIMMER_KEEP_WARM=1 .venv/bin/modal deploy server/private/glimmer_modal.py    # on
.venv/bin/modal deploy server/private/glimmer_modal.py                        # off (default)
```

Stop everything: `.venv/bin/modal app stop glassistant-glimmer`. A stopped app
doesn't answer until the next deploy.

## Cost and speed

GPU price only, from modal.com/pricing on 2026-10-30; CPU and memory add a
little. "Per day" is the keep warm cost. Without keep warm you pay only while
a container is up, plus 120 s of idle after the last request.

Measured 2026-10-30 with DFlash: 5 voice turns twice, one model round, and one
label read. Full table: `eval/results/private_glimmer.md` (not committed).

| GLIMMER_GPU | per hour | per day | turn total (median) | label | cold start |
|---|---|---|---|---|---|
| L40S (default) | $1.95 | $46.80 | 1.7 s | 1.8 s | 19–70 s |
| A10 | $1.10 | $26.40 | 2.3 s | 3.2–3.5 s | 15–29 s |
| H100 | $3.95 | $94.80 | 2.1 s | 12.8 s | 111–121 s |
| A100_80 | $2.50 | $60.00 | 2.5 s | 15.8 s | 132–141 s |

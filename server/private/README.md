# Muse Glimmer on Modal

Private mode's model: Meta's own GGUFs (`meta-models/Muse-Glimmer-30B-GGUF`,
revision pinned in `glimmer_modal.py`) on llama.cpp, with the DFlash drafter and
`--reasoning-budget 0`, behind the guard in `glimmer_modal.py`. There is no key,
so the URL is held by the guard, `max_containers=1` and the Modal workspace
budget.

What runs here: voice commands only (`_call_spark` through
`server/private_chat.py`), transcribed on the iPad. Imports, hand off, photo
questions and search summaries stay on Spark; search, photos and imports ask
first. Say it as "not sent to the Meta API, processed on the Meta model we run",
never "on device". Choose Private on the Mode screen: the choice is saved in
`run_mode.txt` next to the record and survives a restart.

Changing the weights: edit the constants and `pip_voice.json` `private.files`,
then `.venv/bin/modal run server/private/glimmer_modal.py::download` and deploy.

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
label read; L40S's turn is the real path, cache on (`[private]` lines log each
round). Only L40S was measured on Meta's weights; the others are the earlier
unsloth build. Full table: `eval/results/private_glimmer.md` (not committed).

| GLIMMER_GPU | per hour | per day | turn total (median) | label | cold start |
|---|---|---|---|---|---|
| L40S (default) | $1.95 | $46.80 | 2.6–2.8 s real turn | 1.9 s | 20–25 s |
| A10 | $1.10 | $26.40 | 2.3 s | 3.2–3.5 s | 15–29 s |
| H100 | $3.95 | $94.80 | 2.1 s | 12.8 s | 111–121 s |
| A100_80 | $2.50 | $60.00 | 2.5 s | 15.8 s | 132–141 s |

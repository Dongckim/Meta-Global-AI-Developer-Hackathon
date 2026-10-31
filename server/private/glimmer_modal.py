"""Glassistant · Muse Glimmer on Modal (llama.cpp, public HTTPS, no key).

Judges run our server on their own Macs, so a key can't ship with it: the
public URL is held by the guard here, max_containers=1 and the workspace budget.

Run from the repo root:
  .venv/bin/modal run server/private/glimmer_modal.py::download
  .venv/bin/modal deploy server/private/glimmer_modal.py
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque

import httpx
import modal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

REPO = "meta-models/Muse-Glimmer-30B-GGUF"
REVISION = "b44f1072cf75e918a1b82a3a17b45c4389b076e7"
MODEL_FILE = "Muse-Glimmer-30B-KQuant-Dynamic-Q4_K_XL.gguf"
MMPROJ_FILE = "mmproj-Muse-Glimmer-30B-Q4_K_M.gguf"
DRAFT_FILE = "dflash-Muse-Glimmer-30B-Q4_K_M.gguf"
WEIGHTS = "/weights"
#: Read when deploying: GLIMMER_GPU=<key> modal deploy ... (a typo fails here).
GPUS = {"L40S": "L40S", "H100": "H100", "A100_80": "A100-80GB", "A10": "A10"}
GPU = GPUS[os.environ.get("GLIMMER_GPU", "L40S")]
#: GLIMMER_KEEP_WARM=1 keeps one container on: no cold start, billed all day.
MIN_CONTAINERS = 1 if os.environ.get("GLIMMER_KEEP_WARM") == "1" else 0
TUNING = [
    "--chat-template-kwargs", '{"reasoning_strength":"low"}', "--reasoning-budget", "0",
    "--model-draft", f"{WEIGHTS}/{DRAFT_FILE}", "-ngld", "99", "--spec-type", "draft-dflash",
    "--spec-draft-n-max", "15", "-fa", "on",
]
#: Live session window is 60 s (pip_voice.json): idle shorter than that would
#: put the model to sleep in the middle of a conversation.
SCALEDOWN_S = 120
PORT = 8080
UPSTREAM = f"http://127.0.0.1:{PORT}/v1/chat/completions"

MAX_TOKENS = 2048
MAX_BODY_BYTES = 16_000_000
MAX_TEXT_CHARS = 60_000  # about the context below, images not counted
MAX_MESSAGES = 64
MAX_IMAGES = 4
RATE_PER_MIN = 30
CTX_SIZE = 16384
#: Everything else is dropped: n_predict would bypass max_tokens, n fans out.
ALLOWED_KEYS = frozenset({
    "model", "messages", "tools", "tool_choice", "parallel_tool_calls",
    "stream", "stream_options", "max_tokens", "temperature", "top_p", "top_k",
    "reasoning_effort", "response_format", "cache_prompt",
})

app = modal.App("glassistant-glimmer")
volume = modal.Volume.from_name("glimmer-weights", create_if_missing=True)
# llama.cpp server-cuda build 12004 (Glimmer needs b10353+), pinned: the tag moves with every build
image = (
    modal.Image.from_registry(
        "ghcr.io/ggml-org/llama.cpp@sha256:"
        "f25bfa096802cdf35ceb7d124816387a1e3ed5cbfc869eb242246cd29ab29352",
        add_python="3.12",
    )
    .entrypoint([])
    .pip_install("fastapi", "httpx", "huggingface_hub")
)


def clean_body(raw: bytes) -> dict:
    """The request llama.cpp sees: allowed keys only, tokens capped, sizes
    checked, images inline only (a remote image_url makes the server fetch it)."""
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "body is not JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be an object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages required")
    if len(messages) > MAX_MESSAGES:
        raise HTTPException(413, "too many messages")
    images = image_chars = 0
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        for part in content if isinstance(content, list) else []:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "image_url":
                url = part.get("image_url")
                url = url.get("url") if isinstance(url, dict) else None
                if not isinstance(url, str) or not url.startswith("data:image/"):
                    raise HTTPException(400, "images must be data:image URLs")
                images += 1
                image_chars += len(url)
            elif kind != "text":
                raise HTTPException(400, "unsupported content part")
    if images > MAX_IMAGES:
        raise HTTPException(413, "too many images")
    if len(raw) - image_chars > MAX_TEXT_CHARS:
        raise HTTPException(413, "input too long")
    wanted = body.get("max_tokens")
    wanted = wanted if type(wanted) is int and wanted > 0 else MAX_TOKENS
    return {k: v for k, v in body.items() if k in ALLOWED_KEYS} | {
        "max_tokens": min(wanted, MAX_TOKENS)}


class RateLimit:
    """Sliding minute per IP. Memory is one container's: max_containers=1
    makes that the whole service."""

    def __init__(self, per_min: int = RATE_PER_MIN, clock=time.monotonic):
        self.per_min, self.clock, self.hits = per_min, clock, {}

    def allow(self, ip: str) -> bool:
        now = self.clock()
        q = self.hits.setdefault(ip, deque())
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= self.per_min:
            return False
        q.append(now)
        return True


async def read_capped(request: Request) -> bytes:
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        raise HTTPException(413, "body too large")
    raw = bytearray()
    async for chunk in request.stream():  # content-length can lie
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(413, "body too large")
    return bytes(raw)


def make_guard(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    guard = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    limit = RateLimit()
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(300.0))

    @guard.post("/v1/chat/completions")
    async def chat(request: Request):
        if not limit.allow(request.client.host):  # not X-Forwarded-For: clients write that
            return JSONResponse({"error": "rate limited"}, 429, {"Retry-After": "60"})
        body = clean_body(await read_capped(request))
        up = await client.send(client.build_request("POST", UPSTREAM, json=body), stream=True)
        return StreamingResponse(
            up.aiter_raw(), up.status_code, media_type=up.headers.get("content-type"),
            background=BackgroundTask(up.aclose))

    return guard


@app.function(image=image, volumes={WEIGHTS: volume}, timeout=3600)
def download():
    from huggingface_hub import hf_hub_download

    for name in (MODEL_FILE, MMPROJ_FILE, DRAFT_FILE):
        print(hf_hub_download(REPO, name, revision=REVISION, local_dir=WEIGHTS), flush=True)
    volume.commit()


@app.cls(image=image, gpu=GPU, volumes={WEIGHTS: volume}, min_containers=MIN_CONTAINERS,
         max_containers=1, scaledown_window=SCALEDOWN_S, startup_timeout=900, timeout=600)
@modal.concurrent(max_inputs=8)
class Glimmer:
    @modal.enter()
    def start(self):
        t0 = time.perf_counter()
        self.proc = subprocess.Popen([
            "/app/llama-server", "--host", "127.0.0.1", "--port", str(PORT),
            "--model", f"{WEIGHTS}/{MODEL_FILE}", "--mmproj", f"{WEIGHTS}/{MMPROJ_FILE}",
            "--jinja", "--n-gpu-layers", "99", "--ctx-size", str(CTX_SIZE),
            "--parallel", "1", "--no-webui", *TUNING,
            "--temp", "1.0", "--top-p", "0.95", "--top-k", "64",  # model card
        ], env=os.environ | {"LD_LIBRARY_PATH": "/app"})
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited {self.proc.returncode}")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
                break
            except (urllib.error.URLError, TimeoutError):  # 503 while loading
                time.sleep(0.5)
        print(f"[glimmer] ready in {time.perf_counter() - t0:.1f}s", flush=True)

    @modal.exit()
    def stop(self):
        self.proc.terminate()

    @modal.asgi_app()
    def web(self):
        return make_guard()

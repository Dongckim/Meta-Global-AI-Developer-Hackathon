"""One line per real model call, the same shape from every caller:

  [model] muse-spark-1.3 · import · 4210 ms · 200 · in 3409 / out 81 tok

Recorded mode makes no calls, so it prints none.
"""

from __future__ import annotations


def line(model: str, name: str, ms: int, status: int, usage: dict | None = None) -> str:
    out = f"[model] {model or '?'} · {name} · {ms} ms · {status}"
    u = usage or {}
    i = u.get("input_tokens", u.get("prompt_tokens"))
    o = u.get("output_tokens", u.get("completion_tokens"))
    if i is None and o is None:
        return out
    return out + f" · in {'?' if i is None else i} / out {'?' if o is None else o} tok"


def usage_of(resp) -> dict:
    """The usage block of a JSON reply; a stream or an error has none."""
    headers = getattr(resp, "headers", None) or {}  # a log line never breaks the call it reports
    if "json" not in headers.get("content-type", ""):
        return {}
    try:
        return resp.json().get("usage") or {}
    except (ValueError, AttributeError):
        return {}

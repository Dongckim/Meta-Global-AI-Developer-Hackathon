"""A question that needs a photo ("what does this say?", "is this tube the
right one?"): the glasses (else the iPad camera) take one photo, SAM 3.1
finds the thing asked about, the server crops it, and Spark reads only the
crop and answers. The original photo never goes to Spark and is not kept;
the crop is kept with the answer (data/looks/<id>.jpg).

What the Model API gives (dev.meta.ai/docs/sam/segmenting, reading-segmentation):
- model "sam-3.1" on POST /v1/responses, streamed; input_text = one short
  noun phrase ("label"), input_image = a data URL.
- Output, one line per image: <0f>N<|box;x1;y1;x2;y2;w;h[;c]|><|mask;x=0;y=0;
  data=H,W,~payload|>, records comma-separated; no match = empty text.
- The mask raster covers the box; its codec is Meta's (meta-sam-parser,
  server/requirements.txt). Without that package the outline is the box.
"""

from __future__ import annotations

import base64
import io
import json
import re

SAM_MODEL = "sam-3.1"
#: What SAM looks for when the question doesn't name a thing.
DEFAULT_TARGET = "label"
#: Things a question can name (the first one said is the prompt).
TARGETS = ("label", "tube", "bottle", "vial", "flask", "plate", "box", "jar", "bag", "container",
           "reagent", "sample", "pipette", "tip", "screen", "sticker", "cap", "lid", "sheet", "page")
#: Room around the crop, as a share of the box (the edge letters stay in).
CROP_PAD = 0.06
OUTLINE_POINTS = 48

READ_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["read", "answer_short", "detail", "needs_search", "query"],
    "properties": {"read": {"type": "string"}, "answer_short": {"type": "string"},
                   "detail": {"type": "string"}, "needs_search": {"type": "boolean"},
                   "query": {"type": "string"}},
}


def target_of(question: str) -> str:
    """SAM's prompt: the thing the question names ("this tube" -> "tube"), else "label"."""
    words = re.findall(r"[a-z]+", (question or "").lower())
    for w in words:
        base = w[:-1] if w.endswith("s") and w[:-1] in TARGETS else w
        if base in TARGETS:
            return base
    return DEFAULT_TARGET


def data_url(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def build_sam_body(target: str, image_url: str) -> dict:
    return {"model": SAM_MODEL, "stream": True, "metadata": {"include_confidence": "true"},
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": target},
                {"type": "input_image", "image_url": image_url}]}]}


def sse_text(raw: str) -> str:
    """The output text of a streamed /responses body (its text deltas)."""
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line.startswith("data:") or line[5:].strip() in ("", "[DONE]"):
            continue
        try:
            ev = json.loads(line[5:])
        except ValueError:
            continue
        if ev.get("type") == "response.output_text.delta":
            out.append(ev.get("delta") or "")
    return "".join(out)


_BOX = re.compile(r"<\|box;([^|]*)\|>")
_MASK = re.compile(r"<\|mask;([^|]*)\|>")


def _fields(s: str) -> dict:
    out = {}
    for part in s.split(";"):
        k, _, v = part.partition("=")
        out[k] = v
    return out


def matches(text: str) -> list[dict]:
    """SAM's records: [{box: (x1, y1, x2, y2), w, h, c, mask}], best first
    (confidence, then area)."""
    out = []
    for rec in re.split(r",(?=\d+<\|box)", (text or "").replace("<0f>", "")):
        b = _BOX.search(rec)
        if not b:
            continue
        f = _fields(b.group(1))
        try:
            box = tuple(int(f[k]) for k in ("x1", "y1", "x2", "y2"))
            w, h = int(f["w"]), int(f["h"])
        except (KeyError, ValueError):
            continue
        m = _MASK.search(rec)
        out.append({"box": box, "w": w, "h": h, "c": float(f.get("c") or 0),
                    "mask": _fields(m.group(1)) if m else None})
    area = lambda r: (r["box"][2] - r["box"][0] + 1) * (r["box"][3] - r["box"][1] + 1)  # noqa: E731
    return sorted(out, key=lambda r: (r["c"], area(r)), reverse=True)


def outline(match: dict) -> list[list[int]]:
    """The thing's outline in source pixels (left edge down, right edge up),
    from its mask; the box's corners when the mask can't be read."""
    x1, y1, x2, y2 = match["box"]
    corners = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    m = match.get("mask") or {}
    try:
        from meta_sam_parser import decode_mask_to_raster  # Meta's codec (requirements.txt)
        hh, ww, payload = (m.get("data") or "").split(",", 2)
        rows, cols = int(hh), int(ww)
        raster = decode_mask_to_raster(_parser_mask(payload, cols, rows))
    except Exception:  # package missing, or a mask it can't read: the box stands
        return corners
    left, right = [], []
    step = max(1, rows // (OUTLINE_POINTS // 2))
    for r in range(0, rows, step):
        on = [c for c in range(cols) if raster[r * cols + c]]
        if on:
            y = y1 + round(r * (y2 - y1 + 1) / rows)
            left.append([x1 + round(on[0] * (x2 - x1 + 1) / cols), y])
            right.append([x1 + round((on[-1] + 1) * (x2 - x1 + 1) / cols), y])
    return left + right[::-1] if len(left) >= 2 else corners


def _parser_mask(payload: str, width: int, height: int):
    """The parser's mask object ("data=H,W,~payload": rows H, columns W, marker kept)."""
    from meta_sam_parser import SegmentationMask
    return SegmentationMask(encoding="lossless" if payload.startswith("~") else "one_bit",
                            payload=payload, width=width, height=height)


def crop(raw: bytes, box: tuple, size: tuple) -> tuple[bytes, tuple]:
    """The box (plus a little room) cut from the photo, as JPEG, and that
    area in SAM's pixels (to place the outline on the crop)."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    sx, sy = img.width / size[0], img.height / size[1]  # SAM's w/h vs the bytes we hold
    x1, y1, x2, y2 = box
    px, py = (x2 - x1) * CROP_PAD, (y2 - y1) * CROP_PAD
    area = (max(0, int((x1 - px) * sx)), max(0, int((y1 - py) * sy)),
            min(img.width, int((x2 + px) * sx) + 1), min(img.height, int((y2 + py) * sy) + 1))
    out = io.BytesIO()
    img.crop(area).save(out, "JPEG", quality=88)
    return out.getvalue(), (area[0] / sx, area[1] / sy, area[2] / sx, area[3] / sy)


def build_read_body(question: str, target: str, crop_url: str, model: str) -> dict:
    """Spark sees only the crop: read what's written, answer from it; say
    when the answer needs outside knowledge (then the web search path runs)."""
    return {
        "model": model,
        "instructions": (f"The image is a {target} cropped from a lab photo. Read every word and number on it "
                         "exactly as written, then answer the question from what you read. Never guess "
                         "text you can't read. If answering needs facts the image doesn't show, set "
                         "needs_search and give a short web query (no lab names, no note text)."),
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": f"Question: {question}\nread: the text on it. answer_short: "
                                            "at most 8 words. detail: at most 2 short sentences."},
            {"type": "input_image", "image_url": crop_url}]}],
        "reasoning": {"effort": "minimal"},
        "max_output_tokens": 1200,
        "text": {"format": {"type": "json_schema", "name": "PhotoRead", "schema": READ_SCHEMA, "strict": True}},
        "store": False,
    }

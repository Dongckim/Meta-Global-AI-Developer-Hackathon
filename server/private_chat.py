"""Private voice turns: the Responses request `_call_spark` builds, as
chat/completions for Glimmer, and its answer back in Responses shape. The turn
around it doesn't know which model answered.
"""


def to_chat(body: dict) -> dict:
    """The same instructions, input and tools (memory and caching stay behind)."""
    messages = [{"role": "system", "content": body["instructions"]}]
    items = body["input"]
    for item in [{"role": "user", "content": items}] if isinstance(items, str) else items:
        if item.get("type") == "function_call":
            call = {"name": item["name"], "arguments": item["arguments"]}
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": item["call_id"], "type": "function", "function": call}]})
        elif item.get("type") == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        else:
            messages.append({"role": item["role"], "content": item["content"]})
    tools = [{"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}}
             for t in body["tools"]]
    return {"messages": messages, "tools": tools, "max_tokens": body["max_output_tokens"]}


def from_chat(payload: dict) -> dict:
    """What `_call_spark` reads from a Responses payload. No id: there's no
    response to chain onto."""
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output = [{"type": "function_call", "name": c["function"]["name"],
               "arguments": c["function"]["arguments"], "call_id": c.get("id") or f"call_{i}"}
              for i, c in enumerate(message.get("tool_calls") or [])]
    if message.get("content"):
        output.append({"type": "message", "content": [{"type": "output_text", "text": message["content"]}]})
    cut = choice.get("finish_reason") == "length"
    return {"output": output, "usage": payload.get("usage") or {},
            "status": "incomplete" if cut else "completed",
            "incomplete_details": {"reason": "max_output_tokens"} if cut else None}

"""On-demand re-symbolication of a stored event, Countly's "Symbolicate" button.

Sentry symbolicates at ingest and does not touch old events again. When symbols
arrive late we can still show the developer a resolved stack trace: take the raw
event JSON, hand its native frames and debug images to Symbolicator (already
running in the stack) together with the project's debug files as a source, and
render the answer. The stored event is not modified.
"""

from __future__ import annotations

from typing import Any

NATIVE_IMAGE_TYPES = {"macho", "elf", "pe", "wasm", "sourcemap"}


def native_sources(sentry_internal_url: str, org: str, project: str, token: str, extra: list[dict] | None = None) -> list[dict]:
    sources = [
        {
            "id": "sentry:project",
            "type": "sentry",
            "url": f"{sentry_internal_url.rstrip('/')}/api/0/projects/{org}/{project}/files/dsyms/",
            "token": token,
        }
    ]
    sources.extend(extra or [])
    return sources


def _hex(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return hex(value)
    text = str(value)
    return text if text.startswith("0x") else hex(int(text))


def collect_stacktraces(event: dict) -> list[dict]:
    """Every stack trace in the event that carries instruction addresses, with a label."""
    found: list[dict] = []

    def add(label: str, stacktrace: dict | None) -> None:
        frames = (stacktrace or {}).get("frames") or []
        if not any(f.get("instruction_addr") is not None for f in frames):
            return
        found.append({"label": label, "registers": (stacktrace or {}).get("registers") or {}, "frames": frames})

    for i, exc in enumerate((event.get("exception") or {}).get("values") or []):
        label = f"exception {i}: {exc.get('type') or ''} {exc.get('value') or ''}".strip()
        add(label, exc.get("raw_stacktrace") or exc.get("stacktrace"))
    for thread in (event.get("threads") or {}).get("values") or []:
        if thread.get("crashed") and any(s["label"].startswith("exception") for s in found):
            continue  # the crashing thread duplicates the exception stack trace
        label = f"thread {thread.get('id')}" + (f" {thread.get('name')}" if thread.get("name") else "")
        add(label, thread.get("raw_stacktrace") or thread.get("stacktrace"))
    if not found:
        add("stacktrace", event.get("stacktrace"))
    return found


def build_request(event: dict, sources: list[dict]) -> dict | None:
    """Symbolicator /symbolicate payload, or None when the event has nothing native to resolve."""
    images = (event.get("debug_meta") or {}).get("images") or []
    modules = []
    for image in images:
        if image.get("type") not in NATIVE_IMAGE_TYPES:
            continue
        module = {
            "type": image["type"],
            "debug_id": image.get("debug_id"),
            "code_id": image.get("code_id"),
            "code_file": image.get("code_file") or image.get("name"),
            "debug_file": image.get("debug_file"),
            "image_addr": _hex(image.get("image_addr")),
            "image_size": image.get("image_size"),
            "arch": image.get("arch"),
        }
        modules.append({k: v for k, v in module.items() if v is not None})

    stacktraces = collect_stacktraces(event)
    if not stacktraces or not modules:
        return None

    payload_traces = []
    for trace in stacktraces:
        frames = []
        for frame in trace["frames"]:
            if frame.get("instruction_addr") is None:
                continue
            entry = {"instruction_addr": _hex(frame["instruction_addr"])}
            if frame.get("addr_mode"):
                entry["addr_mode"] = frame["addr_mode"]
            if frame.get("function_id"):
                entry["function_id"] = frame["function_id"]
            frames.append(entry)
        payload_traces.append({"registers": {k: _hex(v) for k, v in trace["registers"].items()}, "frames": frames})

    contexts = event.get("contexts") or {}
    request: dict[str, Any] = {
        "platform": event.get("platform") or "native",
        "sources": sources,
        "stacktraces": payload_traces,
        "modules": modules,
        "options": {"dif_candidates": True},
    }
    signal = ((event.get("exception") or {}).get("values") or [{}])[0].get("mechanism", {}).get("meta", {}).get("signal", {}).get("number")
    if signal is not None:
        request["signal"] = signal
    if contexts.get("os") or contexts.get("device"):
        request["os"] = {"name": (contexts.get("os") or {}).get("name"), "version": (contexts.get("os") or {}).get("version")}
    return request


def summarize(event: dict, response: dict) -> dict:
    """Merge Symbolicator's answer with the labels from the event for the UI."""
    labels = [t["label"] for t in collect_stacktraces(event)]
    traces = []
    for i, trace in enumerate(response.get("stacktraces") or []):
        frames = []
        for frame in trace.get("frames") or []:
            frames.append(
                {
                    "status": frame.get("status"),
                    "instruction_addr": frame.get("instruction_addr"),
                    "function": frame.get("function") or frame.get("symbol"),
                    "filename": frame.get("filename") or frame.get("abs_path"),
                    "lineno": frame.get("lineno"),
                    "package": (frame.get("package") or "").rsplit("/", 1)[-1] or None,
                    "in_app": frame.get("in_app"),
                }
            )
        traces.append({"label": labels[i] if i < len(labels) else f"stacktrace {i}", "frames": frames})

    modules = []
    for module in response.get("modules") or []:
        modules.append(
            {
                "debug_id": module.get("debug_id"),
                "code_file": (module.get("code_file") or "").rsplit("/", 1)[-1] or None,
                "debug_status": module.get("debug_status"),
                "arch": module.get("arch"),
            }
        )
    total = sum(len(t["frames"]) for t in traces)
    resolved = sum(1 for t in traces for f in t["frames"] if f["status"] == "symbolicated")
    return {
        "status": response.get("status"),
        "stacktraces": traces,
        "modules": modules,
        "frames_total": total,
        "frames_symbolicated": resolved,
        "missing_debug_ids": sorted({m["debug_id"] for m in modules if m["debug_status"] == "missing" and m["debug_id"]}),
    }

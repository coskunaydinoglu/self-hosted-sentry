"""On-demand re-symbolication of a stored event, Countly's "Symbolicate" button.

Native events (iOS/macOS, Android NDK) go to Symbolicator's /symbolicate endpoint,
Java/Kotlin events obfuscated by R8/ProGuard to /symbolicate-jvm. Either way the
stored event is not modified; the portal only shows the resolved stack trace.

Sentry symbolicates at ingest and does not touch old events again. When symbols
arrive late we can still show the developer a resolved stack trace: take the raw
event JSON, hand its native frames and debug images to Symbolicator (already
running in the stack) together with the project's debug files as a source, and
render the answer. The stored event is not modified.
"""

from __future__ import annotations

from typing import Any

NATIVE_IMAGE_TYPES = {"macho", "elf", "pe", "wasm", "sourcemap"}


def native_sources(
    sentry_internal_url: str,
    org: str,
    project: str,
    token: str,
    extra: list[dict] | None = None,
    version: str | None = None,
) -> list[dict]:
    """Sources for Symbolicator. `version` identifies the set of debug files currently
    uploaded for the event's images: Symbolicator caches lookups (including "not found")
    per source, so a new version makes it look again after a late upload, while an
    unchanged set keeps reusing its cache."""
    url = f"{sentry_internal_url.rstrip('/')}/api/0/projects/{org}/{project}/files/dsyms/"
    source_id = "sentry:project"
    if version:
        source_id = f"sentry:project:{version}"
        url = f"{url}?portal_version={version}"
    sources = [{"id": source_id, "type": "sentry", "url": url, "token": token}]
    sources.extend(extra or [])
    return sources


def debug_files_version(files_by_debug_id: dict[str, list[dict]]) -> str:
    """Short, stable fingerprint of which debug files exist for the given debug ids."""
    import hashlib

    parts = []
    for debug_id in sorted(files_by_debug_id):
        ids = sorted(str(f.get("id") or f.get("uuid") or f.get("sha1") or "") for f in files_by_debug_id[debug_id])
        parts.append(debug_id + "=" + ",".join(ids))
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def native_debug_ids(event: dict) -> list[str]:
    images = (event.get("debug_meta") or {}).get("images") or []
    return sorted({str(i["debug_id"]).lower() for i in images if i.get("type") in NATIVE_IMAGE_TYPES and i.get("debug_id")})


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
        "kind": "native",
        "status": response.get("status"),
        "stacktraces": traces,
        "modules": modules,
        "frames_total": total,
        "frames_symbolicated": resolved,
        "missing_debug_ids": sorted({m["debug_id"] for m in modules if m["debug_status"] == "missing" and m["debug_id"]}),
    }


# -- JVM (R8 / ProGuard) ------------------------------------------------------------------

JVM_FRAME_FIELDS = ("platform", "abs_path", "lineno", "function", "module", "filename", "in_app")


def proguard_uuids(event: dict) -> list[str]:
    images = (event.get("debug_meta") or {}).get("images") or []
    return sorted({str(i["uuid"]).lower() for i in images if i.get("type") == "proguard" and i.get("uuid")})


def release_package(event: dict) -> str | None:
    release = event.get("release")
    if isinstance(release, dict):
        release = release.get("version")
    if not release or "@" not in str(release):
        return None
    return str(release).split("@", 1)[0]


def _jvm_stacktraces(event: dict) -> list[dict]:
    """Exception stack traces plus other threads', in the order Sentry stores them (caller first)."""
    found = []
    for i, exc in enumerate((event.get("exception") or {}).get("values") or []):
        stacktrace = exc.get("raw_stacktrace") or exc.get("stacktrace") or {}
        if stacktrace.get("frames"):
            label = f"exception {i}: {exc.get('type') or ''} {exc.get('value') or ''}".strip()
            found.append({"label": label, "frames": stacktrace["frames"], "exception": exc})
    for thread in (event.get("threads") or {}).get("values") or []:
        if thread.get("crashed") and found:
            continue
        stacktrace = thread.get("raw_stacktrace") or thread.get("stacktrace") or {}
        if stacktrace.get("frames"):
            label = f"thread {thread.get('id')}" + (f" {thread.get('name')}" if thread.get("name") else "")
            found.append({"label": label, "frames": stacktrace["frames"], "exception": None})
    return found


def build_jvm_request(event: dict, sources: list[dict]) -> dict | None:
    """Symbolicator /symbolicate-jvm payload, or None when there is nothing to deobfuscate."""
    modules = [{"uuid": uuid, "type": "proguard"} for uuid in proguard_uuids(event)]
    traces = _jvm_stacktraces(event)
    if not modules or not traces:
        return None
    request: dict[str, Any] = {
        "platform": "java",
        "sources": sources,
        "exceptions": [
            {"type": t["exception"].get("type"), "module": t["exception"].get("module")}
            for t in traces if t["exception"] and t["exception"].get("type") and t["exception"].get("module")
        ],
        "stacktraces": [
            {"frames": [{"index": i, **{k: f[k] for k in JVM_FRAME_FIELDS if f.get(k) is not None}} for i, f in enumerate(t["frames"])]}
            for t in traces
        ],
        "modules": modules,
        "classes": [],
        "options": {"apply_source_context": True, "frame_order": "caller_first"},
    }
    package = release_package(event)
    if package:
        request["release_package"] = package
    return request


def _frame_text(frame: dict) -> str:
    module = frame.get("module") or ""
    function = frame.get("function") or "?"
    where = frame.get("filename") or frame.get("abs_path") or ""
    line = frame.get("lineno")
    name = f"{module}.{function}" if module else function
    return f"{name}({where}{':' + str(line) if line else ''})"


def summarize_jvm(event: dict, response: dict) -> dict:
    """Resolved JVM frames next to the obfuscated originals, callee first like the Sentry UI."""
    traces_in = _jvm_stacktraces(event)
    package = release_package(event)
    traces = []
    for i, trace in enumerate(response.get("stacktraces") or []):
        originals = traces_in[i]["frames"] if i < len(traces_in) else []
        frames = []
        for frame in trace.get("frames") or []:
            index = frame.get("index")
            original = originals[index] if isinstance(index, int) and 0 <= index < len(originals) else {}
            changed = _frame_text(frame) != _frame_text(original)
            module = frame.get("module") or ""
            frames.append(
                {
                    "status": "symbolicated" if changed else "unchanged",
                    "function": f"{module}.{frame.get('function')}" if module else frame.get("function"),
                    "filename": frame.get("filename") or frame.get("abs_path"),
                    "lineno": frame.get("lineno"),
                    "package": None,
                    "in_app": bool(package and module.startswith(package)),
                    "instruction_addr": None,
                    "original": _frame_text(original) if changed and original else None,
                    "context_line": (frame.get("context_line") or "").strip() or None,
                }
            )
        frames.reverse()  # most recent call first, as Sentry shows it
        label = traces_in[i]["label"] if i < len(traces_in) else f"stacktrace {i}"
        traces.append({"label": label, "frames": frames})

    missing = sorted({str(e.get("uuid")).lower() for e in response.get("errors") or [] if e.get("uuid")})
    known = set(proguard_uuids(event))
    modules = [
        {"debug_id": uuid, "code_file": "ProGuard / R8 mapping", "debug_status": "missing" if uuid in missing else "found", "arch": None}
        for uuid in sorted(known | set(missing))
    ]
    total = sum(len(t["frames"]) for t in traces)
    resolved = sum(1 for t in traces for f in t["frames"] if f["status"] == "symbolicated")
    return {
        "kind": "jvm",
        "status": response.get("status"),
        "stacktraces": traces,
        "modules": modules,
        "frames_total": total,
        "frames_symbolicated": resolved,
        "missing_debug_ids": missing,
        "exceptions": response.get("exceptions") or [],
    }

#!/usr/bin/env python3
"""
Code Execution Tool -- Programmatic Tool Calling (PTC)

Lets the LLM write a Python script that calls Hermes tools via RPC,
collapsing multi-step tool chains into a single inference turn.

Architecture (two transports):

  **Local backend (UDS):**
  1. Parent generates a `hermes_tools.py` stub module with UDS RPC functions
  2. Parent opens a Unix domain socket and starts an RPC listener thread
  3. Parent spawns a child process that runs the LLM's script
  4. Tool calls travel over the UDS back to the parent for dispatch

  **Remote backends (file-based RPC):**
  1. Parent generates `hermes_tools.py` with file-based RPC stubs
  2. Parent ships both files to the remote environment
  3. Script runs inside the terminal backend (Docker/SSH/Modal/Daytona/etc.)
  4. Tool calls are written as request files; a polling thread on the parent
     reads them via env.execute(), dispatches, and writes response files
  5. The script polls for response files and continues

In both cases, only the script's stdout is returned to the LLM; intermediate
tool results never enter the context window.

Platform: Linux / macOS only (Unix domain sockets for local). Disabled on Windows.
Remote execution additionally requires Python 3 in the terminal backend.
"""

import atexit
import base64
import functools
import json
import keyword
import logging
import os
import platform
import re
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

_IS_WINDOWS = platform.system() == "Windows"
from typing import Any, Dict, Iterable, List, Literal, Optional

from tools.thread_context import propagate_context_to_thread

# Availability gate.  On Windows we fall back to loopback TCP for the
# sandbox RPC transport (AF_UNIX is unreliable on Windows Python) — see
# ``_use_tcp_rpc`` in ``_execute_local`` below.  That makes execute_code
# available on every platform Hermes itself runs on.
logger = logging.getLogger(__name__)

SANDBOX_AVAILABLE = True

# Durable artifacts are deliberately bounded so a generated helper cannot use
# save_artifact as an unbounded file-read or disk-filling primitive.  These are
# execution-level defaults; a single execution has a separate aggregate cap.
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 50 * 1024 * 1024


@dataclass
class ArtifactBudget:
    """Thread-safe per-execution artifact byte budget."""

    max_bytes: int = MAX_ARTIFACT_BYTES
    max_total_bytes: int = MAX_TOTAL_ARTIFACT_BYTES
    used_bytes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self, size: int) -> bool:
        if size < 0 or size > self.max_bytes:
            return False
        with self._lock:
            if self.used_bytes + size > self.max_total_bytes:
                return False
            self.used_bytes += size
            return True


# The 7 tools allowed inside the sandbox. The intersection of this list
# and the session's enabled tools determines which stubs are generated.
SANDBOX_ALLOWED_TOOLS = frozenset([
    "web_search",
    "web_extract",
    "read_file",
    "write_file",
    "search_files",
    "patch",
    "terminal",
])


@dataclass(frozen=True)
class CodeExecutionContext:
    """Immutable parent-session context carried over script RPC calls."""

    task_id: Optional[str]
    session_id: Optional[str]
    enabled_toolsets: tuple[str, ...]
    disabled_toolsets: tuple[str, ...]
    # Parent-authorized roots for generated save_artifact calls.  These are
    # never taken from an untrusted RPC request; they are bound per execution.
    artifact_roots: tuple[str, ...] = ()
    artifact_budget: Optional["ArtifactBudget"] = None


# Keep policy names centralized so every RPC transport applies the same
# recursion, interaction, memory, and lifecycle boundary.
SCRIPT_RECURSION_TOOLS = frozenset({
    "execute_code", "delegate_task", "tool_search", "tool_describe", "tool_call",
})
SCRIPT_INTERACTIVE_TOOLS = frozenset({"clarify"})
SCRIPT_MEMORY_TOOLS = frozenset({"memory"})
SCRIPT_LIFECYCLE_TOOLS = frozenset({
    "process", "read_terminal", "close_terminal", "todo", "cronjob",
    "kanban_show", "kanban_list", "kanban_complete", "kanban_block",
    "kanban_heartbeat", "kanban_comment", "kanban_create", "kanban_link",
    "kanban_unblock",
})
SCRIPT_DENIED_TOOLS = frozenset().union(
    SCRIPT_RECURSION_TOOLS,
    SCRIPT_INTERACTIVE_TOOLS,
    SCRIPT_MEMORY_TOOLS,
    SCRIPT_LIFECYCLE_TOOLS,
)
# Internal request kind used by generated catalog helpers. It is not exposed
# as a registry tool and is handled by the parent adapter below.
SCRIPT_INTERNAL_TOOLS = frozenset({"__code_mode_catalog__"})

_DEFAULT_OPERATION_METADATA = {
    "read_only": False,
    "destructive": True,
    "idempotent": False,
}


def _operation_metadata_for(
    tool_name: str,
    operation_metadata: Optional[Dict[str, Dict[str, bool]]] = None,
) -> Dict[str, bool]:
    """Resolve operation metadata without weakening conservative defaults."""
    try:
        from tools.registry import registry
        resolved = dict(registry.get_operation_metadata(tool_name))
    except Exception:
        resolved = {}
    resolved = {**_DEFAULT_OPERATION_METADATA, **resolved}
    if operation_metadata and tool_name in operation_metadata:
        resolved.update(operation_metadata[tool_name] or {})
    return resolved


def _scriptable_tool_names(
    session_tools: Optional[Iterable[str]] = None,
    operation_metadata: Optional[Dict[str, Dict[str, bool]]] = None,
) -> set[str]:
    """Return deterministic session tools minus the script denylist."""
    names = {str(name) for name in (session_tools or ())}
    # Resolve metadata for every candidate here so callers can build one
    # registry-backed snapshot before generation/dispatch.
    for name in sorted(names):
        _operation_metadata_for(name, operation_metadata)
    return names - SCRIPT_DENIED_TOOLS


def _script_operation_decision(
    tool_name: str,
    metadata: Optional[Dict[str, bool]],
) -> Literal["allow", "approval_required", "deny"]:
    """Classify a scripted operation before it reaches the normal dispatcher."""
    if tool_name in SCRIPT_DENIED_TOOLS:
        return "deny"
    resolved = {**_DEFAULT_OPERATION_METADATA, **(metadata or {})}
    if bool(resolved.get("read_only")) and not bool(resolved.get("destructive")):
        return "allow"
    return "approval_required"


def _context_payload(context: Optional[CodeExecutionContext]) -> Dict[str, object]:
    context = context or CodeExecutionContext(None, None, (), ())
    budget = context.artifact_budget
    return {
        "task_id": context.task_id,
        "session_id": context.session_id,
        "enabled_toolsets": list(context.enabled_toolsets),
        "disabled_toolsets": list(context.disabled_toolsets),
        "artifact_roots": list(context.artifact_roots),
        "artifact_max_bytes": int(budget.max_bytes if budget else MAX_ARTIFACT_BYTES),
    }


def _context_from_rpc_request(
    request: Dict[str, Any], fallback: CodeExecutionContext,
) -> CodeExecutionContext:
    """Keep the parent RPC context authoritative over untrusted requests."""
    return fallback


def _call_handle_function_call(
    handler, tool_name: str, arguments: dict, dispatch_kwargs: Dict[str, Any],
):
    """Call current or legacy dispatchers without dropping real context."""
    try:
        return handler(tool_name, dict(arguments), **dispatch_kwargs)
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return handler(
            tool_name,
            dict(arguments),
            task_id=dispatch_kwargs.get("task_id"),
        )


def _dispatch_catalog_action(
    arguments: Dict[str, object],
    context: CodeExecutionContext,
) -> dict[str, object]:
    """Route generated catalog helpers through the existing bridge dispatcher."""
    action = arguments.get("action")
    bridge_name = {
        "tool_search": "tool_search",
        "tool_describe": "tool_describe",
        "tool_call": "tool_call",
    }.get(action)
    if action == "save_artifact":
        save_args = arguments.get("arguments", {})
        if not isinstance(save_args, dict):
            return {"error": "save_artifact arguments must be an object."}
        source: str | bytes | None = None
        path = save_args.get("path")
        path_or_bytes = save_args.get("path_or_bytes")
        encoded = save_args.get("bytes_b64")
        if isinstance(encoded, str):
            try:
                source = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                return {"error": "save_artifact bytes_b64 is invalid."}
        elif isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
            source = bytes(path_or_bytes)
        elif isinstance(path_or_bytes, str) and path_or_bytes.strip():
            source = path_or_bytes
        elif isinstance(path, str) and path.strip():
            source = path
        else:
            return {"error": "save_artifact requires path_or_bytes."}
        name = save_args.get("name")
        mime_type = save_args.get("mime_type")
        return _persist_file_artifact(
            source,
            name=name if isinstance(name, str) else None,
            mime_type=mime_type if isinstance(mime_type, str) else None,
            allowed_roots=context.artifact_roots,
            budget=context.artifact_budget,
        )
    if bridge_name is None:
        return {"error": f"Unknown code-mode catalog action: {action}"}
    bridge_args = arguments.get("arguments", {})
    if not isinstance(bridge_args, dict):
        return {"error": "Code-mode catalog arguments must be an object."}

    from model_tools import handle_function_call
    dispatch_kwargs: Dict[str, Any] = {"task_id": context.task_id}
    if context.session_id is not None:
        dispatch_kwargs["session_id"] = context.session_id
    if context.enabled_toolsets:
        dispatch_kwargs["enabled_toolsets"] = list(context.enabled_toolsets)
    if context.disabled_toolsets:
        dispatch_kwargs["disabled_toolsets"] = list(context.disabled_toolsets)
    raw_result = _call_handle_function_call(
        handle_function_call, bridge_name, bridge_args, dispatch_kwargs,
    )
    if isinstance(raw_result, dict):
        return raw_result
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except (TypeError, json.JSONDecodeError):
            return {"result": raw_result}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {"result": raw_result}


def _dispatch_script_call(
    tool_name: str,
    arguments: Dict[str, object],
    context: Optional[CodeExecutionContext] = None,
) -> dict[str, object]:
    """Dispatch one in-scope scripted call through the standard tool path."""
    context = context or CodeExecutionContext(None, None, (), ())
    if not isinstance(arguments, dict):
        return {"error": "Script tool arguments must be an object."}
    if tool_name in SCRIPT_INTERNAL_TOOLS:
        try:
            return _dispatch_catalog_action(arguments, context)
        except Exception as exc:
            logger.error("Code-mode catalog action failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    if tool_name in SCRIPT_DENIED_TOOLS:
        return {"error": f"Tool '{tool_name}' is not available to execute_code scripts."}

    try:
        from tools.registry import registry
        if registry.get_entry(tool_name) is None:
            return {"error": f"Unknown scripted tool: {tool_name}"}

        operation_metadata = _operation_metadata_for(tool_name)
        decision = _script_operation_decision(tool_name, operation_metadata)
        if decision == "deny":
            return {"error": f"Tool '{tool_name}' is not available to execute_code scripts."}

        # Reuse the active model-facing scope when the parent supplied one;
        # the empty tuple preserves the legacy unrestricted RPC behavior.
        if context.enabled_toolsets or context.disabled_toolsets:
            from model_tools import get_tool_definitions
            from tools.tool_search import scoped_tool_names
            definitions = get_tool_definitions(
                enabled_toolsets=list(context.enabled_toolsets) or None,
                disabled_toolsets=list(context.disabled_toolsets) or None,
                quiet_mode=True,
                skip_tool_search_assembly=True,
            )
            if tool_name not in scoped_tool_names(definitions):
                return {"error": f"Tool '{tool_name}' is not available in this session."}

        if decision == "approval_required":
            try:
                from hermes_cli.plugins import has_middleware
                has_approval_middleware = has_middleware("tool_execution")
            except Exception:
                has_approval_middleware = False
            if not has_approval_middleware:
                return {
                    "status": "approval_required",
                    "tool_name": tool_name,
                    "requester": context.session_id or "",
                    "task_id": context.task_id,
                    "session_id": context.session_id,
                    "operation_metadata": operation_metadata,
                }

        from model_tools import handle_function_call
        dispatch_kwargs: Dict[str, Any] = {"task_id": context.task_id}
        if context.session_id is not None:
            dispatch_kwargs["session_id"] = context.session_id
        if context.enabled_toolsets:
            dispatch_kwargs["enabled_toolsets"] = list(context.enabled_toolsets)
        if context.disabled_toolsets:
            dispatch_kwargs["disabled_toolsets"] = list(context.disabled_toolsets)
        dispatch_kwargs["operation_metadata"] = operation_metadata
        raw_result = _call_handle_function_call(
            handle_function_call, tool_name, arguments, dispatch_kwargs,
        )
    except Exception as exc:
        logger.error("Scripted tool call failed: %s", exc, exc_info=True)
        return {"error": str(exc)}

    if isinstance(raw_result, dict):
        return raw_result
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except (TypeError, json.JSONDecodeError):
            return {"result": raw_result}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {"result": raw_result}

# Resource limit defaults (overridable via config.yaml → code_execution.*)
DEFAULT_TIMEOUT = 300        # 5 minutes
DEFAULT_MAX_TOOL_CALLS = 50
MAX_STDOUT_BYTES = 50_000    # 50 KB
MAX_STDERR_BYTES = 10_000    # 10 KB
DEFAULT_ARTIFACT_DIR = "/tmp/hermes-results"

_IMAGE_SUFFIXES = frozenset({
    ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp",
})


def _image_url_candidate(value: Any) -> Optional[str]:
    """Return a usable image URL/path from one artifact value."""
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() != "image_url":
            return None
        image_url = value.get("image_url")
        value = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = os.fspath(value).strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered.startswith("data:image/") and "," in raw:
        return raw
    parsed = urlsplit(raw)
    if parsed.scheme in {"http", "https"}:
        return raw if os.path.splitext(parsed.path)[1].lower() in _IMAGE_SUFFIXES else None
    if parsed.scheme == "file":
        local_path = unquote(parsed.path)
    elif parsed.scheme:
        return None
    else:
        local_path = os.path.expanduser(raw)
    if os.path.isfile(local_path) and os.path.splitext(local_path)[1].lower() in _IMAGE_SUFFIXES:
        return os.path.abspath(local_path)
    return None


def normalize_image_artifact(value: Any) -> Optional[dict]:
    """Normalize an image URL, data URL, local path, or image part."""
    candidate = _image_url_candidate(value)
    if candidate is None:
        return None
    if not candidate.lower().startswith(("http://", "https://", "data:", "file://")):
        candidate = Path(candidate).resolve().as_uri()
    return {"type": "image_url", "image_url": {"url": candidate}}


# Public alias with the more explicit name used by callers that handle
# structured tool results.
normalize_structured_image_artifact = normalize_image_artifact


def is_structured_image_artifact(value: Any) -> bool:
    """Return whether *value* identifies an image artifact."""
    if normalize_image_artifact(value) is not None:
        return True
    if isinstance(value, dict) and value.get("_multimodal") is True:
        return any(
            normalize_image_artifact(part) is not None
            for part in value.get("content", [])
            if isinstance(part, dict)
        )
    if isinstance(value, (list, tuple)):
        return any(is_structured_image_artifact(item) for item in value)
    return False


def _image_parts_from_output(output: str) -> list[dict]:
    """Find image parts in direct or JSON-encoded execute_code output."""
    values = [output]
    try:
        parsed = json.loads(output.strip())
    except (TypeError, json.JSONDecodeError, AttributeError):
        parsed = None
    if parsed is not None and not isinstance(parsed, str):
        values.insert(0, parsed)

    parts = []
    for value in values:
        if isinstance(value, (list, tuple)):
            candidates = value
        elif isinstance(value, dict) and value.get("_multimodal") is True:
            candidates = value.get("content", [])
        else:
            candidates = [value]
        for candidate in candidates:
            part = normalize_image_artifact(candidate)
            if part and part not in parts:
                parts.append(part)
    return parts


def _artifact_storage_dir() -> str:
    """Return the configured durable tool-result directory."""
    try:
        from tools.tool_result_storage import STORAGE_DIR
        default_dir = STORAGE_DIR
    except Exception:
        default_dir = DEFAULT_ARTIFACT_DIR
    try:
        configured = _load_config().get("artifact_dir", default_dir)
    except Exception:
        configured = default_dir
    directory = configured if isinstance(configured, str) and configured.strip() else default_dir
    directory = os.path.abspath(os.path.expanduser(directory))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory


def _persist_execute_artifact(
    content: str,
    suffix: str = ".txt",
    *,
    budget: Optional[ArtifactBudget] = None,
) -> str:
    """Persist already-redacted text under the durable artifact directory."""
    data = str(content or "").encode("utf-8")
    if len(data) > MAX_ARTIFACT_BYTES:
        data = data[:MAX_ARTIFACT_BYTES].decode("utf-8", errors="ignore").encode("utf-8")
    budget = budget or ArtifactBudget()
    if not budget.reserve(len(data)):
        raise ValueError("Artifact byte budget exceeded.")
    fd, path = tempfile.mkstemp(
        prefix="execute_code_", suffix=suffix, dir=_artifact_storage_dir(), text=False,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _path_is_under_roots(path: str, roots: Iterable[str]) -> bool:
    """Return whether a resolved path is inside one authorized root."""
    try:
        candidate = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
        for root in roots:
            if not isinstance(root, str) or not root.strip():
                continue
            resolved_root = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
            if os.path.commonpath((candidate, resolved_root)) == resolved_root:
                return True
    except (OSError, ValueError):
        return False
    return False


def _safe_artifact_name(name: Optional[str], fallback: str = "artifact") -> str:
    """Return a bounded, basename-only, redacted artifact metadata name."""
    raw = os.path.basename(str(name or fallback)).strip() or fallback
    cleaned = os.path.basename(_clean_execute_text(raw)).strip() or fallback
    return cleaned[:128]


def _safe_mime_type(mime_type: Optional[str]) -> Optional[str]:
    if not isinstance(mime_type, str):
        return None
    cleaned = _clean_execute_text(mime_type).strip()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*", cleaned):
        return cleaned
    return None


def _persist_file_artifact(
    source: str | bytes,
    name: Optional[str] = None,
    mime_type: Optional[str] = None,
    *,
    allowed_roots: Iterable[str] = (),
    budget: Optional[ArtifactBudget] = None,
) -> dict:
    """Persist bounded bytes or a file beneath an authorized execution root."""
    budget = budget or ArtifactBudget()
    source_path = None
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        fallback_name = "artifact"
    elif isinstance(source, str) and source.strip():
        source_path = os.path.realpath(os.path.abspath(os.path.expanduser(source)))
        if not _path_is_under_roots(source_path, allowed_roots):
            return {"error": "Artifact path must be inside the execution artifact roots."}
        if not os.path.isfile(source_path):
            return {"error": f"Artifact does not exist: {source}"}
        try:
            size = os.path.getsize(source_path)
            if size > MAX_ARTIFACT_BYTES:
                return {"error": f"Artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit."}
            with open(source_path, "rb") as handle:
                data = handle.read(MAX_ARTIFACT_BYTES + 1)
            if len(data) > MAX_ARTIFACT_BYTES:
                return {"error": f"Artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit."}
        except OSError as exc:
            return {"error": f"Artifact could not be read: {exc}"}
        fallback_name = os.path.basename(source_path)
    else:
        return {"error": "Artifact requires path_or_bytes."}

    if len(data) > MAX_ARTIFACT_BYTES:
        return {"error": f"Artifact exceeds the {MAX_ARTIFACT_BYTES}-byte limit."}

    safe_mime = _safe_mime_type(mime_type)
    filename = _safe_artifact_name(name, fallback_name)
    suffix = os.path.splitext(filename)[1].lower()
    if safe_mime and safe_mime.startswith("image/") and suffix not in _IMAGE_SUFFIXES:
        import mimetypes
        suffix = mimetypes.guess_extension(safe_mime) or ".bin"
    is_image = suffix in _IMAGE_SUFFIXES or bool(safe_mime and safe_mime.startswith("image/"))

    if is_image:
        if not budget.reserve(len(data)):
            return {"error": "Artifact byte budget exceeded."}
        fd, destination = tempfile.mkstemp(
            prefix="execute_code_", suffix=suffix or ".bin", dir=_artifact_storage_dir(),
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.unlink(destination)
            except OSError:
                pass
            raise
        image_part = normalize_image_artifact(destination)
    else:
        content = _clean_execute_text(data.decode("utf-8", errors="replace"))
        destination = _persist_execute_artifact(
            content,
            suffix=suffix or ".txt",
            budget=budget,
        )
        image_part = None

    response = {
        "status": "ok",
        "artifact_path": destination,
        "name": filename,
    }
    if safe_mime:
        response["mime_type"] = safe_mime
    if image_part:
        response.update({
            "_multimodal": True,
            "content": [image_part],
        })
    return response


def _collect_local_artifacts(
    artifact_dir: Optional[str],
    limit: int,
    *,
    budget: Optional[ArtifactBudget] = None,
) -> tuple[list[dict], Optional[str]]:
    """Copy generated images or spill generated text files before cleanup."""
    if not artifact_dir or not os.path.isdir(artifact_dir):
        return [], None
    budget = budget or ArtifactBudget()
    image_parts = []
    text_path = None
    for name in sorted(os.listdir(artifact_dir)):
        source = os.path.join(artifact_dir, name)
        if not os.path.isfile(source):
            continue
        image_part = normalize_image_artifact(source)
        if image_part:
            copied = _persist_file_artifact(
                source,
                name=name,
                allowed_roots=(artifact_dir,),
                budget=budget,
            )
            part = copied.get("content", [None])[0]
            if part and part not in image_parts:
                image_parts.append(part)
            continue
        try:
            size = os.path.getsize(source)
            if size <= limit:
                continue
            if size > MAX_ARTIFACT_BYTES:
                size = MAX_ARTIFACT_BYTES
            with open(source, "rb") as handle:
                content = _clean_execute_text(handle.read(size + 1).decode("utf-8", errors="replace"))
        except OSError:
            continue
        if len(content) > limit and text_path is None:
            try:
                text_path = _persist_execute_artifact(content, budget=budget)
            except ValueError:
                pass
    return image_parts, text_path


def _clean_execute_text(text: Any) -> str:
    from tools.ansi_strip import strip_ansi
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(strip_ansi(str(text or "")), code_file=True)


def _truncate_execute_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted out of {len(text):,} total] ...\n\n"
        + text[-tail:]
    )


def _prepare_execute_output(
    text: Any,
    limit: int,
    *,
    budget: Optional[ArtifactBudget] = None,
) -> tuple[str, Optional[str], list[dict]]:
    """Clean output, preserve images, and spill oversized text before truncating."""
    cleaned = _clean_execute_text(text)
    image_parts = _image_parts_from_output(cleaned)
    artifact_path = None
    if len(cleaned) > limit:
        artifact_path = _persist_execute_artifact(cleaned, budget=budget)
        cleaned = _truncate_execute_output(cleaned, limit)
    return cleaned, artifact_path, image_parts


def _attach_execute_artifacts(
    result: Dict[str, Any],
    image_parts: Iterable[dict] = (),
    artifact_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Add the common structured-artifact fields without changing text results."""
    parts = list(image_parts)
    if parts:
        result["_multimodal"] = True
        result["content"] = parts
        result["text_summary"] = result.get("output", "")
    if artifact_path:
        result["truncated"] = True
        result["artifact_path"] = artifact_path
    return result


# Environment variable scrubbing rules (shared between the local + remote
# backends).  Secret-substring block is applied first; anything left must
# match a safe prefix, the operational HERMES_ allowlist, or (on Windows) an
# OS-essential name.
#
# NB: the broad "HERMES_" prefix was deliberately removed (#27303) — it leaked
# HERMES_*-named config that lacks a secret substring (e.g. HERMES_BASE_URL,
# HERMES_KANBAN_DB, HERMES_*_WEBHOOK).  The child only needs the few
# location/profile vars in _HERMES_CHILD_ALLOWED below; HERMES_RPC_SOCKET /
# HERMES_RPC_DIR / TZ / HOME are injected explicitly after scrubbing.
_SAFE_ENV_PREFIXES = ("PATH", "HOME", "USER", "LANG", "LC_", "TERM",
                      "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME",
                      "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA")
_SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                      "PASSWD", "AUTH", "DSN", "WEBHOOK",
                      # Abbreviations that appear in real-world credential
                      # variable names but were previously undetected:
                      # CREDS (CREDENTIALS abbreviated), BEARER
                      # (Authorization: Bearer tokens), APIKEY (written
                      # without an underscore). "PASS" is intentionally NOT
                      # added — it false-positives on legitimate non-secret
                      # vars (BYPASS_CACHE, COMPASS_DIR, PASSENGER_HOST) while
                      # PASSWORD/PASSWD already cover the credential cases.
                      "CREDS", "BEARER", "APIKEY")

# Operational HERMES_* vars the child legitimately needs by exact name — these
# are non-secret runtime-location flags (the same set hermes_cli treats as the
# runtime location) that repo-root modules a sandbox script imports may read at
# import time.  None match _SECRET_SUBSTRINGS.
_HERMES_CHILD_ALLOWED = frozenset({
    "HERMES_HOME",
    "HERMES_PROFILE",
    "HERMES_CONFIG",
    "HERMES_ENV",
})

# Windows-only: a handful of variables are required by the OS/CRT itself.
# Without them, even stdlib calls like ``socket.socket()`` fail with
# WinError 10106 (Winsock can't locate mswsock.dll) and ``subprocess``
# can't resolve cmd.exe.  These are well-known OS paths, not secrets, so
# we allow them through by exact name.  The _SECRET_SUBSTRINGS block
# still runs as a safety net (none of these names match those substrings).
_WINDOWS_ESSENTIAL_ENV_VARS = frozenset({
    "SYSTEMROOT",       # %SYSTEMROOT%\System32 — Winsock needs this
    "SYSTEMDRIVE",      # C: (or wherever Windows lives)
    "WINDIR",           # usually same as SYSTEMROOT
    "COMSPEC",          # cmd.exe path — subprocess shell=True needs it
    "PATHEXT",          # .COM;.EXE;.BAT;... — shell lookup
    "OS",               # "Windows_NT" — some tools gate on this
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "PUBLIC",           # C:\Users\Public
    "ALLUSERSPROFILE",  # C:\ProgramData — some stdlib paths use it
    "PROGRAMDATA",      # C:\ProgramData
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "APPDATA",          # %USERPROFILE%\AppData\Roaming — Python uses it
    "LOCALAPPDATA",     # %USERPROFILE%\AppData\Local
    "USERPROFILE",      # C:\Users\<name> — Python's expanduser uses it
    "USERDOMAIN",
    "USERNAME",
    "HOMEDRIVE",        # C:
    "HOMEPATH",         # \Users\<name>
    "COMPUTERNAME",
})


def _scrub_child_env(source_env, is_passthrough=None, is_windows=None):
    """Produce the scrubbed child-process env for execute_code.

    Rules (order matters):
      1. Passthrough vars (skill- or config-declared) always pass.
      2. Secret-substring names (KEY/TOKEN/DSN/WEBHOOK/etc.) are blocked.
      3. Names matching a safe prefix pass.
      4. Operational HERMES_* vars (_HERMES_CHILD_ALLOWED) pass by exact name.
      5. On Windows, a small OS-essential allowlist passes by exact name
         — without these the child can't even create a socket or spawn a
         subprocess.

    Extracted into a helper so tests can exercise the logic without
    spawning a subprocess.
    """
    if is_passthrough is None:
        try:
            from tools.env_passthrough import is_env_passthrough as _ep
        except Exception:
            _ep = lambda _: False  # noqa: E731
        is_passthrough = _ep
    if is_windows is None:
        is_windows = _IS_WINDOWS

    scrubbed = {}
    # Non-secret HERMES_* vars dropped by the tightened allowlist (#27303). The
    # broad "HERMES_" prefix used to pass these through; now only the
    # operational set does. The drop is intentional (those vars can carry
    # config like HERMES_KANBAN_DB / HERMES_BASE_URL), but a sandbox script
    # that imports a repo module reading one at import time would otherwise see
    # it silently unset. Surface the drop once so the behavior change is
    # diagnosable and points at the env_passthrough opt-in escape hatch.
    _dropped_hermes = []
    for k, v in source_env.items():
        if is_passthrough(k):
            scrubbed[k] = v
            continue
        if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
            continue
        if any(k.startswith(p) for p in _SAFE_ENV_PREFIXES):
            scrubbed[k] = v
            continue
        if k in _HERMES_CHILD_ALLOWED:
            scrubbed[k] = v
            continue
        if is_windows and k.upper() in _WINDOWS_ESSENTIAL_ENV_VARS:
            scrubbed[k] = v
            continue
        if k.startswith("HERMES_"):
            # Non-secret (secrets were already dropped above) and not in any
            # allowlist — a deliberately-dropped HERMES_* var.
            _dropped_hermes.append(k)
    if _dropped_hermes:
        logger.debug(
            "execute_code: dropped %d non-allowlisted HERMES_* var(s) from the "
            "sandbox child env (%s). This is intentional hardening (#27303); if "
            "a sandbox script legitimately needs one, declare it via "
            "env_passthrough in the skill/config so it passes by explicit opt-in.",
            len(_dropped_hermes),
            ", ".join(sorted(_dropped_hermes)),
        )
    return scrubbed


def check_sandbox_requirements() -> bool:
    """Code execution sandbox requires a POSIX OS for Unix domain sockets
    and a working terminal backend (execute_code dispatches through it)."""
    if not SANDBOX_AVAILABLE:
        return False
    from tools.terminal_tool import check_terminal_requirements
    if not check_terminal_requirements():
        return False
    return True


# ---------------------------------------------------------------------------
# hermes_tools.py code generator
# ---------------------------------------------------------------------------

# Per-tool stub templates: (function_name, signature, docstring, args_dict_expr)
# The args_dict_expr builds the JSON payload sent over the RPC socket.
_TOOL_STUBS = {
    "web_search": (
        "web_search",
        "query: str, limit: int = 5",
        '"""Search the web. Returns dict with data.web list of {url, title, description}."""',
        '{"query": query, "limit": limit}',
    ),
    "web_extract": (
        "web_extract",
        "urls: list, char_limit: int = None",
        '"""Extract content from URLs (no LLM summarization). Returns dict with results list of {url, title, content, error}. Pages over char_limit (default 15000) are head+tail truncated with the full text stored on disk; the content footer gives the path. content is markdown."""',
        '{"urls": urls, "char_limit": char_limit}',
    ),
    "read_file": (
        "read_file",
        "path: str, offset: int = 1, limit: int = 500",
        '"""Read a file (1-indexed lines). Returns dict with "content" and "total_lines"."""',
        '{"path": path, "offset": offset, "limit": limit}',
    ),
    "write_file": (
        "write_file",
        "path: str, content: str, cross_profile: bool = False",
        '"""Write content to a file (always overwrites). Returns dict with status. cross_profile=True opts out of the cross-Hermes-profile soft guard."""',
        '{"path": path, "content": content, "cross_profile": cross_profile}',
    ),
    "search_files": (
        "search_files",
        'pattern: str, target: str = "content", path: str = ".", file_glob: str = None, limit: int = 50, offset: int = 0, output_mode: str = "content", context: int = 0',
        '"""Search file contents (target="content") or find files by name (target="files"). Returns dict with "matches"."""',
        '{"pattern": pattern, "target": target, "path": path, "file_glob": file_glob, "limit": limit, "offset": offset, "output_mode": output_mode, "context": context}',
    ),
    "patch": (
        "patch",
        'path: str = None, old_string: str = None, new_string: str = None, replace_all: bool = False, mode: str = "replace", patch: str = None, cross_profile: bool = False',
        '"""Targeted find-and-replace (mode="replace") or V4A multi-file patches (mode="patch"). Returns dict with status. cross_profile=True opts out of the cross-Hermes-profile soft guard."""',
        '{"path": path, "old_string": old_string, "new_string": new_string, "replace_all": replace_all, "mode": mode, "patch": patch, "cross_profile": cross_profile}',
    ),
    "terminal": (
        "terminal",
        "command: str, timeout: int = None, workdir: str = None",
        '"""Run a shell command (foreground only). Returns dict with "output" and "exit_code"."""',
        '{"command": command, "timeout": timeout, "workdir": workdir}',
    ),
}


_MAX_GENERATED_SOURCE_CHARS = 100_000
_MAX_TOOL_DESCRIPTION_CHARS = 600
_MAX_SCHEMA_DOC_CHARS = 2_400


_CODE_MODE_CATALOG_HELPERS = '''

def search_tools(query: str, limit: int = 5):
    """Search the active deferred-tool catalog by capability."""
    return _call('__code_mode_catalog__', {
        'action': 'tool_search',
        'arguments': {'query': query, 'limit': limit},
    })


def describe_tool(name: str):
    """Return the registered schema for one catalog tool."""
    return _call('__code_mode_catalog__', {
        'action': 'tool_describe',
        'arguments': {'name': name},
    })


def call_tool(name: str, arguments: dict = None):
    """Invoke a catalog tool through the normal parent dispatcher."""
    return _call('__code_mode_catalog__', {
        'action': 'tool_call',
        'arguments': {'name': name, 'arguments': arguments or {}},
    })


def save_artifact(path_or_bytes, name: str = None, mime_type: str = None):
    """Persist bounded bytes or a file under the execution artifact roots."""
    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        data = bytes(path_or_bytes)
        source_name = None
    elif isinstance(path_or_bytes, str) and path_or_bytes.strip():
        candidate = os.path.realpath(os.path.expanduser(path_or_bytes))
        roots = [
            os.path.realpath(str(root))
            for root in (_HERMES_RPC_CONTEXT.get('artifact_roots') or [])
            if isinstance(root, str) and root.strip()
        ]
        try:
            allowed = any(os.path.commonpath((candidate, root)) == root for root in roots)
        except (OSError, ValueError):
            allowed = False
        if not allowed:
            return {'error': 'save_artifact path must be inside the execution artifact roots.'}
        try:
            size = os.path.getsize(candidate)
            max_bytes = int(_HERMES_RPC_CONTEXT.get('artifact_max_bytes') or 0)
            if max_bytes > 0 and size > max_bytes:
                return {'error': f'save_artifact file exceeds the {max_bytes}-byte limit.'}
            with open(candidate, 'rb') as handle:
                data = handle.read(max_bytes + 1 if max_bytes > 0 else -1)
            if max_bytes > 0 and len(data) > max_bytes:
                return {'error': f'save_artifact file exceeds the {max_bytes}-byte limit.'}
        except OSError as exc:
            return {'error': f'save_artifact could not read the file: {exc}'}
        source_name = os.path.basename(candidate)
    else:
        return {'error': 'save_artifact requires path_or_bytes.'}

    max_bytes = int(_HERMES_RPC_CONTEXT.get('artifact_max_bytes') or 0)
    if max_bytes > 0 and len(data) > max_bytes:
        return {'error': f'save_artifact bytes exceed the {max_bytes}-byte limit.'}
    return _call('__code_mode_catalog__', {
        'action': 'save_artifact',
        'arguments': {
            'bytes_b64': base64.b64encode(data).decode('ascii'),
            'name': name or source_name,
            'mime_type': mime_type,
        },
    })
'''


def _definition_input(value: Any) -> bool:
    """Return whether *value* is a registry/OpenAI definition container."""
    if isinstance(value, dict):
        return True
    return isinstance(value, (list, tuple)) and any(
        isinstance(item, dict) for item in value
    )


def _normalize_tool_definitions(value: Any) -> List[dict]:
    """Normalize registry mappings and OpenAI definitions to one shape."""
    if isinstance(value, dict):
        if isinstance(value.get("function"), dict) or "name" in value:
            raw_items = [value]
        else:
            raw_items = []
            for name, definition in value.items():
                if isinstance(definition, dict) and isinstance(
                    definition.get("function"), dict
                ):
                    item = dict(definition)
                    item["function"] = dict(definition["function"])
                    item["function"].setdefault("name", str(name))
                elif isinstance(definition, dict) and (
                    "parameters" in definition or "description" in definition
                ):
                    item = dict(definition)
                    item.setdefault("name", str(name))
                else:
                    item = {"name": str(name), "parameters": definition}
                raw_items.append(item)
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        return []

    normalized: List[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if isinstance(function, dict):
            function = dict(function)
        else:
            function = dict(item)
            function.pop("type", None)
        name = function.get("name")
        if name is None and item.get("name") is not None:
            name = item["name"]
        if name is None:
            continue
        function["name"] = str(name)
        if "parameters" not in function:
            # A mapping value may itself be a JSON-Schema object.
            if isinstance(item.get("type"), str) and item.get("type") != "function":
                function["parameters"] = item
            else:
                function["parameters"] = {"type": "object", "properties": {}}
        normalized.append({"type": "function", "function": function})
    return sorted(normalized, key=lambda item: str(
        (item.get("function") or {}).get("name", "")
    ))


def _sanitize_registry_definitions(definitions: List[dict]) -> List[dict]:
    """Run the shared schema sanitizer before code-mode conversion."""
    try:
        from tools.schema_sanitizer import sanitize_tool_schemas
        return sanitize_tool_schemas(definitions)
    except Exception as exc:
        logger.debug("Code-mode schema sanitization failed: %s", exc)
        return definitions


def _safe_python_identifier(name: str, used: set[str]) -> tuple[str, bool]:
    """Return a deterministic Python identifier and whether the original is valid."""
    original_valid = name.isidentifier() and not keyword.iskeyword(name)
    candidate = re.sub(r"\W", "_", name, flags=re.ASCII) or "tool"
    if candidate[0].isdigit():
        candidate = "tool_" + candidate
    if keyword.iskeyword(candidate):
        candidate += "_"
    base = candidate
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate, original_valid


def _safe_default(value: Any) -> str:
    """Return a source-safe literal for JSON-compatible schema defaults."""
    if value is None or isinstance(value, (bool, int, str)):
        return repr(value)
    if isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")):
        return repr(value)
    if isinstance(value, list):
        return repr([_json_default_value(item) for item in value])
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return repr({key: _json_default_value(item) for key, item in value.items()})
    return "None"


def _json_default_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_json_default_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_default_value(item) for key, item in value.items()}
    return None


def _schema_annotation(schema: Any) -> Optional[str]:
    if not isinstance(schema, dict) or any(
        key in schema for key in ("$ref", "anyOf", "oneOf", "allOf")
    ):
        return None
    return {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list",
        "object": "dict",
    }.get(schema.get("type"))


def _schema_is_unsupported(schema: Any) -> bool:
    """Reject schemas that cannot be represented by a safe Python signature."""
    if not isinstance(schema, dict):
        return True
    if any(key in schema for key in ("$ref", "anyOf", "oneOf", "allOf")):
        return True
    schema_type = schema.get("type")
    if schema_type == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return True
        if "additionalProperties" in schema and schema["additionalProperties"] is not False:
            return True
        if not properties and schema.get("additionalProperties") is not False:
            return True
        return any(_schema_is_unsupported(item) for item in properties.values())
    if schema_type == "array":
        return "items" in schema and _schema_is_unsupported(schema.get("items"))
    return _schema_annotation(schema) is None


def _bounded_schema_text(schema: Any) -> str:
    try:
        text = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(schema)
    return text[:_MAX_SCHEMA_DOC_CHARS]


def _render_registry_wrapper(
    definition: dict,
    used_names: set[str],
) -> str:
    function = definition.get("function") or {}
    registered_name = str(function.get("name", "tool"))
    function_name, valid_name = _safe_python_identifier(registered_name, used_names)
    parameters = function.get("parameters") or {"type": "object", "properties": {}}
    description = str(function.get("description") or "Registered Hermes tool.")
    description = description[:_MAX_TOOL_DESCRIPTION_CHARS]
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    raw_required = parameters.get("required", []) if isinstance(parameters, dict) else []
    required = [
        name for name in (raw_required if isinstance(raw_required, list) else [])
        if isinstance(name, str) and isinstance(properties, dict) and name in properties
    ]
    ordered_names = required + [
        name for name in (properties or {}) if name not in required
    ]
    unsupported = (
        not valid_name
        or not isinstance(properties, dict)
        or _schema_is_unsupported(parameters)
    )
    if unsupported:
        doc = f"{description}\nSanitized schema: {_bounded_schema_text(parameters)}"
        return (
            f"def {function_name}(**kwargs):\n"
            f"    {repr(doc)}\n"
            f"    return _call({registered_name!r}, kwargs)\n\n"
        )

    used_params: set[str] = set()
    signature_parts: List[str] = []
    payload_parts: List[str] = []
    for raw_name in ordered_names:
        schema = properties[raw_name]
        param_name, _ = _safe_python_identifier(str(raw_name), used_params)
        annotation = _schema_annotation(schema)
        if annotation is None:
            # A malformed property should not produce a partially typed API.
            doc = f"{description}\nSanitized schema: {_bounded_schema_text(parameters)}"
            return (
                f"def {function_name}(**kwargs):\n"
                f"    {repr(doc)}\n"
                f"    return _call({registered_name!r}, kwargs)\n\n"
            )
        default = ""
        if raw_name not in required:
            default = " = " + _safe_default(schema.get("default"))
        signature_parts.append(f"{param_name}: {annotation}{default}")
        payload_parts.append(f"{str(raw_name)!r}: {param_name}")
    signature = ", ".join(signature_parts)
    payload = "{" + ", ".join(payload_parts) + "}"
    return (
        f"def {function_name}({signature}):\n"
        f"    {repr(description)}\n"
        f"    return _call({registered_name!r}, {payload})\n\n"
    )


def generate_hermes_tools_module(
    enabled_tools: Any = None,
    transport: str = "uds",
    *,
    context: Optional[CodeExecutionContext] = None,
    active_tool_definitions: Any = None,
) -> str:
    """Build ``hermes_tools.py`` for legacy names or active registry definitions."""
    if isinstance(transport, CodeExecutionContext):
        context, transport = transport, "uds"

    definitions_input = active_tool_definitions is not None or _definition_input(enabled_tools)
    if active_tool_definitions is not None:
        definitions = _normalize_tool_definitions(active_tool_definitions)
    elif definitions_input:
        definitions = _normalize_tool_definitions(enabled_tools)
    else:
        tools_to_generate = sorted(_scriptable_tool_names(
            SANDBOX_ALLOWED_TOOLS & set(enabled_tools or ()),
        ))
        stub_functions = []
        for tool_name in tools_to_generate:
            if tool_name not in _TOOL_STUBS:
                continue
            func_name, sig, doc, args_expr = _TOOL_STUBS[tool_name]
            stub_functions.append(
                f"def {func_name}({sig}):\n"
                f"    {doc}\n"
                f"    return _call({func_name!r}, {args_expr})\n\n"
            )
        definitions = []

    if definitions_input:
        definitions = _sanitize_registry_definitions(definitions)
        definitions = [
            item for item in definitions
            if str((item.get("function") or {}).get("name", ""))
            not in SCRIPT_DENIED_TOOLS | SCRIPT_INTERNAL_TOOLS
        ]
        used_names: set[str] = {
            "search_tools", "describe_tool", "call_tool", "save_artifact",
        }
        stub_functions = [
            _render_registry_wrapper(item, used_names) for item in definitions
        ]

    header = _FILE_TRANSPORT_HEADER if transport == "file" else _UDS_TRANSPORT_HEADER
    context_source = "\n_HERMES_RPC_CONTEXT = " + repr(_context_payload(context)) + "\n"
    catalog_helpers = _CODE_MODE_CATALOG_HELPERS if definitions_input else ""
    prefix = header + context_source
    body = ""
    for stub in stub_functions:
        if len(prefix) + len(body) + len(stub) + len(catalog_helpers) > _MAX_GENERATED_SOURCE_CHARS:
            break
        body += stub
    return prefix + body + catalog_helpers


# ---- Shared helpers section (embedded in both transport headers) ----------

_COMMON_HELPERS = '''\

# ---------------------------------------------------------------------------
# Convenience helpers (avoid common scripting pitfalls)
# ---------------------------------------------------------------------------

def json_parse(text: str):
    """Parse JSON tolerant of control characters (strict=False).
    Use this instead of json.loads() when parsing output from terminal()
    or web_extract() that may contain raw tabs/newlines in strings."""
    return json.loads(text, strict=False)


def shell_quote(s: str) -> str:
    """Shell-escape a string for safe interpolation into commands.
    Use this when inserting dynamic content into terminal() commands:
        terminal(f"echo {shell_quote(user_input)}")
    """
    return shlex.quote(s)


def retry(fn, max_attempts=3, delay=2):
    """Retry a function up to max_attempts times with exponential backoff.
    Use for transient failures (network errors, API rate limits):
        result = retry(lambda: terminal("gh issue list ..."))
    """
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay * (2 ** attempt))
    raise last_err

'''

# ---- UDS transport (local backend) ---------------------------------------

_UDS_TRANSPORT_HEADER = '''\
"""Auto-generated Hermes tools RPC stubs."""
import base64
import json, os, socket, shlex, threading, time

_sock = None
# The RPC server handles a single client connection serially and has no
# request-id in the protocol, so concurrent _call() invocations from multiple
# threads (e.g. ThreadPoolExecutor) would race on the shared socket and get
# each other's responses. Serialize the entire send+recv round-trip.
_call_lock = threading.Lock()
''' + _COMMON_HELPERS + '''\

def _connect():
    """Connect to the parent's RPC server via the transport it picked.

    HERMES_RPC_SOCKET can be either:
      - a filesystem path (POSIX Unix domain socket — the default on
        Linux and macOS)
      - a string of the form ``tcp://127.0.0.1:<port>`` (Windows, where
        AF_UNIX is unreliable — the parent falls back to loopback TCP)
    """
    global _sock
    if _sock is None:
        endpoint = os.environ["HERMES_RPC_SOCKET"]
        if endpoint.startswith("tcp://"):
            # tcp://host:port  (host is always 127.0.0.1 in practice — we
            # only bind loopback server-side)
            _host_port = endpoint[len("tcp://"):]
            _host, _, _port = _host_port.rpartition(":")
            _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _sock.connect((_host or "127.0.0.1", int(_port)))
        else:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _sock.connect(endpoint)
        _sock.settimeout(300)
    return _sock

def _call(tool_name, args):
    """Send a tool call to the parent process and return the parsed result."""
    request = json.dumps({
        "tool": tool_name,
        "args": args,
        "token": os.environ.get("HERMES_RPC_TOKEN", ""),
        "task_id": _HERMES_RPC_CONTEXT["task_id"],
        "session_id": _HERMES_RPC_CONTEXT["session_id"],
        "enabled_toolsets": _HERMES_RPC_CONTEXT["enabled_toolsets"],
        "disabled_toolsets": _HERMES_RPC_CONTEXT["disabled_toolsets"],
    }) + "\\n"
    with _call_lock:
        conn = _connect()
        conn.sendall(request.encode())
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                raise RuntimeError("Agent process disconnected")
            buf += chunk
            if buf.endswith(b"\\n"):
                break
    raw = buf.decode().strip()
    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

'''

# ---- File-based transport (remote backends) -------------------------------

_FILE_TRANSPORT_HEADER = '''\
"""Auto-generated Hermes tools RPC stubs (file-based transport)."""
import base64
import json, os, shlex, tempfile, threading, time

_RPC_DIR = os.environ.get("HERMES_RPC_DIR") or os.path.join(tempfile.gettempdir(), "hermes_rpc")
_seq = 0
# `_seq += 1` is not atomic (read-modify-write), so concurrent _call()
# invocations from multiple threads could allocate the same sequence number
# and clobber each other's request files. Guard seq allocation with a lock.
_seq_lock = threading.Lock()
''' + _COMMON_HELPERS + '''\

def _call(tool_name, args):
    """Send a tool call request via file-based RPC and wait for response."""
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq
    seq_str = f"{seq:06d}"
    req_file = os.path.join(_RPC_DIR, f"req_{seq_str}")
    res_file = os.path.join(_RPC_DIR, f"res_{seq_str}")

    # Write request atomically (write to .tmp, then rename).
    # encoding="utf-8" is critical: on Windows-hosted remote backends
    # (or any non-UTF-8 locale) the default open() mode would mangle
    # non-ASCII chars in tool args when encoding them as JSON.
    tmp = req_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "tool": tool_name,
            "args": args,
            "seq": seq,
            "token": os.environ.get("HERMES_RPC_TOKEN", ""),
            "task_id": _HERMES_RPC_CONTEXT["task_id"],
            "session_id": _HERMES_RPC_CONTEXT["session_id"],
            "enabled_toolsets": _HERMES_RPC_CONTEXT["enabled_toolsets"],
            "disabled_toolsets": _HERMES_RPC_CONTEXT["disabled_toolsets"],
        }, f)
    os.rename(tmp, req_file)

    # Wait for response with adaptive polling
    deadline = time.monotonic() + 300  # 5-minute timeout per tool call
    poll_interval = 0.05  # Start at 50ms
    while not os.path.exists(res_file):
        if time.monotonic() > deadline:
            raise RuntimeError(f"RPC timeout: no response for {tool_name} after 300s")
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.2, 0.25)  # Back off to 250ms

    with open(res_file, encoding="utf-8") as f:
        raw = f.read()

    # Clean up response file
    try:
        os.unlink(res_file)
    except OSError:
        pass

    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

'''


# ---------------------------------------------------------------------------
# RPC server (runs in a thread inside the parent process)
# ---------------------------------------------------------------------------

# Terminal parameters that must not be used from ephemeral sandbox scripts
_TERMINAL_BLOCKED_PARAMS = {"background", "pty", "notify_on_complete", "watch_patterns"}


def _rpc_server_loop(
    server_sock: socket.socket,
    task_id: str,
    tool_call_log: list,
    tool_call_counter: list,   # mutable [int] so the thread can increment
    max_tool_calls: int,
    allowed_tools: frozenset,
    stop_event: threading.Event,
    rpc_token: str,
    rpc_context: Optional[CodeExecutionContext] = None,
):
    """
    Accept one client connection and dispatch tool-call requests until
    the client disconnects or the call limit is reached.
    """
    allowed_tools = frozenset(_scriptable_tool_names(allowed_tools, {})) | SCRIPT_INTERNAL_TOOLS
    if isinstance(rpc_context, list):
        if not rpc_context:
            rpc_context.append(CodeExecutionContext(task_id, None, (), ()))
    else:
        rpc_context = rpc_context or CodeExecutionContext(task_id, None, (), ())
    conn = None
    try:
        server_sock.settimeout(0.05)
        while not stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
                break
            except socket.timeout:
                continue
        if conn is None:
            return
        conn.settimeout(300)

        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk

            # Process all complete newline-delimited messages in the buffer
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue

                call_start = time.monotonic()
                try:
                    request = json.loads(line.decode())
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    resp = tool_error(f"Invalid RPC request: {exc}")
                    conn.sendall((resp + "\n").encode())
                    continue

                if not rpc_token or not secrets.compare_digest(
                    str(request.get("token") or ""), rpc_token
                ):
                    resp = json.dumps({"error": "Unauthorized RPC request"})
                    conn.sendall((resp + "\n").encode())
                    continue

                tool_name = request.get("tool", "")
                tool_args = request.get("args", {})
                current_context = (
                    rpc_context[0] if isinstance(rpc_context, list) else rpc_context
                )
                request_context = _context_from_rpc_request(request, current_context)
                call_limit = (
                    max_tool_calls[0]
                    if isinstance(max_tool_calls, list)
                    else max_tool_calls
                )

                # Enforce the allow-list
                if tool_name not in allowed_tools:
                    available = ", ".join(sorted(allowed_tools))
                    resp = json.dumps({
                        "error": (
                            f"Tool '{tool_name}' is not available in execute_code. "
                            f"Available: {available}"
                        )
                    })
                    conn.sendall((resp + "\n").encode())
                    continue

                # Enforce tool call limit
                if tool_call_counter[0] >= call_limit:
                    resp = json.dumps({
                        "error": (
                            f"Tool call limit reached ({call_limit}). "
                            "No more tool calls allowed in this execution."
                        )
                    })
                    conn.sendall((resp + "\n").encode())
                    continue

                # Strip forbidden terminal parameters
                if tool_name == "terminal" and isinstance(tool_args, dict):
                    for param in _TERMINAL_BLOCKED_PARAMS:
                        tool_args.pop(param, None)

                # Dispatch through the standard tool handler.
                # Suppress stdout/stderr from internal tool handlers so
                # their status prints don't leak into the CLI spinner.
                try:
                    _real_stdout, _real_stderr = sys.stdout, sys.stderr
                    devnull = open(os.devnull, "w", encoding="utf-8")
                    approval_token = None
                    reset_approval_session = None
                    try:
                        from tools.approval import (
                            reset_current_session_key,
                            set_current_session_key,
                        )
                        approval_token = set_current_session_key(
                            request_context.session_id or "",
                        )
                        reset_approval_session = reset_current_session_key
                    except Exception:
                        pass
                    try:
                        sys.stdout = devnull
                        sys.stderr = devnull
                        result = json.dumps(
                            _dispatch_script_call(
                                tool_name, tool_args, request_context,
                            ),
                            ensure_ascii=False,
                        )
                    finally:
                        sys.stdout, sys.stderr = _real_stdout, _real_stderr
                        if approval_token is not None and reset_approval_session is not None:
                            reset_approval_session(approval_token)
                        devnull.close()
                except Exception as exc:
                    logger.error("Tool call failed in sandbox: %s", exc, exc_info=True)
                    result = tool_error(str(exc))

                tool_call_counter[0] += 1
                call_duration = time.monotonic() - call_start

                # Log for observability
                args_preview = str(tool_args)[:80]
                tool_call_log.append({
                    "tool": tool_name,
                    "args_preview": args_preview,
                    "duration": round(call_duration, 2),
                })

                conn.sendall((result + "\n").encode())

    except socket.timeout:
        logger.debug("RPC listener socket timeout")
    except OSError as e:
        logger.debug("RPC listener socket error: %s", e, exc_info=True)
    finally:
        if conn:
            try:
                conn.close()
            except OSError as e:
                logger.debug("RPC conn close error: %s", e)


# ---------------------------------------------------------------------------
# Remote execution support (file-based RPC via terminal backend)
# ---------------------------------------------------------------------------

def _get_or_create_env(task_id: str):
    """Get or create the terminal environment for *task_id*.

    Reuses the same environment (container/sandbox/SSH session) that the
    terminal and file tools use, creating one if it doesn't exist yet.
    Returns ``(env, env_type)`` tuple.
    """
    from tools.terminal_tool import (
        _active_environments, _env_lock, _create_environment,
        _get_env_config, _last_activity, _start_cleanup_thread,
        _creation_locks, _creation_locks_lock, _task_env_overrides,
        _resolve_container_task_id,
    )

    effective_task_id = _resolve_container_task_id(task_id)

    # Fast path: environment already exists
    with _env_lock:
        if effective_task_id in _active_environments:
            _last_activity[effective_task_id] = time.time()
            return _active_environments[effective_task_id], _get_env_config()["env_type"]

    # Slow path: create environment (same pattern as file_tools._get_file_ops)
    with _creation_locks_lock:
        if effective_task_id not in _creation_locks:
            _creation_locks[effective_task_id] = threading.Lock()
        task_lock = _creation_locks[effective_task_id]

    with task_lock:
        with _env_lock:
            if effective_task_id in _active_environments:
                _last_activity[effective_task_id] = time.time()
                return _active_environments[effective_task_id], _get_env_config()["env_type"]

        config = _get_env_config()
        env_type = config["env_type"]
        overrides = _task_env_overrides.get(effective_task_id, {})

        if env_type == "docker":
            image = overrides.get("docker_image") or config["docker_image"]
        elif env_type == "singularity":
            image = overrides.get("singularity_image") or config["singularity_image"]
        elif env_type == "modal":
            image = overrides.get("modal_image") or config["modal_image"]
        elif env_type == "daytona":
            image = overrides.get("daytona_image") or config["daytona_image"]
        else:
            image = ""

        cwd = overrides.get("cwd") or config["cwd"]

        container_config = None
        if env_type in {"docker", "singularity", "modal", "daytona"}:
            container_config = {
                "container_cpu": config.get("container_cpu", 1),
                "container_memory": config.get("container_memory", 5120),
                "container_disk": config.get("container_disk", 51200),
                "container_persistent": config.get("container_persistent", True),
                "docker_volumes": config.get("docker_volumes", []),
                "docker_run_as_host_user": config.get("docker_run_as_host_user", False),
                "docker_network": config.get("docker_network", True),
            }

        ssh_config = None
        if env_type == "ssh":
            ssh_config = {
                "host": config.get("ssh_host", ""),
                "user": config.get("ssh_user", ""),
                "port": config.get("ssh_port", 22),
                "key": config.get("ssh_key", ""),
                "persistent": config.get("ssh_persistent", False),
            }

        local_config = None
        if env_type == "local":
            local_config = {
                "persistent": config.get("local_persistent", False),
            }

        logger.info("Creating new %s environment for execute_code task %s...",
                     env_type, effective_task_id[:8])
        env = _create_environment(
            env_type=env_type,
            image=image,
            cwd=cwd,
            timeout=config["timeout"],
            ssh_config=ssh_config,
            container_config=container_config,
            local_config=local_config,
            task_id=effective_task_id,
            host_cwd=config.get("host_cwd"),
        )

        with _env_lock:
            _active_environments[effective_task_id] = env
            _last_activity[effective_task_id] = time.time()

        _start_cleanup_thread()
        logger.info("%s environment ready for execute_code task %s",
                     env_type, effective_task_id[:8])
        return env, env_type


def _ship_file_to_remote(env, remote_path: str, content: str) -> None:
    """Write *content* to *remote_path* on the remote environment.

    Uses ``echo … | base64 -d`` rather than stdin piping because some
    backends (Modal) don't reliably deliver stdin_data to chained
    commands.  Base64 output is shell-safe ([A-Za-z0-9+/=]) so single
    quotes are fine.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    quoted_remote_path = shlex.quote(remote_path)
    env.execute(
        f"echo '{encoded}' | base64 -d > {quoted_remote_path}",
        cwd="/",
        timeout=30,
    )


def _collect_remote_artifacts(
    env: Any,
    artifact_dir: str,
    limit: int,
    *,
    budget: Optional[ArtifactBudget] = None,
) -> tuple[list[dict], Optional[str]]:
    """Fetch bounded remote artifacts before the remote sandbox is removed."""
    budget = budget or ArtifactBudget()
    quoted_dir = shlex.quote(artifact_dir)
    try:
        listing = env.execute(
            f"find {quoted_dir} -maxdepth 1 -type f -print 2>/dev/null || true",
            cwd="/",
            timeout=15,
        )
        paths = [
            line.strip()
            for line in str(listing.get("output", "")).splitlines()
            if line.strip()
        ]
    except Exception:
        logger.debug("Could not list remote execute_code artifacts", exc_info=True)
        return [], None

    image_parts: list[dict] = []
    text_path = None
    root_prefix = artifact_dir.rstrip("/") + "/"
    for remote_path in paths:
        if not remote_path.startswith(root_prefix):
            continue
        name = os.path.basename(remote_path)
        suffix = os.path.splitext(name)[1].lower()
        try:
            quoted_path = shlex.quote(remote_path)
            size_result = env.execute(
                f"wc -c < {quoted_path}", cwd="/", timeout=15,
            )
            size_match = re.search(r"\d+", str(size_result.get("output", "")))
            size = int(size_match.group(0)) if size_match else 0
        except Exception:
            logger.debug("Could not stat remote artifact %s", remote_path, exc_info=True)
            continue

        is_image = suffix in _IMAGE_SUFFIXES
        if size <= 0:
            continue
        if not is_image and size <= limit:
            continue
        if size > MAX_ARTIFACT_BYTES and is_image:
            logger.warning("Skipping oversized remote image artifact %s", name)
            continue
        fetch_size = min(max(size, 0), MAX_ARTIFACT_BYTES)
        try:
            command = f"head -c {fetch_size} {shlex.quote(remote_path)} | base64"
            encoded_result = env.execute(command, cwd="/", timeout=60)
            encoded = "".join(str(encoded_result.get("output", "")).split())
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, OSError):
            logger.debug("Could not fetch remote artifact %s", remote_path, exc_info=True)
            continue
        except Exception:
            logger.debug("Could not fetch remote artifact %s", remote_path, exc_info=True)
            continue

        copied = _persist_file_artifact(
            data,
            name=name,
            allowed_roots=(),
            budget=budget,
        )
        part = copied.get("content", [None])[0]
        if part and part not in image_parts:
            image_parts.append(part)
        if not is_image and copied.get("artifact_path") and text_path is None:
            text_path = copied["artifact_path"]
    return image_parts, text_path


def _env_temp_dir(env: Any) -> str:
    """Return a writable temp dir for env-backed execute_code sandboxes."""
    get_temp_dir = getattr(env, "get_temp_dir", None)
    if callable(get_temp_dir):
        try:
            temp_dir = get_temp_dir()
            if isinstance(temp_dir, str) and temp_dir.startswith("/"):
                return temp_dir.rstrip("/") or "/"
        except Exception as exc:
            logger.debug("Could not resolve execute_code env temp dir: %s", exc)
    candidate = tempfile.gettempdir()
    if isinstance(candidate, str) and candidate.startswith("/"):
        return candidate.rstrip("/") or "/"
    return "/tmp"


def _rpc_poll_loop(
    env,
    rpc_dir: str,
    task_id: str,
    tool_call_log: list,
    tool_call_counter: list,
    max_tool_calls: int,
    allowed_tools: frozenset,
    stop_event: threading.Event,
    rpc_token: str,
    rpc_context: Optional[CodeExecutionContext] = None,
):
    """Poll the remote filesystem for tool call requests and dispatch them.

    Runs in a background thread.  Each ``env.execute()`` spawns an
    independent process, so these calls run safely concurrent with the
    script-execution thread.
    """
    allowed_tools = frozenset(_scriptable_tool_names(allowed_tools, {})) | SCRIPT_INTERNAL_TOOLS
    rpc_context = rpc_context or CodeExecutionContext(task_id, None, (), ())
    poll_interval = 0.1  # 100 ms

    quoted_rpc_dir = shlex.quote(rpc_dir)
    while not stop_event.is_set():
        try:
            # List pending request files (skip .tmp partials)
            ls_result = env.execute(
                f"ls -1 {quoted_rpc_dir}/req_* 2>/dev/null || true",
                cwd="/",
                timeout=10,
            )
            output = ls_result.get("output", "").strip()
            if not output:
                stop_event.wait(poll_interval)
                continue

            req_files = sorted([
                f.strip() for f in output.split("\n")
                if f.strip()
                and not f.strip().endswith(".tmp")
                and "/req_" in f.strip()
            ])

            for req_file in req_files:
                if stop_event.is_set():
                    break

                call_start = time.monotonic()

                quoted_req_file = shlex.quote(req_file)
                # Read request
                read_result = env.execute(
                    f"cat {quoted_req_file}",
                    cwd="/",
                    timeout=10,
                )
                try:
                    request = json.loads(read_result.get("output", ""))
                except (json.JSONDecodeError, ValueError):
                    logger.debug("Malformed RPC request in %s", req_file)
                    # Remove bad request to avoid infinite retry
                    env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
                    continue

                if not rpc_token or not secrets.compare_digest(
                    str(request.get("token") or ""), rpc_token
                ):
                    logger.debug("Unauthorized RPC request in %s", req_file)
                    env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
                    continue

                tool_name = request.get("tool", "")
                tool_args = request.get("args", {})
                request_context = _context_from_rpc_request(request, rpc_context)
                seq = request.get("seq", 0)
                seq_str = f"{seq:06d}"
                res_file = f"{rpc_dir}/res_{seq_str}"
                quoted_res_file = shlex.quote(res_file)

                # Enforce allow-list
                if tool_name not in allowed_tools:
                    available = ", ".join(sorted(allowed_tools))
                    tool_result = json.dumps({
                        "error": (
                            f"Tool '{tool_name}' is not available in execute_code. "
                            f"Available: {available}"
                        )
                    })
                # Enforce tool call limit
                elif tool_call_counter[0] >= max_tool_calls:
                    tool_result = json.dumps({
                        "error": (
                            f"Tool call limit reached ({max_tool_calls}). "
                            "No more tool calls allowed in this execution."
                        )
                    })
                else:
                    # Strip forbidden terminal parameters
                    if tool_name == "terminal" and isinstance(tool_args, dict):
                        for param in _TERMINAL_BLOCKED_PARAMS:
                            tool_args.pop(param, None)

                    # Dispatch through the standard tool handler
                    try:
                        _real_stdout, _real_stderr = sys.stdout, sys.stderr
                        devnull = open(os.devnull, "w", encoding="utf-8")
                        try:
                            sys.stdout = devnull
                            sys.stderr = devnull
                            tool_result = json.dumps(
                                _dispatch_script_call(
                                    tool_name, tool_args, request_context,
                                ),
                                ensure_ascii=False,
                            )
                        finally:
                            sys.stdout, sys.stderr = _real_stdout, _real_stderr
                            devnull.close()
                    except Exception as exc:
                        logger.error("Tool call failed in remote sandbox: %s",
                                     exc, exc_info=True)
                        tool_result = tool_error(str(exc))

                    tool_call_counter[0] += 1
                    call_duration = time.monotonic() - call_start
                    tool_call_log.append({
                        "tool": tool_name,
                        "args_preview": str(tool_args)[:80],
                        "duration": round(call_duration, 2),
                    })

                # Write response atomically (tmp + rename).
                # Use echo piping (not stdin_data) because Modal doesn't
                # reliably deliver stdin to chained commands.
                encoded_result = base64.b64encode(
                    tool_result.encode("utf-8")
                ).decode("ascii")
                env.execute(
                    f"echo '{encoded_result}' | base64 -d > {quoted_res_file}.tmp"
                    f" && mv {quoted_res_file}.tmp {quoted_res_file}",
                    cwd="/",
                    timeout=60,
                )

                # Remove the request file
                env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)

        except Exception as e:
            if not stop_event.is_set():
                logger.debug("RPC poll error: %s", e, exc_info=True)

        if not stop_event.is_set():
            stop_event.wait(poll_interval)


def _execute_remote(
    code: str,
    task_id: Optional[str],
    enabled_tools: Optional[List[str]],
    context: Optional[CodeExecutionContext] = None,
    timeout: Optional[float] = None,
) -> str:
    """Run a script on the remote terminal backend via file-based RPC.

    The script and the generated hermes_tools.py module are shipped to
    the remote environment, and tool calls are proxied through a polling
    thread that communicates via request/response files.
    """

    _cfg = _load_config()
    timeout = timeout if timeout is not None else _config_number(_cfg, "timeout", DEFAULT_TIMEOUT)
    max_tool_calls = _config_number(_cfg, "max_tool_calls", DEFAULT_MAX_TOOL_CALLS, integer=True)
    max_stdout_bytes = _config_number(_cfg, "max_stdout_bytes", MAX_STDOUT_BYTES, integer=True)
    context = context or CodeExecutionContext(task_id, None, (), ())

    requested_tools = SANDBOX_ALLOWED_TOOLS & set(enabled_tools or ())
    if not requested_tools:
        requested_tools = SANDBOX_ALLOWED_TOOLS
    sandbox_tools = frozenset(_scriptable_tool_names(requested_tools, {}))

    effective_task_id = task_id or "default"
    env, env_type = _get_or_create_env(effective_task_id)

    sandbox_id = uuid.uuid4().hex[:12]
    temp_dir = _env_temp_dir(env)
    sandbox_dir = f"{temp_dir}/hermes_exec_{sandbox_id}"
    artifact_dir = f"{sandbox_dir}/artifacts"
    context = replace(
        context,
        artifact_roots=(sandbox_dir, artifact_dir),
        artifact_budget=context.artifact_budget or ArtifactBudget(),
    )
    quoted_sandbox_dir = shlex.quote(sandbox_dir)
    quoted_rpc_dir = shlex.quote(f"{sandbox_dir}/rpc")

    tool_call_log: list = []
    tool_call_counter = [0]
    exec_start = time.monotonic()
    stop_event = threading.Event()
    rpc_thread = None
    remote_artifact_images: list[dict] = []
    remote_artifact_file: Optional[str] = None

    try:
        # Verify Python is available on the remote
        py_check = env.execute(
            "command -v python3 >/dev/null 2>&1 && echo OK",
            cwd="/", timeout=15,
        )
        if "OK" not in py_check.get("output", ""):
            return json.dumps({
                "status": "error",
                "error": (
                    f"Python 3 is not available in the {env_type} terminal "
                    "environment. Install Python to use execute_code with "
                    "remote backends."
                ),
                "tool_calls_made": 0,
                "duration_seconds": 0,
            })

        # Create sandbox directory on remote
        env.execute(
            f"mkdir -p {quoted_rpc_dir} {shlex.quote(artifact_dir)}", cwd="/", timeout=10,
        )

        rpc_token = secrets.token_urlsafe(32)

        # Generate and ship files
        tools_src = generate_hermes_tools_module(
            list(sandbox_tools), transport="file", context=context,
        )
        _ship_file_to_remote(env, f"{sandbox_dir}/hermes_tools.py", tools_src)
        _ship_file_to_remote(env, f"{sandbox_dir}/script.py", code)

        # Wrapped so the thread inherits the turn's approval context + callbacks
        # (see tools.thread_context) — else sandbox RPC tool calls lose approval
        # routing (#33057).
        rpc_thread = threading.Thread(
            target=propagate_context_to_thread(_rpc_poll_loop),
            args=(
                env, f"{sandbox_dir}/rpc", effective_task_id,
                tool_call_log, tool_call_counter, max_tool_calls,
                sandbox_tools, stop_event, rpc_token, context,
            ),
            daemon=True,
        )
        rpc_thread.start()

        # Build environment variable prefix for the script
        env_prefix = (
            f"HERMES_RPC_DIR={shlex.quote(f'{sandbox_dir}/rpc')} "
            f"HERMES_RPC_TOKEN={shlex.quote(rpc_token)} "
            f"HERMES_ARTIFACTS_DIR={shlex.quote(artifact_dir)} "
            f"PYTHONDONTWRITEBYTECODE=1"
        )
        tz = os.getenv("HERMES_TIMEZONE", "").strip()
        if tz:
            env_prefix += f" TZ={shlex.quote(tz)}"

        # Execute the script on the remote backend
        logger.info("Executing code on %s backend (task %s)...",
                     env_type, effective_task_id[:8])
        script_result = env.execute(
            f"cd {quoted_sandbox_dir} && {env_prefix} python3 script.py",
            timeout=timeout,
        )

        stdout_text = script_result.get("output", "")
        exit_code = script_result.get("returncode", -1)
        status = "success"

        # Check for timeout/interrupt from the backend
        if exit_code == 124:
            status = "timeout"
        elif exit_code == 130:
            status = "interrupted"

        # Collect remote artifacts while the sandbox still exists.  The durable
        # parent-side paths are then safe to return after cleanup.
        remote_artifact_images, remote_artifact_file = _collect_remote_artifacts(
            env,
            artifact_dir,
            max_stdout_bytes,
            budget=context.artifact_budget,
        )

    except Exception as exc:
        duration = round(time.monotonic() - exec_start, 2)
        logger.error(
            "execute_code remote failed after %ss with %d tool calls: %s: %s",
            duration, tool_call_counter[0], type(exc).__name__, exc,
            exc_info=True,
        )
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }, ensure_ascii=False)

    finally:
        # Stop the polling thread
        stop_event.set()
        if rpc_thread is not None:
            rpc_thread.join(timeout=5)

        # Clean up remote sandbox dir
        try:
            env.execute(
                f"rm -rf {quoted_sandbox_dir}", cwd="/", timeout=15,
            )
        except Exception:
            logger.debug("Failed to clean up remote sandbox %s", sandbox_dir)

    duration = round(time.monotonic() - exec_start, 2)

    # --- Post-process output (same as local path) ---
    stdout_text, stdout_artifact_path, stdout_image_parts = _prepare_execute_output(
        stdout_text, max_stdout_bytes, budget=context.artifact_budget,
    )

    # Build response
    result: Dict[str, Any] = {
        "status": status,
        "output": stdout_text,
        "tool_calls_made": tool_call_counter[0],
        "duration_seconds": duration,
    }

    if status == "timeout":
        timeout_msg = f"Script timed out after {timeout}s and was killed."
        result["error"] = timeout_msg
        # Include timeout message in output so the LLM always surfaces it
        # to the user (see local path comment — same reasoning, #10807).
        if stdout_text:
            result["output"] = stdout_text + f"\n\n⏰ {timeout_msg}"
        else:
            result["output"] = f"⏰ {timeout_msg}"
        logger.warning(
            "execute_code (remote) timed out after %ss (limit %ss) with %d tool calls",
            duration, timeout, tool_call_counter[0],
        )
    elif status == "interrupted":
        result["output"] = (
            stdout_text + "\n[execution interrupted — user sent a new message]"
        )
    elif exit_code != 0:
        result["status"] = "error"
        result["error"] = f"Script exited with code {exit_code}"

    _attach_execute_artifacts(
        result,
        [*stdout_image_parts, *remote_artifact_images],
        stdout_artifact_path or remote_artifact_file,
    )
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Opt-in persistent execution kernels
# ---------------------------------------------------------------------------

DEFAULT_KERNEL_IDLE_TTL = 15 * 60

_KERNEL_WORKER_SOURCE = r'''\
"""Private worker for one opt-in execute_code kernel."""
import contextlib
import io
import json
import os
import socket
import sys
import time
import traceback


def _connect():
    endpoint = os.environ["HERMES_KERNEL_SOCKET"]
    if endpoint.startswith("tcp://"):
        host, _, port = endpoint[len("tcp://"):].rpartition(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host or "127.0.0.1", int(port)))
    else:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(endpoint)
    return sock


def _new_globals():
    return {"__name__": "__main__", "__builtins__": __builtins__}


sock = _connect()
reader = sock.makefile("rb")
writer = sock.makefile("wb")
globals_ns = _new_globals()
for raw in reader:
    try:
        request = json.loads(raw.decode("utf-8"))
        context = request.get("context")
        if isinstance(context, dict):
            hermes_tools = sys.modules.get("hermes_tools")
            rpc_context = getattr(hermes_tools, "_HERMES_RPC_CONTEXT", None)
            if isinstance(rpc_context, dict):
                rpc_context.update({
                    "task_id": context.get("task_id"),
                    "session_id": context.get("session_id"),
                    "enabled_toolsets": list(context.get("enabled_toolsets") or ()),
                    "disabled_toolsets": list(context.get("disabled_toolsets") or ()),
                })
        code = request.get("code", "")
        stdout = io.StringIO()
        stderr = io.StringIO()
        started = time.monotonic()
        status = "success"
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                exec(compile(code, "<execute_code_kernel>", "exec"), globals_ns, globals_ns)
            except BaseException:
                status = "error"
                traceback.print_exc()
        response = {
            "status": status,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    except BaseException as exc:
        response = {
            "status": "error",
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "duration_seconds": 0,
        }
    writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
    writer.flush()
'''


def _kernel_listener(prefix: str):
    """Create a private local listener and its generated-child endpoint."""
    use_tcp = _IS_WINDOWS
    if use_tcp:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        host, port = listener.getsockname()[:2]
        endpoint = f"tcp://{host}:{port}"
        path = None
    else:
        path = os.path.join("/tmp", f"{prefix}_{uuid.uuid4().hex}.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        os.chmod(path, 0o600)
        endpoint = path
    listener.listen(1)
    return listener, endpoint, path


def _truncate_kernel_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted out of {len(text):,} total] ...\n\n"
        + text[-tail:]
    )


def _clean_kernel_output(text: str, limit: int) -> str:
    from tools.ansi_strip import strip_ansi
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(
        strip_ansi(_truncate_kernel_output(text or "", limit)),
        code_file=True,
    )


class ExecutionKernel:
    """One task-scoped child interpreter with shared globals between calls."""

    def __init__(
        self,
        task_id: str,
        kernel_id: str,
        sandbox_tools: frozenset,
        context: CodeExecutionContext,
        *,
        idle_ttl: float = DEFAULT_KERNEL_IDLE_TTL,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_stdout_bytes: int = MAX_STDOUT_BYTES,
        max_stderr_bytes: int = MAX_STDERR_BYTES,
        clock=time.monotonic,
    ):
        self.task_id = task_id
        self.kernel_id = kernel_id
        self.sandbox_tools = frozenset(sandbox_tools)
        self.context = context
        self.idle_ttl = float(idle_ttl)
        self.max_tool_calls = int(max_tool_calls)
        self._max_tool_calls = [self.max_tool_calls]
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.clock = clock
        self.last_activity = clock()
        self.process = None
        self._tmpdir = None
        self._rpc_server = None
        self._rpc_path = None
        self._control_server = None
        self._control_path = None
        self._control_conn = None
        self._control_buffer = b""
        self._rpc_thread = None
        self._stop_event = threading.Event()
        self._tool_call_log = []
        self._tool_call_counter = [0]
        self._rpc_context = [context]
        self._lock = threading.RLock()
        self.closed = False

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None and not self.closed

    def start(self) -> None:
        with self._lock:
            if self.alive:
                return
            self._tmpdir = tempfile.mkdtemp(prefix="hermes_kernel_")
            artifact_dir = os.path.join(self._tmpdir, "artifacts")
            os.makedirs(artifact_dir, mode=0o700, exist_ok=True)
            self.context = replace(
                self.context,
                artifact_roots=(self._tmpdir, artifact_dir),
                artifact_budget=self.context.artifact_budget or ArtifactBudget(),
            )
            self._rpc_context[0] = self.context
            tools_src = generate_hermes_tools_module(
                list(self.sandbox_tools), context=self.context,
            )
            with open(os.path.join(self._tmpdir, "hermes_tools.py"), "w", encoding="utf-8") as handle:
                handle.write(tools_src)
            worker_path = os.path.join(self._tmpdir, "kernel_worker.py")
            with open(worker_path, "w", encoding="utf-8") as handle:
                handle.write(_KERNEL_WORKER_SOURCE)

            self._rpc_server, rpc_endpoint, self._rpc_path = _kernel_listener("hermes_kernel_rpc")
            self._control_server, control_endpoint, self._control_path = _kernel_listener("hermes_kernel_ctl")
            rpc_token = secrets.token_urlsafe(32)
            self._rpc_thread = threading.Thread(
                target=propagate_context_to_thread(_rpc_server_loop),
                args=(
                    self._rpc_server, self.task_id, self._tool_call_log,
                    self._tool_call_counter, self._max_tool_calls,
                    self.sandbox_tools, self._stop_event, rpc_token, self._rpc_context,
                ),
                daemon=True,
            )
            self._rpc_thread.start()

            child_env = _scrub_child_env(os.environ)
            child_env.update({
                "HERMES_RPC_SOCKET": rpc_endpoint,
                "HERMES_RPC_TOKEN": rpc_token,
                "HERMES_KERNEL_SOCKET": control_endpoint,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            })
            hermes_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            existing_pp = child_env.get("PYTHONPATH", "")
            pp_parts = [self._tmpdir, hermes_root]
            if existing_pp:
                pp_parts.append(existing_pp)
            child_env["PYTHONPATH"] = os.pathsep.join(pp_parts)
            tz = os.getenv("HERMES_TIMEZONE", "").strip()
            if tz:
                child_env["TZ"] = tz
            child_env.pop("HERMES_TIMEZONE", None)
            from hermes_constants import apply_subprocess_home_env
            apply_subprocess_home_env(child_env)

            child_python = _resolve_child_python(_get_execution_mode())
            self.process = subprocess.Popen(
                [child_python, worker_path],
                cwd=self._tmpdir,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
            )
            self._control_server.settimeout(5)
            try:
                self._control_conn, _ = self._control_server.accept()
            except (socket.timeout, OSError):
                raise RuntimeError("Persistent execute_code kernel failed to start.")
            self._control_conn.settimeout(None)
            self.last_activity = self.clock()

    def execute(
        self,
        code: str,
        timeout: float,
        *,
        max_tool_calls: Optional[int] = None,
        context: Optional[CodeExecutionContext] = None,
    ) -> dict:
        with self._lock:
            if context is not None:
                self.context = replace(
                    context,
                    artifact_roots=self.context.artifact_roots,
                    artifact_budget=ArtifactBudget(),
                )
                self._rpc_context[0] = self.context
            if max_tool_calls is not None:
                self.max_tool_calls = int(max_tool_calls)
                self._max_tool_calls[0] = self.max_tool_calls
            self._tool_call_counter[0] = 0
            self._tool_call_log.clear()
            self.last_activity = self.clock()
            if not self.alive:
                self.start()
            request = (json.dumps({
                "code": code,
                "context": _context_payload(self.context),
            }, ensure_ascii=False) + "\n").encode("utf-8")
            exec_start = time.monotonic()
            deadline = exec_start + max(0.01, float(timeout))
            try:
                from tools.interrupt import is_interrupted

                self._control_conn.sendall(request)
                while b"\n" not in self._control_buffer:
                    if is_interrupted():
                        _kill_process_group(self.process)
                        self.close()
                        duration = round(time.monotonic() - exec_start, 2)
                        return {
                            "status": "interrupted",
                            "output": "\n[execution interrupted — user sent a new message]",
                            "stdout": "",
                            "stderr": "",
                            "interrupted": True,
                            "kernel_restarted": True,
                            "tool_calls_made": self._tool_call_counter[0],
                            "duration_seconds": duration,
                            "persistent": True,
                            "kernel_id": self.kernel_id,
                        }
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout()
                    self._control_conn.settimeout(min(0.2, remaining))
                    try:
                        chunk = self._control_conn.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise RuntimeError("Persistent execute_code kernel exited.")
                    self._control_buffer += chunk
                line, self._control_buffer = self._control_buffer.split(b"\n", 1)
                response = json.loads(line.decode("utf-8"))
                self.last_activity = self.clock()
            except socket.timeout:
                _kill_process_group(self.process, escalate=True)
                self.close()
                return {
                    "status": "timeout",
                    "output": f"⏰ Kernel timed out after {timeout}s and was killed.",
                    "stdout": "",
                    "stderr": "",
                    "timed_out": True,
                    "kernel_restarted": True,
                    "tool_calls_made": self._tool_call_counter[0],
                    "duration_seconds": round(float(timeout), 2),
                    "error": f"Kernel timed out after {timeout}s and was killed.",
                }
            except Exception as exc:
                self.close()
                return {
                    "status": "error",
                    "output": "",
                    "stdout": "",
                    "stderr": "",
                    "error": str(exc),
                    "tool_calls_made": self._tool_call_counter[0],
                    "duration_seconds": 0,
                }

            stdout = _clean_kernel_output(response.get("stdout", ""), self.max_stdout_bytes)
            stderr = _clean_kernel_output(response.get("stderr", ""), self.max_stderr_bytes)
            result = {
                "status": response.get("status", "error"),
                "output": stdout,
                "stdout": stdout,
                "stderr": stderr,
                "tool_calls_made": self._tool_call_counter[0],
                "duration_seconds": response.get("duration_seconds", 0),
                "persistent": True,
                "kernel_id": self.kernel_id,
            }
            if stderr:
                result["error"] = stderr
                result["output"] = (stdout + "\n--- stderr ---\n" + stderr).strip()
                result["stdout"] = stdout
            return result

    def reset(self) -> None:
        """Terminate this kernel; the registry creates a fresh one on demand."""
        self.close()

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._stop_event.set()
            if self._control_conn is not None:
                try:
                    self._control_conn.close()
                except OSError:
                    pass
            if self.process is not None and self.process.poll() is None:
                _kill_process_group(self.process, escalate=True)
            if self._rpc_server is not None:
                try:
                    self._rpc_server.close()
                except OSError:
                    pass
            if self._control_server is not None:
                try:
                    self._control_server.close()
                except OSError:
                    pass
            if self._rpc_thread is not None:
                self._rpc_thread.join(timeout=3)
            for pipe in (getattr(self.process, "stdout", None), getattr(self.process, "stderr", None)):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            import shutil
            shutil.rmtree(self._tmpdir or "", ignore_errors=True)
            for path in (self._rpc_path, self._control_path):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass


class ExecutionKernelRegistry:
    """Thread-safe registry scoped by parent task and explicit kernel id."""

    def __init__(self, *, clock=time.monotonic, idle_ttl=DEFAULT_KERNEL_IDLE_TTL):
        self.clock = clock
        self.idle_ttl = float(idle_ttl)
        self._kernels = {}
        self._expired_keys = set()
        self._lock = threading.RLock()
        self._reaper_stop = threading.Event()
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop,
            name="execute-code-kernel-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def _reaper_loop(self):
        while not self._reaper_stop.is_set():
            with self._lock:
                ttls = [
                    kernel.idle_ttl
                    for kernel in self._kernels.values()
                    if kernel.idle_ttl >= 0
                ]
            ttl = min(ttls, default=self.idle_ttl)
            interval = min(0.5, max(0.01, ttl / 2 if ttl > 0 else 0.05))
            if self._reaper_stop.wait(interval):
                break
            try:
                self.cleanup_expired()
            except Exception:
                logger.debug("Persistent kernel reaper failed", exc_info=True)

    @staticmethod
    def _key(task_id, kernel_id):
        if task_id is None or not str(task_id).strip():
            raise ValueError("Persistent kernels require an explicit task_id.")
        return (str(task_id), str(kernel_id or "default"))

    def get(self, task_id, kernel_id):
        self.cleanup_expired()
        key = self._key(task_id, kernel_id)
        with self._lock:
            kernel = self._kernels.get(key)
            if kernel is not None and not kernel.alive:
                self._kernels.pop(key, None)
                kernel.close()
                return None
            return kernel

    def get_or_create(
        self,
        task_id,
        kernel_id,
        sandbox_tools,
        context,
        *,
        idle_ttl=None,
        max_tool_calls=DEFAULT_MAX_TOOL_CALLS,
        max_stdout_bytes=MAX_STDOUT_BYTES,
        max_stderr_bytes=MAX_STDERR_BYTES,
    ):
        self.cleanup_expired()
        key = self._key(task_id, kernel_id)
        with self._lock:
            kernel = self._kernels.get(key)
            if kernel is not None and kernel.alive:
                return kernel
            if kernel is not None:
                kernel.close()
            kernel = ExecutionKernel(
                key[0], key[1], sandbox_tools, context,
                idle_ttl=self.idle_ttl if idle_ttl is None else idle_ttl,
                max_tool_calls=max_tool_calls,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
                clock=self.clock,
            )
            self._kernels[key] = kernel
            try:
                kernel.start()
            except Exception:
                self._kernels.pop(key, None)
                kernel.close()
                raise
            return kernel

    def reset(self, task_id, kernel_id):
        key = self._key(task_id, kernel_id)
        with self._lock:
            self._expired_keys.discard(key)
            kernel = self._kernels.pop(key, None)
        if kernel is not None:
            kernel.close()
            return True
        return False

    def remove(self, task_id, kernel_id, *, close=True):
        key = self._key(task_id, kernel_id)
        with self._lock:
            self._expired_keys.discard(key)
            kernel = self._kernels.pop(key, None)
        if kernel is not None and close:
            kernel.close()
        return kernel

    def cleanup_task(self, task_id):
        if task_id is None or not str(task_id).strip():
            return 0
        task = str(task_id)
        with self._lock:
            kernels = [self._kernels.pop(key) for key in list(self._kernels) if key[0] == task]
            self._expired_keys = {key for key in self._expired_keys if key[0] != task}
        for kernel in kernels:
            kernel.close()
        return len(kernels)

    def for_task(self, task_id):
        if task_id is None or not str(task_id).strip():
            return {}
        task = str(task_id)
        with self._lock:
            return {key[1]: kernel for key, kernel in self._kernels.items() if key[0] == task}

    def cleanup_expired(self):
        now = self.clock()
        with self._lock:
            expired = []
            for key, kernel in list(self._kernels.items()):
                if kernel.idle_ttl < 0 or now - kernel.last_activity < kernel.idle_ttl:
                    continue
                expired.append(self._kernels.pop(key))
                self._expired_keys.add(key)
        for kernel in expired:
            kernel.close()
        return len(expired)

    def consume_expired(self, task_id, kernel_id):
        key = self._key(task_id, kernel_id)
        with self._lock:
            if key not in self._expired_keys:
                return False
            self._expired_keys.remove(key)
            return True

    def close_all(self):
        self._reaper_stop.set()
        with self._lock:
            kernels = list(self._kernels.values())
            self._kernels.clear()
            self._expired_keys.clear()
        for kernel in kernels:
            kernel.close()
        reaper = self._reaper_thread
        if reaper is not threading.current_thread():
            reaper.join(timeout=2)


_kernel_registry = ExecutionKernelRegistry()
atexit.register(_kernel_registry.close_all)


def _kernel_registry_for_task(task_id):
    _kernel_registry.cleanup_expired()
    return _kernel_registry.for_task(task_id)


def cleanup_execution_kernels(task_id):
    """Terminate and forget every persistent kernel owned by *task_id*."""
    return _kernel_registry.cleanup_task(task_id)


def reap_kernels_for_task(task_id):
    """Compatibility alias for environment teardown callers."""
    return cleanup_execution_kernels(task_id)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def execute_code(
    code: Optional[str] = None,
    task_id: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    *,
    session_id: Optional[str] = None,
    enabled_toolsets: Optional[List[str]] = None,
    disabled_toolsets: Optional[List[str]] = None,
    persistent: Optional[bool] = None,
    kernel_id: Optional[str] = None,
    session: Optional[str] = None,
    reset: bool = False,
    timeout: Optional[float] = None,
    idle_ttl: Optional[float] = None,
    kernel_idle_ttl: Optional[float] = None,
) -> str:
    """
    Run a Python script in a sandboxed child process with RPC access
    to a subset of Hermes tools.

    Dispatches to the local (UDS) or remote (file-based RPC) path
    depending on the configured terminal backend.

    Args:
        code:          Python source code to execute.
        task_id:       Session task ID for tool isolation (terminal env, etc.).
        enabled_tools: Tool names enabled in the current session. The sandbox
                       gets the intersection with SANDBOX_ALLOWED_TOOLS.
        persistent:    Opt into a task-scoped interpreter reused across calls.
        kernel_id:     Explicit persistent-kernel name; ``session`` is an alias.
        reset:         Terminate the matching persistent kernel before running.
        timeout:       Per-call timeout override in seconds.

    Returns:
        JSON string with execution results.
    """
    if not SANDBOX_AVAILABLE:
        return json.dumps({
            "error": "execute_code sandbox is unavailable in this environment. "
                     "Use normal tool calls (terminal, read_file, write_file, ...) instead."
        })

    _cfg = _load_config()
    if persistent is None:
        persistent = _cfg.get("persistent") is True
    persistent_requested = bool(persistent or session or kernel_id or reset)
    selected_kernel_id = str(kernel_id or session or "default")
    if persistent_requested and (task_id is None or not str(task_id).strip()):
        return json.dumps({
            "status": "error",
            "error": "Persistent execute_code requires an explicit task_id.",
            "persistent": True,
            "kernel_id": selected_kernel_id,
            "tool_calls_made": 0,
            "duration_seconds": 0,
        }, ensure_ascii=False)
    if reset:
        reset_done = _kernel_registry.reset(task_id, selected_kernel_id)
        if not code or not code.strip():
            return json.dumps({
                "status": "success",
                "output": "",
                "stdout": "",
                "persistent": persistent_requested,
                "kernel_id": selected_kernel_id,
                "kernel_reset": reset_done,
                "tool_calls_made": 0,
                "duration_seconds": 0,
            })
        persistent_requested = True
    elif not code or not code.strip():
        return tool_error("No code provided.")

    context = CodeExecutionContext(
        task_id=task_id,
        session_id=session_id,
        enabled_toolsets=tuple(str(name) for name in (enabled_toolsets or ())),
        disabled_toolsets=tuple(str(name) for name in (disabled_toolsets or ())),
        artifact_budget=ArtifactBudget(),
    )

    # Dispatch: remote backends use file-based RPC, local uses UDS
    from tools.terminal_tool import _get_env_config, _docker_has_host_access
    _env_config = _get_env_config()
    env_type = _env_config["env_type"]

    # execute_code runs arbitrary Python (subprocess/os.system/...) that never
    # passes through terminal()/DANGEROUS_PATTERNS, so guard the whole script
    # here before either dispatch path spawns it. Runs synchronously in the
    # caller (tool-executor) thread, which holds the session context (#30882).
    # A Docker sandbox with host bind mounts is no longer isolated, so its
    # script does not get the container fast-path.
    from tools.approval import check_execute_code_guard
    _guard = check_execute_code_guard(
        code, env_type,
        has_host_access=_docker_has_host_access(_env_config),
    )
    if not _guard.get("approved", False):
        return json.dumps({
            "status": "error",
            "error": _guard.get("message") or "execute_code blocked by approval guard.",
            "tool_calls_made": 0,
            "duration_seconds": 0,
            **{
                key: _guard[key]
                for key in (
                    "request_id", "argument_hash", "operation", "tool_name",
                    "created_at", "expires_at",
                )
                if key in _guard
            },
        }, ensure_ascii=False)

    # Clean interrupt slate for a user-approved script before EITHER dispatch
    # path spawns it: drop a stale bit that landed on this thread during the
    # blocking approval-wait so it can't kill the just-approved run on the first
    # poll (local _wait_for_process loop, or remote/ssh env.execute which routes
    # through the same poll loop).  A genuine post-clear interrupt re-sets the
    # bit and is still caught downstream.
    if _guard.get("user_approved"):
        from tools.interrupt import clear_current_thread_interrupt
        clear_current_thread_interrupt()

    if persistent_requested:
        if env_type != "local":
            return json.dumps({
                "status": "error",
                "error": "Persistent execute_code kernels currently require the local terminal backend.",
                "tool_calls_made": 0,
                "duration_seconds": 0,
            })
        cfg = _cfg
        kernel_timeout = timeout if timeout is not None else _config_number(cfg, "timeout", DEFAULT_TIMEOUT)
        max_tool_calls = _config_number(cfg, "max_tool_calls", DEFAULT_MAX_TOOL_CALLS, integer=True)
        max_stdout_bytes = _config_number(cfg, "max_stdout_bytes", MAX_STDOUT_BYTES, integer=True)
        max_stderr_bytes = _config_number(cfg, "max_stderr_bytes", MAX_STDERR_BYTES, integer=True)
        configured_idle_ttl = (
            _config_number(cfg, "kernel_idle_ttl", DEFAULT_KERNEL_IDLE_TTL)
            if "kernel_idle_ttl" in cfg
            else None
        )
        requested_tools = SANDBOX_ALLOWED_TOOLS & set(enabled_tools or ())
        if not requested_tools:
            requested_tools = SANDBOX_ALLOWED_TOOLS
        sandbox_tools = frozenset(_scriptable_tool_names(requested_tools, {}))
        try:
            kernel = _kernel_registry.get_or_create(
                task_id,
                selected_kernel_id,
                sandbox_tools,
                context,
                idle_ttl=(
                    kernel_idle_ttl
                    if kernel_idle_ttl is not None
                    else idle_ttl
                    if idle_ttl is not None
                    else configured_idle_ttl
                ),
                max_tool_calls=max_tool_calls,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
            )
            kernel_expired = _kernel_registry.consume_expired(task_id, selected_kernel_id)
            result = kernel.execute(
                code,
                kernel_timeout,
                max_tool_calls=max_tool_calls,
                context=context,
            )
            if kernel_expired:
                result["kernel_expired"] = True
            if result.get("status") == "timeout" or not kernel.alive:
                _kernel_registry.remove(task_id, selected_kernel_id)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            _kernel_registry.reset(task_id, selected_kernel_id)
            return json.dumps({
                "status": "error",
                "error": str(exc),
                "tool_calls_made": 0,
                "duration_seconds": 0,
            }, ensure_ascii=False)

    if env_type != "local":
        try:
            if timeout is None:
                return _execute_remote(code, task_id, enabled_tools, context)
            return _execute_remote(code, task_id, enabled_tools, context, timeout=timeout)
        except TypeError as exc:
            # Preserve tiny legacy test/integration adapters that still expose
            # the pre-context three-argument remote seam.
            if "positional argument" not in str(exc) and "unexpected keyword" not in str(exc):
                raise
            return _execute_remote(code, task_id, enabled_tools)

    # --- Local execution path (UDS) --- below this line is unchanged ---

    # Import per-thread interrupt check (cooperative cancellation)
    from tools.interrupt import is_interrupted as _is_interrupted

    # Resolve config
    timeout = timeout if timeout is not None else _config_number(_cfg, "timeout", DEFAULT_TIMEOUT)
    max_tool_calls = _config_number(_cfg, "max_tool_calls", DEFAULT_MAX_TOOL_CALLS, integer=True)
    max_stdout_bytes = _config_number(_cfg, "max_stdout_bytes", MAX_STDOUT_BYTES, integer=True)
    max_stderr_bytes = _config_number(_cfg, "max_stderr_bytes", MAX_STDERR_BYTES, integer=True)

    # Determine which tools the sandbox can call
    requested_tools = SANDBOX_ALLOWED_TOOLS & set(enabled_tools or ())
    if not requested_tools:
        requested_tools = SANDBOX_ALLOWED_TOOLS
    sandbox_tools = frozenset(_scriptable_tool_names(requested_tools, {}))

    # --- Set up temp directory with hermes_tools.py and script.py ---
    tmpdir = tempfile.mkdtemp(prefix="hermes_sandbox_")
    artifact_dir = os.path.join(tmpdir, "artifacts")
    os.makedirs(artifact_dir, mode=0o700, exist_ok=True)
    context = replace(context, artifact_roots=(tmpdir, artifact_dir))
    # Use /tmp on macOS to avoid the long /var/folders/... path that pushes
    # Unix domain socket paths past the 104-byte macOS AF_UNIX limit.
    # On Linux, tempfile.gettempdir() already returns /tmp.
    #
    # Windows: Python 3.9+ added partial AF_UNIX support but the file-backed
    # variant is flaky across Windows builds (requires Windows 10 1803+,
    # still fails under some configurations, and the socket file can't live
    # on the same temp drive as the script).  Fall back to loopback TCP —
    # same ephemeral port, same 1-connection listen queue, same serialized
    # request/response framing.  The generated client reads the transport
    # selector from HERMES_RPC_SOCKET (path vs. ``tcp://host:port``).
    _sock_tmpdir = "/tmp" if sys.platform == "darwin" else tempfile.gettempdir()
    _use_tcp_rpc = _IS_WINDOWS
    if _use_tcp_rpc:
        sock_path = None  # not used on Windows; TCP endpoint stored below
        rpc_endpoint = None  # set after bind()
    else:
        sock_path = os.path.join(_sock_tmpdir, f"hermes_rpc_{uuid.uuid4().hex}.sock")
        rpc_endpoint = sock_path

    tool_call_log: list = []
    tool_call_counter = [0]  # mutable so the RPC thread can increment
    exec_start = time.monotonic()
    server_sock = None
    stop_event = threading.Event()

    try:
        # Write the auto-generated hermes_tools module.
        # encoding="utf-8" is required on Windows — the stub and user code
        # both contain non-ASCII characters (em-dashes in docstrings, plus
        # whatever the user script carries).  Python's default open() uses
        # the system locale on Windows (cp1252 typically), which corrupts
        # those bytes; the child then fails to import with a SyntaxError
        # ("'utf-8' codec can't decode byte 0x97 in position ...") because
        # Python source files are decoded as UTF-8 by default (PEP 3120).
        # sandbox_tools is already the correct set (intersection with session
        # tools, or SANDBOX_ALLOWED_TOOLS as fallback — see lines above).
        tools_src = generate_hermes_tools_module(
            list(sandbox_tools), context=context,
        )
        with open(os.path.join(tmpdir, "hermes_tools.py"), "w", encoding="utf-8") as f:
            f.write(tools_src)

        # Write the user's script
        with open(os.path.join(tmpdir, "script.py"), "w", encoding="utf-8") as f:
            f.write(code)

        # --- Start RPC server ---
        rpc_token = secrets.token_urlsafe(32)
        # Two transports:
        #   POSIX: AF_UNIX stream socket on sock_path, chmod 0600 for
        #   owner-only access.  Filesystem permissions gate the socket.
        #   Windows: AF_INET stream socket on 127.0.0.1 with an ephemeral
        #   port.  No filesystem permission story, but loopback-only bind
        #   means only the current user's processes (not remote) can
        #   connect.  HERMES_RPC_SOCKET is set to ``tcp://127.0.0.1:<port>``
        #   which the generated client parses to pick AF_INET.
        if _use_tcp_rpc:
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_sock.bind(("127.0.0.1", 0))  # ephemeral port
            _host, _port = server_sock.getsockname()[:2]
            rpc_endpoint = f"tcp://{_host}:{_port}"
        else:
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(sock_path)
            os.chmod(sock_path, 0o600)
        server_sock.listen(1)

        # Wrapped so the thread inherits the turn's approval context + callbacks
        # (see tools.thread_context) — else gateway sandbox tool calls silently
        # auto-approve dangerous commands (#33057, #30882).
        rpc_thread = threading.Thread(
            target=propagate_context_to_thread(_rpc_server_loop),
            args=(
                server_sock, task_id, tool_call_log,
                tool_call_counter, max_tool_calls, sandbox_tools, stop_event,
                rpc_token, context,
            ),
            daemon=True,
        )
        rpc_thread.start()

        # --- Spawn child process ---
        # Build a minimal environment for the child. We intentionally exclude
        # API keys and tokens to prevent credential exfiltration from LLM-
        # generated scripts. The child accesses tools via RPC, not direct API.
        # Exception: env vars declared by loaded skills (via env_passthrough
        # registry) or explicitly allowed by the user in config.yaml
        # (terminal.env_passthrough) are passed through.  On Windows, a small
        # OS-essential allowlist (SYSTEMROOT, WINDIR, COMSPEC, ...) is also
        # passed through — without those, the child can't create a socket
        # or spawn a subprocess.  See ``_scrub_child_env`` for the rules.
        child_env = _scrub_child_env(os.environ)
        child_env["HERMES_RPC_SOCKET"] = rpc_endpoint
        child_env["HERMES_RPC_TOKEN"] = rpc_token
        child_env["HERMES_ARTIFACTS_DIR"] = artifact_dir
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Force UTF-8 for the child's stdio and default file encoding.
        #
        # Without this, on Windows sys.stdout is bound to the console code
        # page (cp1252 on US-locale installs), and any script that does
        # ``print("café")`` or ``print("→")`` crashes with:
        #
        #   UnicodeEncodeError: 'charmap' codec can't encode character
        #   '\u2192' in position N: character maps to <undefined>
        #
        # PYTHONIOENCODING fixes sys.stdin/stdout/stderr.
        # PYTHONUTF8=1 enables "UTF-8 mode" (PEP 540) which additionally
        # makes ``open()``'s default encoding UTF-8, so user scripts that
        # write files without specifying encoding= also work correctly.
        #
        # On POSIX both values usually match the locale default already,
        # so setting them is harmless belt-and-suspenders for environments
        # with a C/POSIX locale (containers, minimal base images).
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        # Ensure the hermes-agent root is importable in the sandbox so
        # repo-root modules are available to child scripts.  We also prepend
        # the staging tmpdir so ``from hermes_tools import ...`` resolves even
        # when the subprocess CWD is not tmpdir (project mode).
        _hermes_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _existing_pp = child_env.get("PYTHONPATH", "")
        _pp_parts = [tmpdir, _hermes_root]
        if _existing_pp:
            _pp_parts.append(_existing_pp)
        child_env["PYTHONPATH"] = os.pathsep.join(_pp_parts)
        # Inject user's configured timezone so datetime.now() in sandboxed
        # code reflects the correct wall-clock time.  Only TZ is set —
        # HERMES_TIMEZONE is an internal Hermes setting and must not leak
        # into child processes.
        _tz_name = os.getenv("HERMES_TIMEZONE", "").strip()
        if _tz_name:
            child_env["TZ"] = _tz_name
        child_env.pop("HERMES_TIMEZONE", None)

        from hermes_constants import apply_subprocess_home_env
        apply_subprocess_home_env(child_env)

        # Resolve interpreter + CWD based on execute_code mode.
        #   - strict : today's behavior (sys.executable + tmpdir CWD).
        #   - project: user's venv python + session's working directory, so
        #              project deps like pandas and user files resolve.
        # Env scrubbing and tool whitelist apply identically in both modes.
        _mode = _get_execution_mode()
        _child_python = _resolve_child_python(_mode)
        _child_cwd = _resolve_child_cwd(_mode, tmpdir)
        _script_path = os.path.join(tmpdir, "script.py")

        proc = subprocess.Popen(
            [_child_python, _script_path],
            cwd=_child_cwd,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
        )

        # --- Poll loop: watch for exit, timeout, and interrupt ---
        deadline = time.monotonic() + timeout
        stderr_chunks: list = []

        # Background readers avoid pipe-buffer deadlocks. Keep complete stdout
        # so a redacted copy can be persisted before the display value is capped.
        def _drain(pipe, chunks, max_bytes):
            """Head-only drain for stderr."""
            total = 0
            try:
                while True:
                    data = pipe.read(4096)
                    if not data:
                        break
                    if total < max_bytes:
                        keep = max_bytes - total
                        chunks.append(data[:keep])
                    total += len(data)
            except (ValueError, OSError) as e:
                logger.debug("Error reading process output: %s", e, exc_info=True)

        def _drain_full(pipe, chunks):
            try:
                while True:
                    data = pipe.read(4096)
                    if not data:
                        break
                    chunks.append(data)
            except (ValueError, OSError) as e:
                logger.debug("Error reading process output: %s", e, exc_info=True)

        stdout_full_chunks: list = []
        stdout_reader = threading.Thread(
            target=_drain_full, args=(proc.stdout, stdout_full_chunks), daemon=True,
        )
        stderr_reader = threading.Thread(
            target=_drain, args=(proc.stderr, stderr_chunks, max_stderr_bytes), daemon=True
        )
        stdout_reader.start()
        stderr_reader.start()

        status = "success"
        _activity_state = {
            "last_touch": time.monotonic(),
            "start": exec_start,
        }
        try:
            from tools.environments.base import touch_activity_if_due
        except Exception:
            touch_activity_if_due = None
        poll_interval = 0.005
        while proc.poll() is None:
            if _is_interrupted():
                _kill_process_group(proc)
                status = "interrupted"
                break
            now = time.monotonic()
            if now > deadline:
                _kill_process_group(proc, escalate=True)
                status = "timeout"
                break
            # Periodic activity touch so the gateway's inactivity timeout
            # doesn't kill the agent during long code execution (#10807).
            if touch_activity_if_due is not None:
                try:
                    touch_activity_if_due(_activity_state, "execute_code running")
                except Exception:
                    pass
            try:
                proc.wait(timeout=min(poll_interval, max(0.0, deadline - now)))
            except subprocess.TimeoutExpired:
                pass
            poll_interval = min(0.2, poll_interval * 1.5)

        # Wait for readers to finish draining
        stdout_reader.join(timeout=3)
        stderr_reader.join(timeout=3)

        stdout_full = b"".join(stdout_full_chunks).decode("utf-8", errors="replace")
        stderr_text = _clean_execute_text(
            b"".join(stderr_chunks).decode("utf-8", errors="replace")
        )
        stdout_text, stdout_artifact_path, stdout_image_parts = _prepare_execute_output(
            stdout_full, max_stdout_bytes, budget=context.artifact_budget,
        )

        exit_code = proc.returncode if proc.returncode is not None else -1
        duration = round(time.monotonic() - exec_start, 2)

        # Wait for RPC thread to finish
        stop_event.set()
        server_sock.close()  # break accept() so thread exits promptly
        server_sock = None  # prevent double close in finally
        rpc_thread.join(timeout=3)

        # Output was normalized before truncation so spill files never contain raw secrets.
        # Build response
        result: Dict[str, Any] = {
            "status": status,
            "output": stdout_text,
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }

        if status == "timeout":
            timeout_msg = f"Script timed out after {timeout}s and was killed."
            result["error"] = timeout_msg
            # Include timeout message in output so the LLM always surfaces it
            # to the user.  When output is empty, models often treat the result
            # as "nothing happened" and produce an empty response, which the
            # gateway stream consumer silently drops (#10807).
            if stdout_text:
                result["output"] = stdout_text + f"\n\n⏰ {timeout_msg}"
            else:
                result["output"] = f"⏰ {timeout_msg}"
            logger.warning(
                "execute_code timed out after %ss (limit %ss) with %d tool calls",
                duration, timeout, tool_call_counter[0],
            )
        elif status == "interrupted":
            result["output"] = stdout_text + "\n[execution interrupted — user sent a new message]"
        elif exit_code != 0:
            result["status"] = "error"
            result["error"] = stderr_text or f"Script exited with code {exit_code}"
            # Include stderr in output so the LLM sees the traceback
            if stderr_text:
                result["output"] = stdout_text + "\n--- stderr ---\n" + stderr_text

        artifact_images, artifact_file = _collect_local_artifacts(
            artifact_dir,
            max_stdout_bytes,
            budget=context.artifact_budget,
        )
        ephemeral_prefix = Path(artifact_dir).resolve().as_uri().rstrip("/") + "/"
        stdout_image_parts = [
            part for part in stdout_image_parts
            if not str(part.get("image_url", {}).get("url", "")).startswith(ephemeral_prefix)
        ]
        _attach_execute_artifacts(
            result,
            [*stdout_image_parts, *artifact_images],
            stdout_artifact_path or artifact_file,
        )
        return json.dumps(result, ensure_ascii=False)

    except Exception as exc:
        duration = round(time.monotonic() - exec_start, 2)
        logger.error(
            "execute_code failed after %ss with %d tool calls: %s: %s",
            duration,
            tool_call_counter[0],
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }, ensure_ascii=False)

    finally:
        # Cleanup temp dir and socket
        if server_sock is not None:
            try:
                server_sock.close()
            except OSError as e:
                logger.debug("Server socket close error: %s", e)
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            # Only UDS has a filesystem socket to unlink; TCP sockets are
            # freed by server_sock.close() above.
            if sock_path:
                os.unlink(sock_path)
        except OSError:
            pass  # already cleaned up or never created


def _kill_process_group(proc, escalate: bool = False):
    """Kill the child and its entire process tree (cross-platform via psutil)."""
    import psutil
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        try:
            parent.terminate()
        except psutil.NoSuchProcess:
            pass
    except psutil.NoSuchProcess:
        pass
    except (PermissionError, OSError) as e:
        logger.debug("Could not terminate process tree: %s", e, exc_info=True)
        try:
            proc.kill()
        except Exception as e2:
            logger.debug("Could not kill process: %s", e2, exc_info=True)

    if escalate:
        # Give the process 5s to exit after SIGTERM, then SIGKILL
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                try:
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            except psutil.NoSuchProcess:
                pass
            except (PermissionError, OSError) as e:
                logger.debug("Could not kill process tree: %s", e, exc_info=True)
                try:
                    proc.kill()
                except Exception as e2:
                    logger.debug("Could not kill process: %s", e2, exc_info=True)


def _load_config() -> dict:
    """Load code_execution config without importing the interactive CLI.

    This helper is called while building the module-level execute_code schema
    during tool discovery.  Importing ``cli`` here pulls prompt_toolkit/Rich and
    a large chunk of the classic REPL onto every agent startup path, including
    ``hermes --tui`` where it is never used.  Read the lightweight raw config
    instead; the config layer already caches by (mtime, size), and an absent
    key cleanly falls back to DEFAULT_EXECUTION_MODE.
    """
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config().get("code_execution", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _config_number(config: dict, key: str, default, *, integer: bool = False) -> Any:
    """Read a positive code_execution number without trusting raw YAML."""
    value = config.get(key, default)
    valid_type = isinstance(value, int if integer else (int, float))
    if isinstance(value, bool) or not valid_type or value <= 0:
        logger.warning("Ignoring invalid code_execution.%s=%r; using %r", key, value, default)
        return default
    return int(value) if integer else value


# ---------------------------------------------------------------------------
# Execution mode resolution (strict vs project)
# ---------------------------------------------------------------------------

# Valid values for code_execution.mode. Kept as a module constant so tests
# and the config layer can reference the canonical set.
EXECUTION_MODES = ("project", "strict")
DEFAULT_EXECUTION_MODE = "project"


def _get_execution_mode() -> str:
    """Return the active execute_code mode — 'project' or 'strict'.

    Reads ``code_execution.mode`` from config.yaml; invalid values fall back
    to ``DEFAULT_EXECUTION_MODE`` ('project') with a log warning.

    Mode semantics:
      - ``project`` (default): scripts run in the session's working directory
        with the active virtual environment's python, so project dependencies
        (pandas, torch, project packages) and files resolve naturally.
      - ``strict``: scripts run in an isolated temp directory with
        ``sys.executable`` (hermes-agent's python). Reproducible and the
        interpreter is guaranteed to work, but project deps and relative paths
        won't resolve.

    Env scrubbing and tool whitelist apply identically in both modes.
    """
    cfg_value = str(_load_config().get("mode", DEFAULT_EXECUTION_MODE)).strip().lower()
    if cfg_value in EXECUTION_MODES:
        return cfg_value
    logger.warning(
        "Ignoring code_execution.mode=%r (expected one of %s), falling back to %r",
        cfg_value, EXECUTION_MODES, DEFAULT_EXECUTION_MODE,
    )
    return DEFAULT_EXECUTION_MODE


@functools.lru_cache(maxsize=32)
def _is_usable_python(python_path: str) -> bool:
    """Check whether a candidate Python interpreter is usable for execute_code.

    Requires Python 3.8+ (f-strings and stdlib modules the RPC stubs need).
    Cached so we don't fork a subprocess on every execute_code call.
    """
    try:
        result = subprocess.run(
            [python_path, "-c",
             "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"],
            timeout=5,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False


def _resolve_child_python(mode: str) -> str:
    """Pick the Python interpreter for the execute_code subprocess.

    In ``strict`` mode, always ``sys.executable`` — guaranteed to work and
    keeps behavior fully reproducible across sessions.

    In ``project`` mode, prefer the user's active virtualenv/conda env's
    python so ``import pandas`` etc. work. Falls back to ``sys.executable``
    if no venv is detected, the candidate binary is missing/not executable,
    or it fails a Python 3.8+ version check.
    """
    if mode != "project":
        return sys.executable

    if _IS_WINDOWS:
        exe_names = ("python.exe", "python3.exe")
        subdirs = ("Scripts",)
    else:
        exe_names = ("python", "python3")
        subdirs = ("bin",)

    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        root = os.environ.get(var, "").strip()
        if not root:
            continue
        for subdir in subdirs:
            for exe in exe_names:
                candidate = os.path.join(root, subdir, exe)
                if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                    continue
                if _is_usable_python(candidate):
                    return candidate
                # Found the interpreter but it failed the version check —
                # log once and fall through to sys.executable.
                logger.info(
                    "execute_code: skipping %s=%s (Python version < 3.8 or broken). "
                    "Using sys.executable instead.", var, candidate,
                )
                return sys.executable

    return sys.executable


def _resolve_child_cwd(mode: str, staging_dir: str) -> str:
    """Resolve the working directory for the execute_code subprocess.

    - ``strict``: the staging tmpdir (today's behavior).
    - ``project``: the session's TERMINAL_CWD (same as the terminal tool), or
      ``os.getcwd()`` if TERMINAL_CWD is unset or doesn't point at a real dir.
      Falls back to the staging tmpdir as a last resort so we never invoke
      Popen with a nonexistent cwd.
    """
    if mode != "project":
        return staging_dir
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        expanded = os.path.expanduser(raw)
        if os.path.isdir(expanded):
            return expanded
    here = os.getcwd()
    if os.path.isdir(here):
        return here
    return staging_dir


# ---------------------------------------------------------------------------
# OpenAI Function-Calling Schema
# ---------------------------------------------------------------------------

# Per-tool documentation lines for the execute_code description.
# Ordered to match the canonical display order.
_TOOL_DOC_LINES = [
    ("web_search",
     "  web_search(query: str, limit: int = 5) -> dict\n"
     "    Returns {\"data\": {\"web\": [{\"url\", \"title\", \"description\"}, ...]}}"),
    ("web_extract",
     "  web_extract(urls: list[str], char_limit: int = None) -> dict\n"
     "    Returns {\"results\": [{\"url\", \"title\", \"content\", \"error\"}, ...]} where content is markdown.\n"
     "    No LLM summarization. Pages over char_limit (default 15000) are head+tail truncated; full text stored on disk (path in the content footer)."),
    ("read_file",
     "  read_file(path: str, offset: int = 1, limit: int = 500) -> dict\n"
     "    Lines are 1-indexed. Returns {\"content\": \"...\", \"total_lines\": N}"),
    ("write_file",
     "  write_file(path: str, content: str) -> dict\n"
     "    Always overwrites the entire file."),
    ("search_files",
     "  search_files(pattern: str, target=\"content\", path=\".\", file_glob=None, limit=50) -> dict\n"
     "    target: \"content\" (search inside files) or \"files\" (find files by name). Returns {\"matches\": [...]}"),
    ("patch",
     "  patch(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict\n"
     "    Replaces old_string with new_string in the file."),
    ("terminal",
     "  terminal(command: str, timeout=None, workdir=None) -> dict\n"
     "    Foreground only (no background/pty). Returns {\"output\": \"...\", \"exit_code\": N}"),
]


def build_execute_code_schema(enabled_sandbox_tools: set = None,
                              mode: str = None) -> dict:
    """Build the execute_code schema with description listing only enabled tools.

    When tools are disabled via ``hermes tools`` (e.g. web is turned off),
    the schema description should NOT mention web_search / web_extract —
    otherwise the model thinks they are available and keeps trying to use them.

    ``mode`` controls the working-directory sentence in the description:
      - ``'strict'``: scripts run in a temp dir (not the session's CWD)
      - ``'project'`` (default): scripts run in the session's CWD with the
        active venv's python
    If ``mode`` is None, the current ``code_execution.mode`` config is read.
    """
    if enabled_sandbox_tools is None:
        enabled_sandbox_tools = SANDBOX_ALLOWED_TOOLS
    if mode is None:
        mode = _get_execution_mode()
    config = _load_config()
    timeout_limit = _config_number(config, "timeout", DEFAULT_TIMEOUT)
    max_tool_calls = _config_number(config, "max_tool_calls", DEFAULT_MAX_TOOL_CALLS, integer=True)
    max_stdout_bytes = _config_number(config, "max_stdout_bytes", MAX_STDOUT_BYTES, integer=True)
    timeout_label = "5-minute timeout" if timeout_limit == DEFAULT_TIMEOUT else f"{timeout_limit:g}-second timeout"
    stdout_label = "50KB stdout cap" if max_stdout_bytes == MAX_STDOUT_BYTES else f"{max_stdout_bytes:,} byte stdout cap"

    # Build tool documentation lines for only the enabled tools
    tool_lines = "\n".join(
        doc for name, doc in _TOOL_DOC_LINES if name in enabled_sandbox_tools
    )

    # Build example import list from enabled tools
    import_examples = [n for n in ("web_search", "terminal") if n in enabled_sandbox_tools]
    if not import_examples:
        import_examples = sorted(enabled_sandbox_tools)[:2]
    if import_examples:
        import_str = ", ".join(import_examples) + ", ..."
    else:
        import_str = "..."

    # Mode-specific CWD guidance. Project mode is the default and matches
    # terminal()'s filesystem/interpreter; strict mode retains the isolated
    # temp-dir staging and hermes-agent's own python.
    if mode == "strict":
        cwd_note = (
            "Scripts run in their own temp dir, not the session's CWD — use absolute paths "
            "(os.path.expanduser('~/.hermes/.env')) or terminal()/read_file() for user files."
        )
    else:
        cwd_note = (
            "Scripts run in the session's working directory with the active venv's python, "
            "so project deps (pandas, etc.) and relative paths work like in terminal()."
        )

    description = (
        "Run a Python script that can call Hermes tools programmatically. "
        "Use this when you need 3+ tool calls with processing logic between them, "
        "need to filter/reduce large tool outputs before they enter your context, "
        "need conditional branching (if X then Y else Z), or need to loop "
        "(fetch N pages, process N files, retry on failure).\n\n"
        "Use normal tool calls instead when: single tool call with no processing, "
        "you need to see the full result and apply complex reasoning, "
        "or the task requires interactive user input.\n\n"
        f"Available via `from hermes_tools import ...`:\n\n"
        f"{tool_lines}\n\n"
        f"Limits: {timeout_label}, {stdout_label}, "
        f"max {max_tool_calls} tool calls per script. "
        "terminal() is foreground-only (no background or pty).\n\n"
        f"{cwd_note}\n\n"
        "Print your final result to stdout. Use Python stdlib (json, re, math, csv, "
        "datetime, collections, etc.) for processing between tool calls.\n\n"
        "Also available (no import needed — built into hermes_tools):\n"
        "  json_parse(text: str) — json.loads with strict=False; use for terminal() output with control chars\n"
        "  shell_quote(s: str) — shlex.quote(); use when interpolating dynamic strings into shell commands\n"
        "  retry(fn, max_attempts=3, delay=2) — retry with exponential backoff for transient failures"
    )

    return {
        "name": "execute_code",
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python code to execute. Import tools with "
                        f"`from hermes_tools import {import_str}` "
                        "and print your final result to stdout."
                    ),
                },
                "persistent": {
                    "type": "boolean",
                    "description": "Opt into a task-scoped persistent Python kernel; omitted or false keeps fresh-process execution.",
                },
                "kernel_id": {
                    "type": "string",
                    "description": "Persistent kernel name within the current task (defaults to the task's default kernel).",
                },
                "session": {
                    "type": "string",
                    "description": "Compatibility alias for kernel_id; supplying it opts into persistence.",
                },
                "reset": {
                    "type": "boolean",
                    "description": "Terminate and clear the selected persistent kernel before running code; omit code to reset only.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Optional execution timeout in seconds; a timed-out kernel is killed and recreated cleanly on the next call.",
                },
                "idle_ttl": {
                    "type": "number",
                    "description": "Optional idle-expiry TTL in seconds for this persistent kernel.",
                },
            },
            "required": [],
        },
    }


# Default schema used at registration time (all sandbox tools listed,
# current configured mode).  model_tools.py rebuilds per-session anyway.
EXECUTE_CODE_SCHEMA = build_execute_code_schema()


# --- Registry ---
from tools.registry import registry, tool_error


def _execute_code_registry_handler(args: dict, **kw):
    """Keep ordinary results as strings and expose validated image envelopes."""
    raw = execute_code(
        code=args.get("code", ""),
        task_id=kw.get("task_id"),
        enabled_tools=kw.get("enabled_tools"),
        session_id=kw.get("session_id"),
        enabled_toolsets=kw.get("enabled_toolsets"),
        disabled_toolsets=kw.get("disabled_toolsets"),
        persistent=args.get("persistent"),
        kernel_id=args.get("kernel_id"),
        session=args.get("session"),
        reset=args.get("reset", False),
        timeout=args.get("timeout"),
        idle_ttl=args.get("idle_ttl"),
    )
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw
        if (
            isinstance(parsed, dict)
            and parsed.get("_multimodal") is True
            and isinstance(parsed.get("content"), list)
        ):
            return parsed
    return raw


registry.register(
    name="execute_code",
    toolset="code_execution",
    schema=EXECUTE_CODE_SCHEMA,
    handler=_execute_code_registry_handler,
    check_fn=check_sandbox_requirements,
    emoji="🐍",
    max_result_size_chars=100_000,
)

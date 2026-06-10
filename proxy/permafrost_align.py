"""permafrost_align — the cache-alignment pipeline.

Everything here is pure: it takes an Anthropic Messages API request body (a dict
parsed from JSON) and returns a normalized body plus a report of what changed.
The HTTP proxy, the tests, and the benchmark all call the same functions, so the
thing we measure is exactly the thing that ships.

The one invariant everything follows from (DeepSeek's words):

    "A subsequent request can only hit the cache if it fully matches a cache
     prefix unit."  Partial matches in the middle do not count — the match is
     anchored at the very first byte.

So the cacheable anchor of every request is `tools` + `system` (they render
first, ahead of the conversation). If those bytes are identical to a prior
request, DeepSeek serves them from its on-disk cache at ~1/50th the price. If a
single byte near the front differs — a reordered tool, today's date baked into
the system prompt, a git-status line that changed since the last turn — the
match breaks there and everything after it is billed at full price.

The pipeline keeps that anchor byte-stable:

  1. strip_cache_control  — DeepSeek ignores Anthropic `cache_control` markers;
                            their shifting positions are pure prefix noise.
  2. sort_tools           — emit tools in a deterministic order so late-binding
                            MCP servers can't reshuffle position 0.
  3. relocate_volatile    — lift volatile env/context blocks (dates, git status,
                            UUIDs, hashes) out of the cached prefix and re-attach
                            them to the tail of the latest turn, where they
                            change nothing upstream of themselves. (aggressive)
  4. canonical_dumps      — serialize with compact, UTF-8-faithful, stable bytes.

`safe` mode runs 1, 2 and 4 (provably lossless re-orderings/serialization).
`aggressive` mode adds 3 (relocation), which moves content within the request
but never drops it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

# --- volatile-content detectors (structural, anchored) ----------------------
# These match the classes of token that change request-to-request and therefore
# poison a cache prefix when they sit in the system block.

_RE_ISO_DATETIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?")
_RE_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_RE_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_RE_HEX = re.compile(r"\b[0-9a-fA-F]{32,64}\b")
_RE_GIT_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")

# Markers that identify a Claude Code "environment / context" block — the part
# of the system prompt that carries the working directory, platform, today's
# date and a snapshot of `git status`. This block is the single biggest cache
# buster CC ships, because git status changes every time you touch a file.
_ENV_MARKERS = (
    "<env>",
    "Working directory",
    "Is directory a git repo",
    "Today's date",
    "Current branch",
    "Recent commits",
    "gitStatus",
    "Platform:",
    "OS Version",
)

_VOLATILE_LABELS = (
    ("iso8601", _RE_ISO_DATETIME),
    ("uuid", _RE_UUID),
    ("hexhash", _RE_HEX),
    ("date", _RE_DATE),
)


@dataclass
class AlignReport:
    """What the pipeline did to one request — surfaced by /permafrost/doctor."""

    mode: str = "aggressive"
    tools_sorted: bool = False
    tools_count: int = 0
    cache_control_stripped: int = 0
    volatile_found: dict[str, int] = field(default_factory=dict)
    metadata_stabilized: int = 0
    blocks_relocated: int = 0
    relocated_chars: int = 0
    env_frozen: bool = False
    env_delta_lines: int = 0
    lineage: str = ""
    anchor_fingerprint: str = ""
    anchor_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "tools_sorted": self.tools_sorted,
            "tools_count": self.tools_count,
            "cache_control_stripped": self.cache_control_stripped,
            "volatile_found": self.volatile_found,
            "metadata_stabilized": self.metadata_stabilized,
            "blocks_relocated": self.blocks_relocated,
            "relocated_chars": self.relocated_chars,
            "env_frozen": self.env_frozen,
            "env_delta_lines": self.env_delta_lines,
            "lineage": self.lineage,
            "anchor_fingerprint": self.anchor_fingerprint,
            "anchor_bytes": self.anchor_bytes,
            "notes": self.notes,
        }


def canonical_dumps(body: dict[str, Any], sort_keys: bool = False) -> bytes:
    """Deterministic, cache-stable serialization.

    Compact separators and `ensure_ascii=False` so the bytes match what a
    well-behaved Anthropic client (Claude Code, the SDK) emits — no spurious
    whitespace, no `\\uXXXX` escapes. Python preserves dict insertion order, so
    as long as we always run the body through here the output is byte-identical
    for byte-identical logical content. `sort_keys` is an extra hammer that also
    neutralizes a client that reorders object keys between turns.
    """
    return json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys
    ).encode("utf-8")


def _count_volatile(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, rx in _VOLATILE_LABELS:
        n = len(rx.findall(text))
        if n:
            counts[label] = counts.get(label, 0) + n
    return counts


def _looks_like_env_block(text: str) -> bool:
    return any(m in text for m in _ENV_MARKERS)


# Claude Code injects a billing-telemetry block as the FIRST system block,
# carrying a per-request `cch=<nonce>` that changes every call. Sitting in the
# cache prefix, that nonce busts the cache for the whole system prompt. Found on
# real CC traffic (the SDK/CLI billing header); see docs/cache-busters.md.
_BILLING_MARKER = "x-anthropic-billing-header"
_RE_CCH = re.compile(r"(cch=)[^;\s]*")


def stabilize_metadata(body: dict[str, Any], report: AlignReport) -> int:
    """Pin per-request nonces in Claude Code's billing-header system block.

    The block is telemetry the model ignores, so replacing its volatile `cch`
    value with a constant is lossless — and it stops a 5-char nonce at the front
    of the prefix from invalidating tens of thousands of cached system tokens.
    """
    system = body.get("system")
    if not isinstance(system, list):
        return 0
    n = 0
    for block in system:
        if isinstance(block, dict):
            text = block.get("text", "")
            if _BILLING_MARKER in text:
                new_text, k = _RE_CCH.subn(r"\1permafrost", text)
                if k:
                    block["text"] = new_text
                    n += k
    report.metadata_stabilized = n
    return n


def strip_cache_control(body: dict[str, Any]) -> int:
    """Remove every `cache_control` key in the body. Returns how many.

    DeepSeek's automatic cache does not read these markers, and Claude Code
    slides them to the most recent turn each request, so their byte positions
    drift. Dropping them removes that drift without changing meaning.
    """
    removed = 0

    def walk(obj: Any) -> Any:
        nonlocal removed
        if isinstance(obj, dict):
            if "cache_control" in obj:
                obj.pop("cache_control", None)
                removed += 1
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        return obj

    walk(body)
    return removed


def _tool_sort_key(tool: dict[str, Any]) -> tuple[str, str]:
    name = (
        str(tool.get("name", ""))
        or str(tool.get("function", {}).get("name", ""))
        or str(tool.get("type", ""))
    )
    try:
        canonical = json.dumps(tool, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except TypeError:
        canonical = str(tool)
    return (name, canonical)


def sort_tools(body: dict[str, Any]) -> bool:
    """Sort `tools` by (name, canonical-json). Returns True if order changed.

    Tools render at position 0 of the prefix, so any reshuffle invalidates the
    entire cache. Claude Code's order is usually stable, but MCP servers that
    finish connecting mid-session, or tool-search deferral toggling, can change
    it. Sorting makes the order independent of arrival timing.
    """
    tools = body.get("tools")
    if not isinstance(tools, list) or len(tools) < 2:
        return False
    before = [_tool_sort_key(t) for t in tools]
    ordered = sorted(tools, key=_tool_sort_key)
    after = [_tool_sort_key(t) for t in ordered]
    if before != after:
        body["tools"] = ordered
        return True
    return False


def _coerce_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize a message `content` (str | list) into a list of blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def relocate_volatile(body: dict[str, Any], report: AlignReport) -> None:
    """Lift volatile system blocks out of the cached prefix.

    Claude Code sends `system` as a list of text blocks. The agent instructions
    are stable; the environment/context block (cwd, platform, today's date, a
    `git status` snapshot) is not. We move any block that both (a) contains a
    volatile token and (b) looks like an env/context block, off the front of the
    prefix and onto the end of the most recent user turn. The content is
    preserved verbatim — the model still sees the date and the git status — it
    just no longer sits ahead of the cached conversation, so it stops resetting
    the cache every time a file changes.
    """
    system = body.get("system")
    if not isinstance(system, list):
        # String system prompts are left intact; relocation needs block bounds.
        if isinstance(system, str):
            v = _count_volatile(system)
            if v and _looks_like_env_block(system):
                report.notes.append(
                    "system is a string with volatile env content; cannot relocate "
                    "safely — set it as a list of blocks, or move the env block to a "
                    "user turn."
                )
        return

    keep: list[Any] = []
    moved: list[dict[str, Any]] = []
    for block in system:
        text = block.get("text", "") if isinstance(block, dict) else ""
        v = _count_volatile(text)
        if v and _looks_like_env_block(text):
            moved.append(block)
            report.relocated_chars += len(text)
        else:
            keep.append(block)

    if not moved:
        return

    body["system"] = keep
    report.blocks_relocated = len(moved)

    # Re-attach the moved blocks to the tail of the last message (a user or
    # tool-result turn during normal CC operation). Wrapped so it is obvious in
    # transcripts where the content came from.
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        # No turn to attach to — fold back rather than drop content.
        body["system"] = keep + moved
        report.blocks_relocated = 0
        report.relocated_chars = 0
        report.notes.append("no message turn to relocate env block onto; left in system")
        return

    last = messages[-1]
    blocks = _coerce_blocks(last.get("content"))
    header = {
        "type": "text",
        "text": "<permafrost:relocated-context>\nMoved out of the cache prefix so it can change "
        "without resetting the cache. Same meaning, later position.\n</permafrost:relocated-context>",
    }
    last["content"] = blocks + [header] + moved
    messages[-1] = last


def _append_context_block(body: dict[str, Any], text: str) -> bool:
    """Append a text block to the tail of the most recent message. Returns True
    on success (there was a message to attach to)."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    last["content"] = _coerce_blocks(last.get("content")) + [{"type": "text", "text": text}]
    messages[-1] = last
    return True


class FreezeStore:
    """Per-session frozen env snapshots for freeze+delta (thread-safe, LRU).

    Keyed on a session fingerprint (the stable, non-env part of the prefix), it
    remembers the first env block it saw so later turns can pin the anchor to
    that snapshot and emit only what changed.
    """

    def __init__(self, cap: int = 512) -> None:
        self.lock = threading.Lock()
        self.snap: dict[str, str] = {}
        self.order: list[str] = []
        self.cap = cap

    def freeze(self, key: str, current_text: str) -> str:
        """Return the frozen snapshot for `key`, setting it to `current_text`
        the first time the key is seen."""
        with self.lock:
            if key not in self.snap:
                self.snap[key] = current_text
                self.order.append(key)
                while len(self.order) > self.cap:
                    self.snap.pop(self.order.pop(0), None)
            else:
                self.order.remove(key)
                self.order.append(key)
            return self.snap[key]


def lineage_key(body: dict[str, Any]) -> str:
    """Fingerprint the *stable* part of the prefix: system minus env-like blocks,
    plus tools. Requests in the same lineage share a cache-anchor ancestry —
    every turn of one conversation, and even separate sessions on the same
    project. Different request types (a tool-less preflight vs. the agent loop)
    get different lineages, so churn diagnostics bucket per lineage instead of
    flagging legitimate type switches as cache busts."""
    system = body.get("system")
    stable: Any
    if isinstance(system, list):
        stable = [
            b for b in system
            if not (isinstance(b, dict) and _looks_like_env_block(b.get("text", "")))
        ]
    else:
        stable = system
    payload = canonical_dumps({"system": stable, "tools": body.get("tools")})
    return hashlib.sha256(payload).hexdigest()[:16]


def extract_session(body: dict[str, Any]) -> str | None:
    """Pull a stable session id out of Claude Code's request metadata.

    CC sends metadata.user_id as a JSON-encoded string containing device_id and
    session_id; fall back to the raw user_id string for other clients."""
    md = body.get("metadata")
    if not isinstance(md, dict):
        return None
    uid = md.get("user_id")
    if not isinstance(uid, str) or not uid:
        return None
    try:
        inner = json.loads(uid)
        if isinstance(inner, dict) and inner.get("session_id"):
            return str(inner["session_id"])[:36]
    except (ValueError, TypeError):
        pass
    return hashlib.sha256(uid.encode()).hexdigest()[:12]


def freeze_volatile(body: dict[str, Any], report: AlignReport, store: "FreezeStore") -> None:
    """Freeze the env block into the anchor; emit only changed lines on the tail.

    The stateful upgrade to `relocate_volatile`: instead of re-sending the whole
    env block off the prefix every turn, we pin the first-seen env snapshot into
    `system` (so it's cached for the rest of the session) and inject just the
    lines that differ from it on the latest turn. An unchanged env costs zero
    tokens per turn; a changed one costs only its delta. Falls back to
    relocation when the request doesn't have exactly one env block.
    """
    system = body.get("system")
    if not isinstance(system, list):
        relocate_volatile(body, report)
        return

    env_ids = [
        i for i, b in enumerate(system)
        if isinstance(b, dict) and _count_volatile(b.get("text", "")) and _looks_like_env_block(b.get("text", ""))
    ]
    if len(env_ids) != 1:
        relocate_volatile(body, report)  # 0 or many env blocks: stateless fallback
        return

    idx = env_ids[0]
    cur_text = system[idx].get("text", "")
    key = lineage_key(body)
    frozen = store.freeze(key, cur_text)

    # Pin the anchor to the frozen snapshot (byte-stable across the session).
    if frozen != cur_text:
        block = dict(system[idx])
        block["text"] = frozen
        system[idx] = block
    report.env_frozen = True

    # Emit only the lines that changed since the snapshot, as a superseding note.
    frozen_lines = set(frozen.splitlines())
    delta = [ln for ln in cur_text.splitlines() if ln.strip() and ln not in frozen_lines]
    if delta:
        note = ("<env-update>\nThese environment values changed since the session "
                "start; use them as current:\n" + "\n".join(delta) + "\n</env-update>")
        if _append_context_block(body, note):
            report.env_delta_lines = len(delta)
        else:
            report.notes.append("env changed but no turn to attach the delta to")


def detect_volatile(body: dict[str, Any]) -> dict[str, int]:
    """Report volatile tokens sitting in the cache anchor (system blocks)."""
    found: dict[str, int] = {}
    system = body.get("system")
    texts: list[str] = []
    if isinstance(system, str):
        texts.append(system)
    elif isinstance(system, list):
        for b in system:
            if isinstance(b, dict):
                texts.append(b.get("text", ""))
    for t in texts:
        for k, v in _count_volatile(t).items():
            found[k] = found.get(k, 0) + v
    return found


def anchor_fingerprint(body: dict[str, Any]) -> tuple[str, int]:
    """Hash of the cacheable anchor (tools + system) after alignment.

    Two requests with the same fingerprint share a byte-identical prefix and
    will hit DeepSeek's cache for the whole anchor. Tracking it across turns is
    how /permafrost/doctor proves the prefix is — or isn't — staying frozen.
    """
    anchor = canonical_dumps(
        {"tools": body.get("tools"), "system": body.get("system")}
    )
    return hashlib.sha256(anchor).hexdigest()[:12], len(anchor)


def align_request(body: dict[str, Any], mode: str = "aggressive",
                  store: "FreezeStore | None" = None) -> tuple[dict[str, Any], AlignReport]:
    """Run the full pipeline. `mode` is "safe" or "aggressive"; "off" is a no-op.

    In aggressive mode, pass a `FreezeStore` to use freeze+delta (pin the env
    snapshot into the cached anchor, emit only changes on the tail). Without a
    store, aggressive mode falls back to stateless relocation. Returns the
    (mutated) body and a report; serialize with `canonical_dumps` to forward.
    """
    report = AlignReport(mode=mode)

    if mode == "off":
        fp, n = anchor_fingerprint(body)
        report.anchor_fingerprint, report.anchor_bytes = fp, n
        report.lineage = lineage_key(body)
        report.tools_count = len(body.get("tools") or [])
        report.volatile_found = detect_volatile(body)
        return body, report

    report.volatile_found = detect_volatile(body)
    report.cache_control_stripped = strip_cache_control(body)
    stabilize_metadata(body, report)
    report.tools_sorted = sort_tools(body)
    report.tools_count = len(body.get("tools") or [])

    if mode == "aggressive":
        if store is not None:
            freeze_volatile(body, report, store)
        else:
            relocate_volatile(body, report)

    fp, n = anchor_fingerprint(body)
    report.anchor_fingerprint, report.anchor_bytes = fp, n
    report.lineage = lineage_key(body)
    return body, report


# --- usage / cache-stat extraction ------------------------------------------
# DeepSeek and Anthropic report cache activity under different field names.
# We fold both shapes into one record, exactly like Reasonix's normaliseUsage.


def normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Fold DeepSeek and Anthropic usage shapes into {input, hit, miss, output}.

    DeepSeek (OpenAI-style): top-level `prompt_cache_hit_tokens` /
        `prompt_cache_miss_tokens`, plus `prompt_tokens` / `completion_tokens`.
    Anthropic-style: `cache_read_input_tokens` / `cache_creation_input_tokens` /
        `input_tokens` / `output_tokens`.
    """
    if not isinstance(usage, dict):
        return {"input": 0, "hit": 0, "miss": 0, "output": 0}

    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    if hit == 0:
        hit = int(usage.get("cache_read_input_tokens") or 0)
    details = usage.get("prompt_tokens_details")
    if hit == 0 and isinstance(details, dict):
        hit = int(details.get("cached_tokens") or 0)

    # "miss" = input tokens NOT served from cache, i.e. billed at full price.
    miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    if miss == 0:
        inp = int(usage.get("input_tokens") or 0)
        created = int(usage.get("cache_creation_input_tokens") or 0)
        if inp or created:
            # Anthropic shape: input_tokens is the uncached remainder and
            # cache_creation is what we wrote to cache this turn — the write
            # costs full price plus a premium, so both count as misses here.
            miss = inp + created
        else:
            prompt = int(usage.get("prompt_tokens") or 0)
            if prompt and hit and prompt > hit:
                miss = prompt - hit  # DeepSeek: prompt_tokens includes the hit
            elif prompt:
                miss = prompt

    output = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {"input": hit + miss, "hit": hit, "miss": miss, "output": output}


def hit_rate(hit: int, miss: int) -> float:
    denom = hit + miss
    return (hit / denom) if denom else 0.0


# Default cost model: DeepSeek V4 Flash, USD per 1M tokens. Override via env.
# Source: DeepSeek pricing (cache-hit input is ~98% cheaper than a miss).
DEFAULT_PRICES = {
    "hit_per_m": 0.0028,
    "miss_per_m": 0.14,
    "output_per_m": 0.28,
}


def cost_usd(hit: int, miss: int, output: int, prices: dict[str, float] | None = None) -> float:
    p = prices or DEFAULT_PRICES
    return (
        hit / 1_000_000 * p["hit_per_m"]
        + miss / 1_000_000 * p["miss_per_m"]
        + output / 1_000_000 * p["output_per_m"]
    )

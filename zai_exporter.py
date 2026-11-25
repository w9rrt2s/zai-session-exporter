#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zai-session-exporter — Comprehensive chat.z.ai session exporter.

Exports EVERY session linked to a chat.z.ai identity into precise, readable
files, capturing literally every action in each session:

  * user inputs (text + attached files)
  * the model's thinking / reasoning before each reply
  * the model's final replies
  * every tool call and tool result (verbatim arguments and payloads)
  * token usage, model names, timestamps, branch (parent/child) structure
  * `raw_original` on every JSONL line: the verbatim API message (parsed JSON,
    key order preserved)

Three output formats (--format):
  jsonl  lossless, one JSON line per action (default)
  md     clean readable Markdown per session
  html   self-contained offline viewer per session (search, collapsible
         thinking, styled tool calls, RTL-safe)

Output layout:
  <output>/sessions/NNN_<title>_<id>.<ext>   one file per session
  <output>/index.jsonl                       one summary line per session
  <output>/merged_all.jsonl                  every line of every session
                                             (jsonl format only)
  <output>/export_summary.json               machine-readable run summary

API contract (reverse-engineered, stable since 2025):
  GET  /api/v1/auths/                      -> current identity (or NEW guest if unauthenticated)
  GET  /api/v1/chats/?page=N               -> list of session summaries (paginated)
  GET  /api/v1/chats/{id}                  -> session metadata + message-id map
  POST /api/v1/chats/{id}/messages/batch   -> {"ids": [...]} => {"data": {id: message}}

Zero third-party dependencies (Python 3.8+ stdlib only).
pip-installable: `pip install .` exposes the `zai-exporter` command and lets
other tools `import zai_exporter`.

MIT License. See LICENSE.
"""

import argparse
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover
    sys.stderr.write("Python 3 with urllib is required.\n")
    raise

__version__ = "1.2.1"
DEFAULT_BASE_URL = "https://chat.z.ai"
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def iso_ts(epoch):
    """Epoch seconds -> ISO-8601 UTC string (None-safe)."""
    if epoch in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def safe_filename(text, max_len=60):
    """Filesystem-safe slug from a session title."""
    text = re.sub(r"[\\/:*?\"<>|\n\r\t]", "_", str(text or "untitled")).strip(" ._")
    text = re.sub(r"\s+", "_", text)
    return text[:max_len] or "untitled"


def eprint(*args):
    print(*args, file=sys.stderr, flush=True)


def force_utf8_stdio():
    """Integration fix: on Windows, redirected stdout/stderr default to legacy
    codecs (cp1252, cp936, ...). Any Arabic or em-dash in a session title would
    then crash a long export at the first print. Force UTF-8 with replacement
    so output can never kill a run."""
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if stream is not None and hasattr(stream, "reconfigure") and enc not in ("utf8", "utf"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def dump_line(prefix, raw_json=None):
    """Serialize one JSONL line.

    Performance fix: `raw_original` (the often-huge API message) used to be
    re-encoded once per emitted event; a message with k blocks cost k full
    encodes of the same object. When `raw_json` — a pre-encoded raw_original
    JSON string — is given, it is spliced in verbatim: byte-identical output,
    exactly one serialization per message instead of one per event.
    """
    if raw_json is None:
        return json.dumps(prefix, ensure_ascii=False)
    slim = {k: v for k, v in prefix.items() if k != "raw_original"}
    return json.dumps(slim, ensure_ascii=False)[:-1] + ', "raw_original": ' + raw_json + "}"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, message, status=None, url=None):
        super().__init__(message)
        self.status = status
        self.url = url


class ZaiClient:
    """Minimal authenticated client for the chat.z.ai web API.

    urllib is used (not raw http.client) so system/company proxies from the
    http_proxy / https_proxy environment variables keep working. The trade-off
    is no connection pooling — see README "Performance notes".
    """

    def __init__(self, base_url, token=None, retries=3, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.retries = max(1, retries)
        self.timeout = timeout

    # -- low level ----------------------------------------------------------

    def _request(self, method, path, body=None, auth=True):
        url = self.base_url + path
        headers = {"Accept": "application/json", "User-Agent": "zai-session-exporter/" + __version__}
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                req = Request(url, data=data, headers=headers, method=method)
                with urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    return raw.decode("utf-8", "replace")
            except HTTPError as err:
                payload = ""
                try:
                    payload = err.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                last_err = ApiError(
                    "HTTP %s on %s (%s)" % (err.code, path, payload or err.reason),
                    status=err.code, url=url)
                # Retry only on transient statuses.
                if err.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(min(30.0, 0.5 * (2 ** (attempt - 1))))
                    continue
                raise last_err
            except (URLError, OSError) as err:
                last_err = ApiError("Network error on %s: %s" % (path, err), url=url)
                if attempt < self.retries:
                    time.sleep(min(30.0, 0.5 * (2 ** (attempt - 1))))
                    continue
                raise last_err
        raise last_err or ApiError("Request failed: " + path, url=url)

    # -- identity -----------------------------------------------------------

    def whoami(self):
        """GET /api/v1/auths/ — with a token returns the owning identity."""
        return self._request("GET", "/api/v1/auths/", auth=bool(self.token))

    def create_guest_session(self):
        """GET /api/v1/auths/ WITHOUT auth — server provisions a brand-new guest."""
        return self._request("GET", "/api/v1/auths/", auth=False)

    # -- sessions -----------------------------------------------------------

    def list_chats_page(self, page, chat_type):
        return self._request(
            "GET", "/api/v1/chats/?page=%d&type=%s" % (page, chat_type))

    def get_chat_details(self, chat_id):
        return self._request("GET", "/api/v1/chats/" + str(chat_id))

    def fetch_messages_batch(self, chat_id, ids, batch_size=50, progress_cb=None):
        """POST /chats/{id}/messages/batch — chunked; returns (messages_map, complete, problems)."""
        messages, problems = {}, []
        complete = True
        ids = [str(i) for i in ids if i is not None]
        chunks = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
        for ci, chunk in enumerate(chunks, 1):
            if progress_cb:
                progress_cb(ci, len(chunks))
            data = self._request(
                "POST", "/api/v1/chats/%s/messages/batch" % chat_id,
                body={"ids": chunk})
            # Observed shape: {"data": {id: message}}. Be tolerant of a bare map.
            payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
            if not isinstance(payload, dict):
                problems.append("batch %d returned unexpected payload type %s" % (ci, type(payload).__name__))
                complete = False
                continue
            for mid, msg in payload.items():
                messages[str(mid)] = msg
            chunk_set = set(chunk)
            returned = set(str(k) for k in payload.keys())
            missing = chunk_set - returned
            if missing:
                complete = False
                shown = sorted(missing)[:10]
                extra = len(missing) - len(shown)
                problems.append("batch %d missing ids: %s%s" % (ci, ", ".join(shown),
                                                                " (+%d more)" % extra if extra else ""))
        return messages, complete, problems


# ---------------------------------------------------------------------------
# Auth resolution
# ---------------------------------------------------------------------------

def resolve_token(args):
    """Decide the Bearer token. Returns (token, token_source)."""
    if args.token:
        return args.token.strip(), "--token argument"
    env = os.environ.get("ZAI_TOKEN", "").strip()
    if env:
        return env, "ZAI_TOKEN environment variable"
    token_file = Path(args.token_file)
    if token_file.is_file():
        content = token_file.read_text(encoding="utf-8").strip()
        if content:
            return content, "token file: %s" % token_file
    return None, None


def connect(client, args):
    """Attach an identity to the client. Returns identity dict + description."""
    token, source = resolve_token(args)
    if token:
        client.token = token
        ident = client.whoami()
        if not isinstance(ident, dict) or "id" not in ident:
            raise ApiError("Token rejected by server (unexpected response from /api/v1/auths/).")
        return ident, "existing session via %s" % source
    # No token anywhere: provision a brand-new guest identity (free mode).
    ident = client.create_guest_session()
    if not isinstance(ident, dict) or not ident.get("token"):
        raise ApiError("Could not provision a guest session (unexpected server response).")
    client.token = ident["token"]
    return ident, ("fresh GUEST session auto-provisioned (free mode) — "
                   "note: this is a NEW empty identity; existing sessions need the "
                   "token stored in your browser's localStorage (see README)")


# ---------------------------------------------------------------------------
# Session listing
# ---------------------------------------------------------------------------

def list_all_chats(client, chat_type, quiet=False):
    """Walk all pages of GET /chats/ and return summaries in API order.

    Duplicate session ids (a server pagination bug would otherwise export the
    same session twice under two ordinals) are de-duplicated with a warning.
    """
    summaries, page = [], 1
    seen_ids = set()
    while True:
        data = client.list_chats_page(page, chat_type)
        if isinstance(data, dict):  # tolerate {"chats": [...]} wrappers
            for key in ("chats", "items", "data"):
                if key in data:
                    data = data[key]
                    break
            else:
                raise ApiError("Unknown chat-list payload shape on page %d (top-level keys: %s)"
                               % (page, ", ".join(sorted(data.keys())) or "none"))
        if not isinstance(data, list):
            raise ApiError("Unexpected chat-list payload on page %d: %s" % (page, type(data).__name__))
        if not data:
            break
        for c in data:
            cid = str(c.get("id"))
            if cid in seen_ids:
                if not quiet:
                    eprint("  duplicate session id %s in listing skipped" % cid)
                continue
            seen_ids.add(cid)
            summaries.append(c)
        if not quiet:
            eprint("  listed page %d (%d sessions so far)" % (page, len(summaries)))
        page += 1
        if page > 10000:  # absurd hard cap; API pagination always ends earlier
            break
    return summaries


def apply_scope(summaries, args):
    """Apply --search/--from/--to/--last filters (API order is newest-first).

    Filter order matters: search and dates narrow the pool, and only then does
    --last N take the N most recent *matching* sessions.
    """
    out = summaries
    search = getattr(args, "search", None)
    if search:
        needle = search.lower()
        out = [c for c in out if needle in str(c.get("title") or "").lower()]
    if args.from_date or args.to_date:
        def within(c):
            ts = iso_ts(c.get("updated_at") or c.get("created_at"))
            if not ts:
                return False
            day = ts[:10]
            if args.from_date and day < args.from_date:
                return False
            if args.to_date and day > args.to_date:
                return False
            return True
        out = [c for c in out if within(c)]
    if args.last is not None:
        out = out[:args.last]  # API order is newest-first
    return out


# ---------------------------------------------------------------------------
# Message -> JSONL events (the heart of the tool)
# ---------------------------------------------------------------------------

TOOL_CALL_TYPES = {"tool_use", "tool_call", "function_call", "function"}
TOOL_RESULT_TYPES = {"tool_result", "tool_response", "function_call_output"}
THINKING_TYPES = {"reasoning", "thinking", "thought", "reflection", "plan"}
MEDIA_TYPES = {"image", "image_url", "file", "audio", "video"}
META_TYPES = {"system", "error", "metadata"}

EVENT_TYPES = ("user_input", "attachment", "thinking", "model_reply",
               "tool_call", "tool_result", "other", "missing_message")


def block_text(block):
    """Read a content block's textual payload, tolerating several field shapes."""
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    for key in ("content", "text", "value", "data"):
        if key in block:
            v = block[key]
            if isinstance(v, str):
                return v
            if isinstance(v, list):
                parts = []
                for item in v:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        sub = block_text(item)
                        if sub:
                            parts.append(sub)
                return "\n".join(p for p in parts if p)
            if isinstance(v, dict):
                for sub_key in ("text", "content"):
                    if isinstance(v.get(sub_key), str):
                        return v[sub_key]
    return ""


def _norm_text(s):
    """Whitespace-normalized text, used only for duplicate detection."""
    return " ".join((s or "").split())


def iso_or_none(value):
    """Accept epoch seconds OR an existing ISO string; normalize to ISO-8601."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return iso_ts(value)
    s = str(value)
    if s.replace(".", "", 1).isdigit():
        return iso_ts(float(s))
    return s


def extract_events(msg, message_seq):
    """Split one raw API message into ordered, typed JSONL events.

    Returns a list of dicts WITHOUT session-level bookkeeping fields
    (session_id / event_seq are added by the caller).
    Every returned event embeds `raw_original`: the full, untouched message.
    """
    events = []
    if not isinstance(msg, dict):
        return [{
            "event": "missing_message", "role": None, "text": None,
            "message_seq": message_seq, "message_id": None,
            "parent_id": None, "children_ids": None,
            "timestamp": None, "timestamp_epoch": None,
            "model": None, "usage": None, "files": None, "tool": None,
            "raw_original": msg if isinstance(msg, (dict, list, str, int, float, bool)) or msg is None else str(msg),
        }]

    mid = msg.get("id")
    role = msg.get("role")
    epoch = msg.get("timestamp")
    timestamp = iso_or_none(epoch)
    model = msg.get("model") or msg.get("model_id")
    usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
    files = msg.get("files") if isinstance(msg.get("files"), list) else None
    parent_id = msg.get("parent_id")
    children_ids = msg.get("children_ids")

    base = {
        "message_seq": message_seq,
        "message_id": mid,
        "parent_id": parent_id,
        "children_ids": children_ids,
        "role": role,
        "timestamp": timestamp,
        "timestamp_epoch": epoch,
        "model": model,
        "usage": usage,
    }

    blocks = msg.get("content_blocks")
    blocks = blocks if isinstance(blocks, list) else None

    def emit(event, text, tool=None, raw_block=None, files_ev=None):
        ev = dict(base)
        ev.update({
            "event": event,
            "text": text if (isinstance(text, str) and text != "") else None,
            "tool": tool,
            "files": files_ev,
            "raw_block": raw_block,
            "raw_original": msg,   # verbatim full message (parsed JSON)
        })
        events.append(ev)

    # --- 1. user attachments ---------------------------------------------
    if files:
        for f in files:
            emit("attachment", (f.get("name") or f.get("filename")) if isinstance(f, dict) else None,
                 files_ev=f)

    # --- 2. message-level tool calls (OpenAI-style msg.tool_calls) --------
    level_tool_calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else None
    if level_tool_calls:
        for tc in level_tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            emit("tool_call", block_text(fn.get("arguments")),
                 tool={
                     "id": tc.get("id"),
                     "name": fn.get("name") or tc.get("name"),
                     "arguments": fn.get("arguments") if "arguments" in fn else tc.get("input"),
                     "type": tc.get("type"),
                 },
                 raw_block=tc)

    # --- 3. content_blocks: thinking / tool calls / tool results / text --
    if blocks is not None:
        seen_reasoning_parts = []
        seen_texts = []
        for block in blocks:
            if not isinstance(block, dict):
                if isinstance(block, str) and block.strip():
                    emit("model_reply" if role == "assistant" else "user_input", block)
                continue
            btype = str(block.get("type") or "").lower()

            if btype in THINKING_TYPES:
                seen_reasoning_parts.append(_norm_text(block_text(block)))
                emit("thinking", block_text(block), raw_block=block)
            elif btype in TOOL_CALL_TYPES:
                fn = block.get("function") if isinstance(block.get("function"), dict) else {}
                emit("tool_call", block_text(block),
                     tool={
                         "id": block.get("id"),
                         "name": fn.get("name") or block.get("name") or block.get("tool_name"),
                         "arguments": (block.get("input") if "input" in block
                                       else block.get("arguments") if "arguments" in block
                                       else block.get("args") if "args" in block
                                       else fn.get("arguments") if fn.get("arguments") is not None
                                       else block.get("content")),
                         "type": block.get("tool_type") or block.get("type"),
                     },
                     raw_block=block)
            elif btype in TOOL_RESULT_TYPES:
                emit("tool_result", block_text(block),
                     tool={
                         "id": block.get("tool_call_id") or block.get("id"),
                         "name": block.get("name") or block.get("tool_name"),
                         "status": block.get("status") if block.get("status") is not None
                                   else ("error" if block.get("is_error") else None),
                         "type": block.get("type"),
                     },
                     raw_block=block)
            elif btype in MEDIA_TYPES:
                emit("attachment", block.get("name") or block.get("file_name"), raw_block=block)
            elif btype in META_TYPES:
                emit("other", block_text(block), raw_block=block)
            else:
                # Unknown or plain text block -> answer text.
                text = block_text(block)
                if text:
                    seen_texts.append(text)
                emit("model_reply" if role == "assistant" else ("user_input" if role == "user" else "other"),
                     text, raw_block=block)

        # Top-level `reasoning` string that content_blocks didn't already cover.
        top_reasoning = msg.get("reasoning") or msg.get("reasoning_content")
        if isinstance(top_reasoning, str) and top_reasoning.strip() \
                and _norm_text(top_reasoning) not in seen_reasoning_parts:
            emit("thinking", top_reasoning, raw_block=None)

        # A plain string `content` alongside content_blocks. Emitted only when
        # it is NOT already covered by an answer-text block above (dedup), so
        # nothing appears twice — everything stays available in raw_original.
        content = msg.get("content")
        if isinstance(content, str) and content.strip() \
                and _norm_text(content) not in (_norm_text(t) for t in seen_texts):
            emit("model_reply" if role == "assistant" else ("user_input" if role == "user" else "other"),
                 content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str) and part.strip():
                    emit("model_reply" if role == "assistant" else ("user_input" if role == "user" else "other"),
                         part)
                elif isinstance(part, dict):
                    ptype = str(part.get("type") or "").lower()
                    if ptype in THINKING_TYPES:
                        emit("thinking", block_text(part), raw_block=part)
                    elif ptype in TOOL_CALL_TYPES:
                        pfn = part.get("function") if isinstance(part.get("function"), dict) else {}
                        emit("tool_call", block_text(part),
                             tool={"id": part.get("id"),
                                   "name": pfn.get("name") or part.get("name"),
                                   "arguments": part.get("input") if "input" in part else part.get("arguments") if "arguments" in part else pfn.get("arguments"),
                                   "type": part.get("type")},
                             raw_block=part)
                    elif ptype in TOOL_RESULT_TYPES:
                        emit("tool_result", block_text(part),
                             tool={"id": part.get("tool_call_id") or part.get("id"),
                                   "name": part.get("name"),
                                   "type": part.get("type")},
                             raw_block=part)
                    elif ptype in MEDIA_TYPES:
                        emit("attachment", part.get("name") or part.get("file_name"), raw_block=part)
                    else:
                        emit("model_reply" if role == "assistant" else ("user_input" if role == "user" else "other"),
                             block_text(part), raw_block=part)

    # --- 4. no content_blocks: plain content ------------------------------
    else:
        content = msg.get("content")
        text = content if isinstance(content, str) else block_text(content)
        # OpenAI-style standalone tool-result message: role="tool" + tool_call_id.
        if role == "tool" or msg.get("tool_call_id"):
            emit("tool_result", text,
                 tool={"id": msg.get("tool_call_id"),
                       "name": msg.get("name"),
                       "status": None,
                       "type": "tool_result"})
            return events
        if role == "assistant" and isinstance(top_level := (msg.get("reasoning") or msg.get("reasoning_content")), str) and top_level.strip():
            emit("thinking", top_level)
        if isinstance(text, str) and text.strip():
            emit("model_reply" if role == "assistant" else ("user_input" if role == "user" else "other"), text)
        elif blocks is None and not level_tool_calls and not files:
            # Message with no recoverable payload at all: still record it.
            emit("other", None)

    return events


# ---------------------------------------------------------------------------
# Message ordering (canonical + readable tree order)
# ---------------------------------------------------------------------------

def canonical_ids(detail):
    """Message-id order as stored server-side (dict insertion order of history map)."""
    chat = detail.get("chat") if isinstance(detail, dict) else None
    ids = []
    if isinstance(chat, dict):
        hist = chat.get("history")
        if isinstance(hist, dict) and isinstance(hist.get("messages"), dict):
            ids = list(hist["messages"].keys())
        elif isinstance(hist, dict) and isinstance(hist.get("messages"), list):
            for m in hist["messages"]:
                ids.append(str(m.get("id") if isinstance(m, dict) else m))
    if not ids and isinstance(detail, dict):
        for alt in ("messages",):
            v = detail.get(alt)
            if isinstance(v, list):
                for m in v:
                    ids.append(str(m.get("id") if isinstance(m, dict) else m))
    # de-duplicate, preserve order
    seen, out = set(), []
    for i in ids:
        s = str(i)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def readable_order(messages_by_id):
    """Depth-first parent->children traversal; timestamp fallback ordering."""
    ids = list(messages_by_id.keys())
    children_of, parent_of = {}, {}
    for mid, m in messages_by_id.items():
        pid = m.get("parent_id") if isinstance(m, dict) else None
        if pid is not None and str(pid) in messages_by_id:
            parent_of[mid] = str(pid)
            children_of.setdefault(str(pid), []).append(mid)

    pos_of = {mid: i for i, mid in enumerate(ids)}  # O(1) lookup (ids.index() was O(n^2) overall)

    def sort_key(mid):
        m = messages_by_id.get(mid) or {}
        ts = m.get("timestamp") if isinstance(m, dict) else None
        try:
            t = float(ts) if ts is not None else float("inf")
        except (TypeError, ValueError):
            t = float("inf")
        return (t, pos_of[mid])

    for kids in children_of.values():
        kids.sort(key=sort_key)

    ordered, visited = [], set()
    def walk(start):
        # v1.2.1 performance/robustness fix: the recursive version crashed with
        # RecursionError on linear chains deeper than the interpreter limit
        # (~1000 messages — real long sessions exceed that). Explicit-stack
        # DFS, byte-identical pre-order output.
        stack = [start]
        while stack:
            mid = stack.pop()
            if mid in visited:
                continue
            visited.add(mid)
            ordered.append(mid)
            for kid in reversed(children_of.get(mid, [])):
                stack.append(kid)

    roots = [mid for mid in ids if mid not in parent_of]
    for r in sorted(roots, key=sort_key):
        walk(r)
    for mid in ids:            # anything unreachable (broken links) keeps canonical order
        walk(mid)
    # Correctness fix (v1.2.0): the old formula len(ids) - len(set(parent_of.values()))
    # returned the number of LEAVES, not roots (wrong whenever a parent has 2+
    # children). Roots = nodes with no parent in the map.
    root_count = sum(1 for mid in ids if mid not in parent_of)
    return ordered, root_count


# ---------------------------------------------------------------------------
# Markdown + HTML renderers (--format md / --format html)
# ---------------------------------------------------------------------------

_FENCE_RUN = re.compile(r"`{3,}")


def _md_fence(text):
    """A code fence guaranteed not to collide with backticks inside `text`."""
    n = 3
    for run in _FENCE_RUN.findall(text or ""):
        n = max(n, len(run) + 1)
    return "`" * n


def _md_meta(ev):
    bits = []
    if ev.get("model"):
        bits.append(str(ev["model"]))
    if ev.get("timestamp"):
        bits.append(str(ev["timestamp"]))
    return " · ".join(bits)


def md_session_header(s):
    lines = [
        "# " + str(s.get("title") or "Untitled"),
        "",
        "- Session ID: `%s`" % s.get("session_id"),
        "- Chat type: %s" % s.get("chat_type"),
        "- Created: %s" % (s.get("created_at") or "?"),
        "- Updated: %s" % (s.get("updated_at") or "?"),
        "- Messages advertised: %s" % s.get("message_count"),
        "- Export complete: %s" % ("yes" if s.get("export_complete") else "NO — some payloads missing"),
        "- Exported by: %s" % s.get("tool"),
    ]
    if s.get("problems"):
        lines.append("- Problems: " + "; ".join(str(p) for p in s["problems"]))
    lines += ["", "---", ""]
    return "\n".join(lines)


def md_event(ev):
    etype = ev.get("event")
    head = "### [%s] %s" % (ev.get("display_seq"), etype)
    if ev.get("role"):
        head += " — %s" % ev["role"]
    out = [head]
    meta = _md_meta(ev)
    if meta:
        out += ["", "*%s*" % meta]
    out.append("")

    tool = ev.get("tool") if isinstance(ev.get("tool"), dict) else None

    if etype == "attachment":
        f = ev.get("files") if isinstance(ev.get("files"), dict) else None
        if f:
            name = f.get("name") or f.get("filename") or "file"
            size = f.get("size")
            out.append("- File: **%s**%s" % (name, " (%s bytes)" % size if size else ""))
        else:
            out.append("- Media attachment")
        out.append("")
    elif etype == "missing_message":
        out.append("_(message payload missing on the server — exported as a placeholder "
                   "so nothing silently disappears)_")
        out.append("")
    elif etype == "tool_call":
        label = (tool or {}).get("name") or "tool"
        out.append("**%s**" % label)
        out.append("")
        args = (tool or {}).get("arguments")
        body = None
        if args is not None:
            body = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, indent=2)
        elif ev.get("text"):
            body = ev["text"]
        if body:
            fence = _md_fence(body)
            out += [fence, body, fence, ""]
    elif etype == "tool_result":
        label = (tool or {}).get("name") or "tool"
        status = (tool or {}).get("status")
        if status:
            label += " (status: %s)" % status
        out.append("**%s**" % label)
        out.append("")
        text = ev.get("text")
        if text:
            fence = _md_fence(text)
            out += [fence, text, fence, ""]
    elif etype == "thinking" and ev.get("text"):
        out += ["> " + ln for ln in str(ev["text"]).splitlines()]
        out.append("")
    else:
        text = ev.get("text")
        if text:
            out += [str(text), ""]
        else:
            out += ["_(no textual payload)_", ""]
    return "\n".join(out) + "\n"


def md_session_footer(end_line):
    counts = end_line.get("event_counts") or {}
    summary = ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())) or "no events"
    return "\n".join([
        "---", "",
        "*%d events (%s). Export complete: %s*" % (
            end_line.get("event_count", 0), summary,
            "yes" if end_line.get("export_complete") else "NO"),
        "",
    ])


def _esc(v):
    return html.escape(str(v)) if v is not None else ""


def _html_card(ev):
    etype = ev.get("event") or "other"
    who = []
    if ev.get("role"):
        who.append(str(ev["role"]))
    if ev.get("model"):
        who.append(str(ev["model"]))
    if ev.get("timestamp"):
        who.append(str(ev["timestamp"]))
    tag = '<span class="tag t-%s">%s</span>' % (_esc(etype), _esc(etype.replace("_", " ")))
    seq = '<span class="seq">#%s</span>' % _esc(ev.get("display_seq"))
    whos = '<span class="who">%s</span>' % _esc(" · ".join(who))
    head = '<div class="hd">%s %s%s</div>' % (tag, seq, whos)

    tool = ev.get("tool") if isinstance(ev.get("tool"), dict) else None
    text = ev.get("text")
    body = ""
    if etype == "thinking" and text:
        body = ('<details open><summary>Thinking</summary>'
                '<div class="body" dir="auto">%s</div></details>' % _esc(text))
    elif etype in ("tool_call", "tool_result"):
        status = (tool or {}).get("status")
        st = ""
        if status:
            cls = "status error" if status == "error" else "status"
            st = '<span class="%s">%s</span>' % (cls, _esc(status))
        label = '<span class="toolname">%s</span>%s' % (_esc((tool or {}).get("name") or "tool"), st)
        inner = ""
        if etype == "tool_call":
            args = (tool or {}).get("arguments")
            payload = None
            if args is not None:
                payload = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, indent=2)
            if not payload and text:
                payload = text
            if payload:
                inner = '<pre class="body" dir="auto">%s</pre>' % _esc(payload)
        elif text:
            inner = '<pre class="body" dir="auto">%s</pre>' % _esc(text)
        body = '<details open class="tool"><summary>%s</summary>%s</details>' % (label, inner)
    elif etype == "attachment":
        f = ev.get("files") if isinstance(ev.get("files"), dict) else None
        nm = (f or {}).get("name") or (f or {}).get("filename") or (text or "attachment")
        size = (f or {}).get("size")
        suffix = " · %s bytes" % size if size else ""
        body = '<div class="body" dir="auto">File: <b>%s</b>%s</div>' % (_esc(nm), _esc(suffix))
    elif etype == "missing_message":
        body = ('<div class="body missing" dir="auto">message payload missing on the server — '
                'placeholder exported so nothing silently disappears</div>')
    elif text:
        body = '<div class="body" dir="auto">%s</div>' % _esc(text)
    else:
        body = '<div class="body nopayload">(no textual payload)</div>'
    return '<div class="ev" data-t="%s">%s%s</div>' % (_esc(etype), head, body)


_HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — zai session</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mut:#68718a;--line:#e3e6ee}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans",sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:18px 22px;position:sticky;top:0;z-index:2}
h1{margin:0 0 4px;font-size:19px}
.meta{color:var(--mut);font-size:12.5px}
.meta b{color:var(--ink)}
#search{margin-top:10px;width:min(560px,100%);padding:8px 12px;border:1px solid var(--line);border-radius:8px;font:inherit}
main{max-width:900px;margin:18px auto 60px;padding:0 14px}
.ev{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:10px 0}
.ev.hidden{display:none}
.hd{line-height:1.9}
.tag{display:inline-block;font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;border-radius:5px;padding:2px 8px;margin-right:8px;color:#fff}
.t-user_input{background:#2563eb}.t-model_reply{background:#059669}.t-thinking{background:#7c3aed}
.t-tool_call{background:#0f766e}.t-tool_result{background:#334155}.t-attachment{background:#b45309}
.t-other{background:#64748b}.t-missing_message{background:#dc2626}
.seq{color:var(--mut);font-size:12px}
.who{font-size:12.5px;color:var(--mut);margin-left:8px}
.body{margin-top:8px;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
pre.body{font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#0f172a;color:#e2e8f0;border-radius:8px;padding:10px 12px;overflow:auto;margin:8px 0 0}
details{margin-top:8px}
summary{cursor:pointer;color:#7c3aed;font-size:13px;font-weight:600}
details.tool summary{color:#0f766e}
.toolname{font-weight:600;color:#0f766e}
.status{font-size:11px;border-radius:4px;padding:1px 6px;margin-left:6px;background:#e2e8f0;color:#334155}
.status.error{background:#fee2e2;color:#b91c1c}
.missing{color:#b45309;font-style:italic}
.nopayload{color:var(--mut);font-style:italic}
footer{max-width:900px;margin:0 auto 40px;padding:0 14px;color:var(--mut);font-size:12.5px}
</style>
</head>
<body>
<header>
<h1>__TITLE__</h1>
<div class="meta">__META__</div>
<input id="search" type="search" placeholder="Filter events by text..." aria-label="Filter events">
</header>
<main id="stream">
__CARDS__
</main>
<footer>__FOOTER__</footer>
<script>
(function(){
  var q=document.getElementById('search');
  q.addEventListener('input',function(){var v=q.value.toLowerCase();
    var list=document.querySelectorAll('.ev');
    for(var i=0;i<list.length;i++){var el=list[i];
      el.classList.toggle('hidden',!!v&&el.textContent.toLowerCase().indexOf(v)===-1);}
  });
})();
</script>
</body>
</html>
"""


def _html_page(session_line, cards, end_line):
    meta = [
        "Session <b>%s</b>" % _esc(session_line.get("session_id")),
        "type <b>%s</b>" % _esc(session_line.get("chat_type")),
        "created <b>%s</b>" % _esc(session_line.get("created_at") or "?"),
        "updated <b>%s</b>" % _esc(session_line.get("updated_at") or "?"),
        "messages <b>%s</b>" % _esc(session_line.get("message_count")),
    ]
    ok = bool(session_line.get("export_complete"))
    meta.append('complete <b style="color:%s">%s</b>' % ("#059669" if ok else "#dc2626",
                                                         "yes" if ok else "NO - payloads missing"))
    counts = end_line.get("event_counts") or {}
    foot = ["zai-session-exporter", "%d events" % end_line.get("event_count", 0)]
    foot += ["%s: %d" % (k, v) for k, v in sorted(counts.items())]
    footer = _esc(" · ".join(foot))
    if session_line.get("problems"):
        footer += '<br>problems: %s' % _esc("; ".join(str(p) for p in session_line["problems"]))
    return (_HTML_PAGE
            .replace("__TITLE__", _esc(session_line.get("title") or "Untitled"))
            .replace("__META__", " · ".join(meta))
            .replace("__CARDS__", "\n".join(cards))
            .replace("__FOOTER__", footer))


# ---------------------------------------------------------------------------
# Export pipeline
# ---------------------------------------------------------------------------

def export_session(client, summary, out_dir, ordinal, args, account_label, merged_fh=None):
    """Fetch + convert one session, streaming lines to disk as they are built.

    Returns the index record. Raises ApiError on unrecoverable fetch failure.
    When `merged_fh` is given (single-worker jsonl mode), every JSONL line is
    written to BOTH the session file and the merged stream in the same pass —
    no second read of the session file afterwards.
    """
    chat_id = str(summary.get("id"))
    title = summary.get("title") or "Untitled"
    fmt = args.format
    ext = "jsonl" if fmt == "jsonl" else ("md" if fmt == "md" else "html")
    fname = "%03d_%s_%s.%s" % (ordinal, safe_filename(title), chat_id[:8], ext)
    path = out_dir / "sessions" / fname

    detail = client.get_chat_details(chat_id)
    if not isinstance(detail, dict):
        raise ApiError("Unexpected detail payload for session %s" % chat_id)

    ids = canonical_ids(detail)

    messages_map = {}
    complete, problems = True, []
    if ids:
        def batch_progress(ci, n):
            if not args.quiet and n > 1:
                eprint("    messages batch %d/%d" % (ci, n))
        messages_map, complete, problems = client.fetch_messages_batch(
            chat_id, ids, batch_size=args.batch_size, progress_cb=batch_progress)
    else:
        complete = False
        problems.append("no message ids found in session detail (history map missing or empty)")

    order, _roots = readable_order(messages_map)
    # Display order = readable tree order; keep canonical seq as message_seq anchor.
    canon_pos = {mid: i + 1 for i, mid in enumerate(ids)}
    # Ids the server never returned are appended (canonical order) as
    # missing_message events, so nothing silently disappears from the export.
    missing_ids = [mid for mid in ids if mid not in messages_map]
    display_ids = (order or [mid for mid in ids if mid in messages_map]) + missing_ids

    created = iso_or_none(detail.get("created_at") or summary.get("created_at"))
    updated = iso_or_none(detail.get("updated_at") or summary.get("updated_at"))
    session_line = {
        "record_type": "session",
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tool": "zai-session-exporter " + __version__,
        "account": account_label,
        "session_id": chat_id,
        "title": detail.get("title") or title,
        "chat_type": detail.get("type") or summary.get("type") or "default",
        "created_at": created,
        "updated_at": updated,
        "message_count": len(ids),
        "export_complete": complete,
        "problems": problems,
        "session_meta_raw": {k: v for k, v in summary.items() if k not in ("id", "title")},
    }

    events_set = getattr(args, "events_set", None)
    counts = {}
    event_seq = 0
    raw_cache = {}  # per-session: message id -> pre-serialized raw_original JSON

    def raw_json_for(mid, raw):
        """Encode `raw` exactly once per message (cache by message id)."""
        if raw is None:
            return None
        if not isinstance(raw, dict):
            return json.dumps(raw, ensure_ascii=False)
        key = str(mid)
        cached = raw_cache.get(key)
        if cached is None:
            cached = json.dumps(raw, ensure_ascii=False)
            raw_cache[key] = cached
        return cached

    path.parent.mkdir(parents=True, exist_ok=True)
    html_cards = None
    with path.open("w", encoding="utf-8") as fh:
        def wjson(prefix, rj):
            line = dump_line(prefix, rj) + "\n"
            fh.write(line)
            if merged_fh is not None:
                merged_fh.write(line)

        if fmt == "jsonl":
            wjson(session_line, json.dumps(detail, ensure_ascii=False))
        elif fmt == "md":
            fh.write(md_session_header(session_line))
        else:
            html_cards = []

        for display_i, mid in enumerate(display_ids, 1):
            msg = messages_map.get(mid)
            if msg is None:
                evs = [{
                    "event": "missing_message", "role": None, "text": None,
                    "message_seq": canon_pos.get(mid), "message_id": mid,
                    "parent_id": None, "children_ids": None,
                    "timestamp": None, "timestamp_epoch": None,
                    "model": None, "usage": None, "files": None, "tool": None,
                    "raw_block": None, "raw_original": None,
                }]
            else:
                evs = extract_events(msg, canon_pos.get(mid))
            for ev in evs:
                if events_set and ev["event"] not in events_set:
                    continue
                event_seq += 1
                counts[ev["event"]] = counts.get(ev["event"], 0) + 1
                prefix = {
                    "record_type": "event",
                    "schema_version": SCHEMA_VERSION,
                    "session_id": chat_id,
                    "event_seq": event_seq,
                    "display_seq": display_i,
                }
                prefix.update({k: v for k, v in ev.items() if k != "raw_original"})
                if fmt == "jsonl":
                    wjson(prefix, raw_json_for(mid, ev.get("raw_original")))
                elif fmt == "md":
                    fh.write(md_event(prefix))
                else:
                    html_cards.append(_html_card(prefix))

        end_line = {
            "record_type": "session_end",
            "schema_version": SCHEMA_VERSION,
            "session_id": chat_id,
            "event_count": event_seq,
            "event_counts": counts,
            "export_complete": complete,
            "problems": problems,
        }
        if fmt == "jsonl":
            wjson(end_line, None)
        elif fmt == "md":
            fh.write(md_session_footer(end_line))
        else:
            fh.write(_html_page(session_line, html_cards, end_line))

    index_record = {
        "record_type": "session_index",
        "session_id": chat_id,
        "title": session_line["title"],
        "chat_type": session_line["chat_type"],
        "created_at": created,
        "updated_at": updated,
        "message_count": len(ids),
        "event_count": event_seq,
        "event_counts": counts,
        "export_complete": complete,
        "problems": problems,
        "file": str(path.relative_to(out_dir)),
        "account": account_label,
        "format": fmt,
        "events": getattr(args, "events_tag", "all"),
    }
    return index_record


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _valid_date(s):
    """argparse type: reject values that are not YYYY-MM-DD calendar dates."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD, got %r" % s)
    return s


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="zai_exporter",
        description="Export all chat.z.ai sessions to precise, readable JSONL / "
                    "Markdown / HTML (user inputs, model thinking, replies, tool "
                    "calls — verbatim).")
    p.add_argument("--token", help="Bearer token from chat.z.ai (localStorage key 'token').")
    p.add_argument("--token-file", default="token.txt",
                   help="File containing the token (default: ./token.txt).")
    p.add_argument("--guest", action="store_true",
                   help="Force a brand-new guest session (free mode, empty history).")
    p.add_argument("-o", "--output", default="zai_export", help="Output directory (default: ./zai_export).")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base (default: %s)." % DEFAULT_BASE_URL)
    p.add_argument("--chat-type", default="default", help="Chat list type filter (default: 'default'; try 'all').")
    p.add_argument("--format", choices=("jsonl", "md", "html"), default="jsonl",
                   help="Session file format: jsonl (lossless, default), md (readable "
                        "Markdown), html (offline interactive viewer).")
    p.add_argument("--search", default=None, metavar="TEXT",
                   help="Only sessions whose title contains TEXT (case-insensitive).")
    p.add_argument("--events", default=None, metavar="T1,T2",
                   help="Keep only these event types in the output, e.g. "
                        "model_reply,thinking. Valid: %s. Default: all." % ",".join(EVENT_TYPES))
    p.add_argument("--last", type=int, default=None, metavar="N", help="Only the N most recent matching sessions.")
    p.add_argument("--from", dest="from_date", default=None, metavar="YYYY-MM-DD", type=_valid_date,
                   help="Sessions updated on/after this date (UTC).")
    p.add_argument("--to", dest="to_date", default=None, metavar="YYYY-MM-DD", type=_valid_date,
                   help="Sessions updated on/before this date (UTC).")
    p.add_argument("--session", action="append", default=[], metavar="ID", help="Export only these session ids (repeatable).")
    p.add_argument("--list", action="store_true", help="List sessions to stdout only; do not export.")
    p.add_argument("--resume", action="store_true",
                   help="Skip sessions already exported cleanly with unchanged updated_at "
                        "in the SAME format and event filter (reads previous index.jsonl).")
    p.add_argument("--workers", type=int, default=1, metavar="N",
                   help="Fetch N sessions concurrently (default 1 = serial with polite --delay). "
                        "Values > 1 disable the inter-session pause.")
    p.add_argument("--batch-size", type=int, default=50, help="Message ids per batch POST (default: 50).")
    p.add_argument("--retries", type=int, default=3, help="HTTP retries per request (default: 3).")
    p.add_argument("--delay", type=float, default=0.4, help="Pause between sessions in seconds (default: 0.4; serial mode only).")
    p.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds (default: 60).")
    p.add_argument("--no-merged", action="store_true", help="Skip merged_all.jsonl (jsonl format only).")
    p.add_argument("-q", "--quiet", action="store_true", help="Minimal progress output.")
    p.add_argument("-V", "--version", action="version", version="zai-session-exporter " + __version__)

    args = p.parse_args(argv)
    if args.workers < 1:
        p.error("--workers must be >= 1")
    if args.batch_size < 1:
        p.error("--batch-size must be >= 1")
    if args.retries < 1:
        p.error("--retries must be >= 1")
    if args.events:
        wanted = [t.strip() for t in args.events.split(",") if t.strip()]
        bad = [t for t in wanted if t not in EVENT_TYPES]
        if bad or not wanted:
            p.error("unknown or empty --events value(s): %s — valid types: %s"
                    % (", ".join(bad) or "(empty)", ", ".join(EVENT_TYPES)))
        args.events_set = frozenset(wanted)
        args.events_tag = ",".join(sorted(wanted))
    else:
        args.events_set = None
        args.events_tag = "all"
    return args


def _write_summary_file(out_dir, info):
    """Machine-readable run summary (integration point for scripts/wrappers)."""
    try:
        (out_dir / "export_summary.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        eprint("Run summary: %s" % (out_dir / "export_summary.json"))
    except OSError as err:
        eprint("WARNING: could not write export_summary.json: %s" % err)


def main(argv=None):
    force_utf8_stdio()
    args = parse_args(argv if argv is not None else sys.argv[1:])
    out_dir = Path(args.output).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    client = ZaiClient(args.base_url, retries=args.retries, timeout=args.timeout)

    # ---- identity ----------------------------------------------------------
    eprint("zai-session-exporter v%s" % __version__)
    try:
        if args.guest:
            if args.token or os.environ.get("ZAI_TOKEN", "").strip() or Path(args.token_file).is_file():
                eprint("NOTE: --guest combined with a token / ZAI_TOKEN / token.txt — "
                       "--guest wins: a brand-new empty guest identity will be used.")
            ident = client.create_guest_session()
            if not isinstance(ident, dict) or not ident.get("token"):
                raise ApiError("Guest provisioning failed.")
            client.token = ident["token"]
            account_label = "%s (guest, fresh)" % ident.get("email", "unknown@guest")
            source_desc = "fresh GUEST session (free mode)"
        else:
            ident, source_desc = connect(client, args)
            account_label = "%s (role=%s)" % (ident.get("email") or ident.get("name") or ident.get("id"), ident.get("role", "?"))
    except ApiError as err:
        eprint("ERROR: authentication failed: %s" % err)
        if getattr(err, "status", None) == 401:
            eprint("The server rejected this token (HTTP 401) — it is probably expired, "
                   "revoked, or was copied incompletely. Re-copy it: chat.z.ai -> F12 -> "
                   "Application -> Local Storage -> key 'token'.")
        eprint("Get your token: open https://chat.z.ai -> F12 -> Application -> "
               "Local Storage -> copy the value of the 'token' key, then:\n"
               "  zai_exporter.py --token '<TOKEN>'   (or put it in token.txt)")
        return 2
    eprint("Identity: %s [%s]" % (account_label, source_desc))

    # ---- list / resolve sessions -------------------------------------------
    summaries, unavailable = [], []
    try:
        direct = bool(args.session) and not (args.from_date or args.to_date or args.last is not None)
        if direct and getattr(args, "search", None):
            # v1.2.1 integration fix: --search used to be silently ignored in
            # direct mode; now we say so instead of staying quiet.
            eprint("NOTE: --search is ignored together with --session "
                   "(direct ids are already the precise selection).")
        if direct:
            # Requested ids are fetched straight away — no full listing needed.
            eprint("Fetching %d requested session(s) directly ..." % len(args.session))
            seen = set()
            for sid in args.session:
                sid = str(sid)
                if sid in seen:
                    continue
                seen.add(sid)
                try:
                    d = client.get_chat_details(sid)
                except ApiError as err:
                    eprint("WARNING: session %s unavailable: %s" % (sid, err))
                    unavailable.append(sid)
                    continue
                summaries.append({"id": d.get("id") or sid,
                                  "title": d.get("title") or "Untitled",
                                  "created_at": d.get("created_at"),
                                  "updated_at": d.get("updated_at")})
        else:
            eprint("Listing sessions (chat type: %s) ..." % args.chat_type)
            summaries = list_all_chats(client, args.chat_type, quiet=args.quiet)
            if args.session:
                wanted = set(args.session)
                summaries = [c for c in summaries if str(c.get("id")) in wanted]
                missing = wanted - {str(c.get("id")) for c in summaries}
                for mid in sorted(missing):
                    eprint("WARNING: session id %s not found in your list" % mid)
                unavailable.extend(sorted(missing))
            summaries = apply_scope(summaries, args)
    except ApiError as err:
        eprint("ERROR: could not list sessions: %s" % err)
        return 2

    eprint("Found %d session(s) in scope." % len(summaries))
    if args.list:
        for i, c in enumerate(summaries, 1):
            print("  %3d. [%s] %s  (id=%s, updated=%s)"
                  % (i, c.get("type", "?"), (c.get("title") or "Untitled")[:60],
                     c.get("id"), iso_ts(c.get("updated_at")) or "?"))
        if not summaries:
            print("  (no sessions in scope)")
        return 0
    if not summaries:
        eprint("Nothing to export. If this surprises you, read the token note above — "
               "a fresh guest identity has no history.")
        _write_summary_file(out_dir, {
            "tool": "zai-session-exporter", "version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "account": account_label, "base_url": args.base_url,
            "format": args.format, "events": args.events_tag, "workers": args.workers,
            "requested": 0, "exported_ok": 0, "incomplete": [],
            "unavailable": unavailable, "output_dir": str(out_dir), "merged": False,
        })
        return 0

    # ---- export ------------------------------------------------------------
    sessions_dir = out_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.jsonl"
    merged_path = out_dir / "merged_all.jsonl"

    # merged_all.jsonl is a JSONL stream of session files, so it only exists
    # for --format jsonl. For md/html runs it is deliberately skipped.
    merged_enabled = not args.no_merged and args.format == "jsonl"
    if not args.no_merged and args.format != "jsonl":
        eprint("NOTE: merged_all.jsonl is produced only with --format jsonl (current: %s)." % args.format)

    # --resume: remember cleanly exported sessions from a previous run.
    # A previous record only counts when the format AND event filter match,
    # otherwise switching --format would silently skip real work.
    prev_index = {}
    if args.resume and index_path.is_file():
        with index_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("record_type") == "session_index" \
                        and rec.get("export_complete") and rec.get("file"):
                    prev_index[str(rec.get("session_id"))] = rec

    def resume_matches(rec, summary):
        return (rec.get("format") == args.format
                and rec.get("events") == args.events_tag
                and rec.get("updated_at") == iso_or_none(summary.get("updated_at")))

    index_fh = index_path.open("w", encoding="utf-8")
    merged_fh = merged_path.open("w", encoding="utf-8") if merged_enabled else None

    ok_count, incomplete = 0, []
    started = time.time()

    def absorb(idx_rec):
        """Write an index record; in multi-worker mode also stream the finished
        session file into merged (single-worker mode already dual-wrote it)."""
        nonlocal ok_count
        index_fh.write(json.dumps(idx_rec, ensure_ascii=False) + "\n")
        index_fh.flush()
        if args.workers > 1 and merged_fh is not None:
            with (out_dir / idx_rec["file"]).open("r", encoding="utf-8") as sf:
                for line in sf:
                    merged_fh.write(line)
            merged_fh.flush()
        complete = idx_rec.get("export_complete")
        status = "complete" if complete else "INCOMPLETE (missing message payloads — rerun later)"
        eprint("    -> %s  [%s, %d events]" % (idx_rec["file"], status, idx_rec["event_count"]))
        if complete:
            ok_count += 1
        else:
            incomplete.append((idx_rec.get("session_id"), (idx_rec.get("title") or "")[:50],
                               "missing message payloads"))

    def record_failure(summary, err):
        title = (summary.get("title") or "Untitled")[:50]
        eprint("    FAILED: %s" % err)
        incomplete.append((str(summary.get("id")), title, str(err)))
        index_fh.write(json.dumps({
            "record_type": "session_failed",
            "session_id": summary.get("id"),
            "title": title,
            "account": account_label,
            "format": args.format,
            "error": str(err),
        }, ensure_ascii=False) + "\n")
        index_fh.flush()

    rc = 0
    try:
        # ---- passes: plan skipped (resume) vs to-fetch ----------------------
        todo = []
        for i, summary in enumerate(summaries, 1):
            prev = prev_index.get(str(summary.get("id")))
            if prev and (out_dir / prev["file"]).is_file() and resume_matches(prev, summary):
                title = (prev.get("title") or "Untitled")[:50]
                eprint("[%d/%d] %s ..." % (i, len(summaries), title))
                eprint("    unchanged since last export — skipped (--resume)")
                index_fh.write(json.dumps(prev, ensure_ascii=False) + "\n")
                index_fh.flush()
                if merged_fh is not None:
                    with (out_dir / prev["file"]).open("r", encoding="utf-8") as sf:
                        for line in sf:
                            merged_fh.write(line)
                    merged_fh.flush()
                ok_count += 1
            else:
                todo.append((i, summary))

        if args.workers > 1:
            # Concurrent fetching; results are absorbed in deterministic
            # summary order, so index.jsonl/merged stay identical to a serial
            # run. The inter-session --delay pause is disabled in this mode.
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = [(i, s, ex.submit(export_session, client, s, out_dir, i,
                                            args, account_label, None))
                           for i, s in todo]
                for i, summary, fut in futures:
                    title = (summary.get("title") or "Untitled")[:50]
                    eprint("[%d/%d] %s ..." % (i, len(summaries), title))
                    try:
                        idx_rec = fut.result()
                    except ApiError as err:
                        record_failure(summary, err)
                        continue
                    absorb(idx_rec)
        else:
            for i, summary in todo:
                title = (summary.get("title") or "Untitled")[:50]
                eprint("[%d/%d] %s ..." % (i, len(summaries), title))
                try:
                    idx_rec = export_session(client, summary, out_dir, i, args,
                                             account_label, merged_fh=merged_fh)
                except ApiError as err:
                    record_failure(summary, err)
                    continue
                absorb(idx_rec)
                if args.delay > 0 and i < len(summaries):
                    time.sleep(args.delay)
    except KeyboardInterrupt:
        eprint("\nInterrupted — partial index.jsonl kept.")
        rc = 130
    finally:
        index_fh.close()
        if merged_fh is not None:
            merged_fh.close()

    elapsed = round(time.time() - started, 1)
    eprint("")
    eprint("Done: %d/%d session(s) exported cleanly -> %s" % (ok_count, len(summaries), out_dir))
    if merged_fh is not None and merged_enabled:
        eprint("Merged stream: %s" % merged_path)
    eprint("Index: %s" % index_path)
    if incomplete:
        eprint("Attention: %d session(s) had problems:" % len(incomplete))
        for sid, title, why in incomplete:
            eprint("  - %s (%s): %s" % (sid, title[:40], why))
    _write_summary_file(out_dir, {
        "tool": "zai-session-exporter", "version": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": elapsed,
        "account": account_label, "base_url": args.base_url,
        "format": args.format, "events": args.events_tag, "workers": args.workers,
        "requested": len(summaries), "exported_ok": ok_count,
        "incomplete": [{"session_id": s, "title": t, "problem": w} for s, t, w in incomplete],
        "unavailable": unavailable,
        "output_dir": str(out_dir),
        "merged": merged_enabled and not args.no_merged,
    })
    if rc == 0 and (incomplete or unavailable):
        rc = 1  # partial success counts as failure for scripts (see README: Exit codes)
    return rc


if __name__ == "__main__":
    sys.exit(main())

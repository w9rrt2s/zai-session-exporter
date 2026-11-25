#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zai-session-exporter — Comprehensive chat.z.ai session exporter to JSONL.

Exports EVERY session linked to a chat.z.ai identity into clean, readable,
precise JSONL files, capturing literally every action in each session:

  * user inputs (text + attached files)
  * the model's thinking / reasoning before each reply
  * the model's final replies
  * every tool call and tool result (verbatim arguments and payloads)
  * token usage, model names, timestamps, branch (parent/child) structure
  * `raw_original` on every line: the verbatim API message (parsed JSON, key order preserved)

Output layout (both organizations at once):
  <output>/sessions/NNN_<title>_<id>.jsonl   one file per session
  <output>/index.jsonl                       one summary line per session
  <output>/merged_all.jsonl                  every line of every session

API contract (reverse-engineered, stable since 2025):
  GET  /api/v1/auths/                      -> current identity (or NEW guest if unauthenticated)
  GET  /api/v1/chats/?page=N               -> list of session summaries (paginated)
  GET  /api/v1/chats/{id}                  -> session metadata + message-id map
  POST /api/v1/chats/{id}/messages/batch   -> {"ids": [...]} => {"data": {id: message}}

Zero third-party dependencies (Python 3.8+ stdlib only).

MIT License. See LICENSE.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
except ImportError:  # pragma: no cover
    sys.stderr.write("Python 3 with urllib is required.\n")
    raise

__version__ = "1.1.0"
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


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, message, status=None, url=None):
        super().__init__(message)
        self.status = status
        self.url = url


class ZaiClient:
    """Minimal authenticated client for the chat.z.ai web API."""

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
        chunks = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)] or []
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
    """Walk all pages of GET /chats/ and return summaries in API order."""
    summaries, page = [], 1
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
        summaries.extend(data)
        if not quiet:
            eprint("  listed page %d (%d sessions so far)" % (page, len(summaries)))
        page += 1
        if page > 10000:  # absurd hard cap; API pagination always ends earlier
            break
    return summaries


def apply_scope(summaries, args):
    """Apply --from/--to/--last filters on updated_at (API order is newest-first)."""
    out = summaries
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
    def walk(mid):
        if mid in visited:
            return
        visited.add(mid)
        ordered.append(mid)
        for kid in children_of.get(mid, []):
            walk(kid)

    roots = [mid for mid in ids if mid not in parent_of]
    for r in sorted(roots, key=sort_key):
        walk(r)
    for mid in ids:            # anything unreachable (broken links) keeps canonical order
        walk(mid)
    return ordered, len(ids) - len(set(parent_of.values()))  # ordered, root count


# ---------------------------------------------------------------------------
# Export pipeline
# ---------------------------------------------------------------------------

def export_session(client, summary, out_dir, ordinal, args, account_label):
    """Fetch + convert one session. Returns (index_record, ok)."""
    chat_id = str(summary.get("id"))
    title = summary.get("title") or "Untitled"
    fname = "%03d_%s_%s.jsonl" % (ordinal, safe_filename(title), chat_id[:8])
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
    ordered_present = [mid for mid in order]
    missing_ids = [mid for mid in ids if mid not in messages_map]
    display_ids = (ordered_present or [mid for mid in ids if mid in messages_map]) + missing_ids

    # ---- build lines -------------------------------------------------------
    lines = []

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
        "raw_original": detail,   # the full, untouched session detail response
    }
    lines.append(session_line)

    counts = {}
    event_seq = 0
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
            event_seq += 1
            ev = {
                "record_type": "event",
                "schema_version": SCHEMA_VERSION,
                "session_id": chat_id,
                "event_seq": event_seq,
                "display_seq": display_i,
                **ev,
            }
            counts[ev["event"]] = counts.get(ev["event"], 0) + 1
            lines.append(ev)

    end_line = {
        "record_type": "session_end",
        "schema_version": SCHEMA_VERSION,
        "session_id": chat_id,
        "event_count": event_seq,
        "event_counts": counts,
        "export_complete": complete,
        "problems": problems,
    }
    lines.append(end_line)

    # ---- write -------------------------------------------------------------
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

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
    }
    return index_record, complete


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
        description="Export all chat.z.ai sessions to precise, readable JSONL "
                    "(user inputs, model thinking, replies, tool calls — verbatim).")
    p.add_argument("--token", help="Bearer token from chat.z.ai (localStorage key 'token').")
    p.add_argument("--token-file", default="token.txt",
                   help="File containing the token (default: ./token.txt).")
    p.add_argument("--guest", action="store_true",
                   help="Force a brand-new guest session (free mode, empty history).")
    p.add_argument("-o", "--output", default="zai_export", help="Output directory (default: ./zai_export).")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base (default: %s)." % DEFAULT_BASE_URL)
    p.add_argument("--chat-type", default="default", help="Chat list type filter (default: 'default'; try 'all').")
    p.add_argument("--last", type=int, default=None, metavar="N", help="Only the N most recent sessions.")
    p.add_argument("--from", dest="from_date", default=None, metavar="YYYY-MM-DD", type=_valid_date,
                   help="Sessions updated on/after this date (UTC).")
    p.add_argument("--to", dest="to_date", default=None, metavar="YYYY-MM-DD", type=_valid_date,
                   help="Sessions updated on/before this date (UTC).")
    p.add_argument("--session", action="append", default=[], metavar="ID", help="Export only these session ids (repeatable).")
    p.add_argument("--list", action="store_true", help="List sessions to stdout only; do not export.")
    p.add_argument("--resume", action="store_true",
                   help="Skip sessions already exported cleanly with unchanged updated_at (reads previous index.jsonl).")
    p.add_argument("--batch-size", type=int, default=50, help="Message ids per batch POST (default: 50).")
    p.add_argument("--retries", type=int, default=3, help="HTTP retries per request (default: 3).")
    p.add_argument("--delay", type=float, default=0.4, help="Pause between sessions in seconds (default: 0.4).")
    p.add_argument("--timeout", type=int, default=60, help="HTTP timeout seconds (default: 60).")
    p.add_argument("--no-merged", action="store_true", help="Skip merged_all.jsonl.")
    p.add_argument("-q", "--quiet", action="store_true", help="Minimal progress output.")
    p.add_argument("-V", "--version", action="version", version="zai-session-exporter " + __version__)
    return p.parse_args(argv)


def main(argv=None):
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
        eprint("Get your token: open https://chat.z.ai -> F12 -> Application -> "
               "Local Storage -> copy the value of the 'token' key, then:\n"
               "  zai_exporter.py --token '<TOKEN>'   (or put it in token.txt)")
        return 2
    eprint("Identity: %s [%s]" % (account_label, source_desc))

    # ---- list / resolve sessions -------------------------------------------
    summaries, unavailable = [], []
    try:
        direct = bool(args.session) and not (args.from_date or args.to_date or args.last is not None)
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
            print("  %3d. [%s] %s  (id=%s, updated=%s, %s msg(s))"
                  % (i, c.get("type", "?"), (c.get("title") or "Untitled")[:60],
                     c.get("id"), iso_ts(c.get("updated_at")) or "?", c.get("message_count", "?")))
        return 0
    if not summaries:
        eprint("Nothing to export. If this surprises you, read the token note above — "
               "a fresh guest identity has no history.")
        return 0

    # ---- export ------------------------------------------------------------
    sessions_dir = out_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.jsonl"
    merged_path = out_dir / "merged_all.jsonl"

    # --resume: remember cleanly exported sessions from a previous run.
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

    index_fh = index_path.open("w", encoding="utf-8")
    merged_fh = None if args.no_merged else merged_path.open("w", encoding="utf-8")

    ok_count, incomplete = 0, []
    try:
        for i, summary in enumerate(summaries, 1):
            title = (summary.get("title") or "Untitled")[:50]
            eprint("[%d/%d] %s ..." % (i, len(summaries), title))
            prev = prev_index.get(str(summary.get("id")))
            if prev and (out_dir / prev["file"]).is_file() and \
                    prev.get("updated_at") == iso_or_none(summary.get("updated_at")):
                eprint("    unchanged since last export — skipped (--resume)")
                index_fh.write(json.dumps(prev, ensure_ascii=False) + "\n")
                index_fh.flush()
                if merged_fh is not None:
                    with (out_dir / prev["file"]).open("r", encoding="utf-8") as sf:
                        for line in sf:
                            merged_fh.write(line)
                    merged_fh.flush()
                ok_count += 1
                continue
            try:
                idx_rec, complete = export_session(client, summary, out_dir, i, args, account_label)
            except ApiError as err:
                eprint("    FAILED: %s" % err)
                incomplete.append((summary.get("id"), title, str(err)))
                index_fh.write(json.dumps({
                    "record_type": "session_failed",
                    "session_id": summary.get("id"),
                    "title": title,
                    "account": account_label,
                    "error": str(err),
                }, ensure_ascii=False) + "\n")
                index_fh.flush()
                continue
            index_fh.write(json.dumps(idx_rec, ensure_ascii=False) + "\n")
            index_fh.flush()
            if merged_fh is not None:
                session_file = out_dir / idx_rec["file"]
                with session_file.open("r", encoding="utf-8") as sf:
                    for line in sf:
                        merged_fh.write(line)
                merged_fh.flush()
            status = "complete" if complete else "INCOMPLETE (missing message payloads — rerun later)"
            eprint("    -> %s  [%s, %d events]" % (idx_rec["file"], status, idx_rec["event_count"]))
            if not complete:
                incomplete.append((summary.get("id"), title, "missing message payloads"))
            else:
                ok_count += 1
            if args.delay > 0 and i < len(summaries):
                time.sleep(args.delay)
    except KeyboardInterrupt:
        eprint("\nInterrupted — partial index.jsonl kept.")
        rc = 130
    else:
        rc = 0
    finally:
        index_fh.close()
        if merged_fh is not None:
            merged_fh.close()

    eprint("")
    eprint("Done: %d/%d session(s) exported cleanly -> %s" % (ok_count, len(summaries), out_dir))
    if merged_fh is not None and not args.no_merged:
        eprint("Merged stream: %s" % merged_path)
    eprint("Index: %s" % index_path)
    if incomplete:
        eprint("Attention: %d session(s) had problems:" % len(incomplete))
        for sid, title, why in incomplete:
            eprint("  - %s (%s): %s" % (sid, title[:40], why))
    if rc == 0 and (incomplete or unavailable):
        rc = 1  # partial success counts as failure for scripts (see README: Exit codes)
    return rc


if __name__ == "__main__":
    sys.exit(main())

# zai-session-exporter

[![CI](https://github.com/w9rrt2s/zai-session-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/w9rrt2s/zai-session-exporter/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.8%20%7C%203.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)

Export **every session linked to your chat.z.ai identity** into clean, precise,
readable files — capturing literally every action that happened:

- **user inputs** (text + attached files)
- the model's **thinking / reasoning** produced before each reply
- the model's **final replies**
- every **tool call** and **tool result** (verbatim arguments and payloads)
- **token usage**, model names, timestamps, and branch (regenerated-answer) structure
- `raw_original` on every JSONL line: the **verbatim API message object** (parsed JSON, key order preserved)

Three output **formats**:

| `--format` | Get | Best for |
| --- | --- | --- |
| `jsonl` *(default)* | one lossless JSON line per action | archives, reprocessing, code |
| `md` | clean readable Markdown per session | reading, notes, Obsidian |
| `html` | self-contained offline viewer per session | browsing: collapsible thinking, live text filter, styled tool calls, RTL-safe — works from a double-click, no server, no internet |

Single-file core, **zero dependencies** (Python 3.8+ standard library only),
pip-installable, importable as a library, CI-tested on Linux + Windows.

---

## Install

**Option A — pip (recommended; gives you the `zai-exporter` command):**

```bash
pip install git+https://github.com/w9rrt2s/zai-session-exporter.git
# or, after cloning:
pip install .

zai-exporter --help
```

**Option B — pipx (isolated CLI):**

```bash
pipx install git+https://github.com/w9rrt2s/zai-session-exporter.git
```

**Option C — single file (no install at all):**

```bash
python3 zai_exporter.py --help
```

## How it gets your sessions

chat.z.ai stores chats server-side and exposes a clean internal web API. This
tool speaks that API directly, **read-only**:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/auths/` | identifies the current session (or provisions a new guest) |
| `GET /api/v1/chats/?page=N` | paginated list of session summaries |
| `GET /api/v1/chats/{id}` | session metadata + the ordered message-id map |
| `POST /api/v1/chats/{id}/messages/batch` | full content of messages by id (chunked, retried) |

### Getting your token (works for both free and logged-in usage)

1. Open [chat.z.ai](https://chat.z.ai) and log in (or just use it free).
2. Press **F12** → **Application** tab → **Local Storage** → `https://chat.z.ai`.
3. Copy the value of the key **`token`**.
4. Either paste it into a `token.txt` file next to the script, set
   `ZAI_TOKEN=...`, or pass `--token '...'`.

> The token in your browser localStorage is what ties your sessions to you —
> **free guest users have one too**, so this works the same whether or not you
> ever signed in.

### About `--guest` (fully automatic free mode)

Running with `--guest` (or with no token anywhere) makes the server provision a
**brand-new guest identity**. That identity is fresh and therefore has an
**empty history** — useful for testing the pipeline, but it cannot see your
existing sessions. To export your real sessions, use the token from your
browser as shown above.

---

## Usage

```bash
# everything linked to your account/browser identity
zai-exporter --token 'eyJ...' -o my_export

# token already saved in token.txt (default lookup)
zai-exporter

# last 10 sessions only
zai-exporter --last 10

# date range (UTC, by last-update)
zai-exporter --from 2026-01-01 --to 2026-11-26

# only sessions whose title contains a word
zai-exporter --search sales

# one specific session (fetched directly, without listing everything first)
zai-exporter --session 6f8a...c90

# list what would be exported, without exporting (prints to stdout)
zai-exporter --list

# readable Markdown instead of JSONL
zai-exporter --format md

# interactive offline HTML viewers (one per session)
zai-exporter --format html

# keep only replies and thinking (drop tool noise)
zai-exporter --events model_reply,thinking,user_input

# fetch 4 sessions concurrently (skips the polite --delay pause)
zai-exporter --workers 4

# skip sessions already exported cleanly and unchanged (same format + filter)
zai-exporter --resume

# free automatic mode (new empty guest — see note above)
zai-exporter --guest
```

All flags: `zai-exporter --help` (or `python3 zai_exporter.py --help`).

## Exit codes

| code | meaning |
| --- | --- |
| `0` | every session exported cleanly |
| `1` | exported, but some sessions had problems (see `index.jsonl`, `session_failed` records and stderr) |
| `2` | authentication or session listing failed |
| `130` | interrupted with Ctrl-C (partial `index.jsonl` kept) |

## Output layout

```
my_export/
├── index.jsonl             # one summary line per exported session
├── merged_all.jsonl        # every line of every session, one stream (jsonl format only)
├── export_summary.json     # machine-readable run summary (counts, elapsed, problems)
└── sessions/
    ├── 001_Weather_check_—_Baghdad_bbbb2222.jsonl    (or .md / .html)
    ├── 002_Sales_trend_analysis_&_chart_aaaa1111.jsonl
    └── ...
```

`export_summary.json` is the integration point for scripts and wrappers: it
records the tool version, account, format, event filter, worker count,
per-session problems and elapsed time — no stderr scraping needed.

## JSONL schema

Every session file is three kinds of lines:

**1. `session` header** — metadata + the untouched detail response:

```json
{"record_type": "session", "session_id": "bbbb…", "title": "Weather check — Baghdad",
 "created_at": "2025-11-25T19:30:00+00:00", "updated_at": "…", "message_count": 3,
 "export_complete": true, "problems": [], "raw_original": {…entire API detail response…}}
```

**2. `event` lines** — one line per action, in conversation order:

```json
{"record_type": "event", "session_id": "…", "event_seq": 3, "display_seq": 2,
 "message_seq": 2, "message_id": "m2", "parent_id": "m1", "children_ids": ["m4"],
 "event": "thinking", "role": "assistant", "model": "glm-5.3", "usage": {…},
 "timestamp": "2025-11-25T19:34:05+00:00",
 "text": "The user wants a trend chart. First I must inspect the CSV…",
 "tool": null, "files": null, "raw_block": {"type": "reasoning", "content": "…"},
 "raw_original": {…entire API message verbatim…}}
```

Event types (also the valid values of `--events`):

| `event` | Meaning |
| --- | --- |
| `user_input` | user message text |
| `attachment` | file attached by the user (or media block) |
| `thinking` | model reasoning block (`reasoning` / `thinking` / `thought` / `plan` / top-level `reasoning` / `reasoning_content`) |
| `model_reply` | answer text of the assistant |
| `tool_call` | a tool invocation with its verbatim arguments |
| `tool_result` | the tool's response payload |
| `other` | system / metadata / error blocks, or messages with no recoverable payload |
| `missing_message` | a message id the server did not return — exported as its own event line, never silently dropped |

Tool fields are parsed into `tool.id`, `tool.name`, `tool.status`,
`tool.arguments` — and the original block stays in `raw_block`, the whole
message in `raw_original`.

**3. `session_end` footer** — event counts + completeness flag.

Nothing is reordered inside the raw data: `raw_original` keeps the server's
exact JSON structure and key order, and text fields are extracted read-only.
(One honest caveat: this is parsed JSON re-serialized, not raw bytes — a
duplicated key inside one server object would collapse, and `\u00XX` escapes
normalize to real characters.)

### Example files

- [`examples/sample_session.jsonl`](examples/sample_session.jsonl) — lossless JSONL (with `raw_original`)
- [`examples/sample_session.md`](examples/sample_session.md) — the same session as readable Markdown
- [`examples/sample_session.html`](examples/sample_session.html) — the same session as an offline interactive viewer (download and double-click)

## Use as a Python library

The CLI is a thin wrapper; everything is importable:

```python
import zai_exporter as zx

# 1. run the CLI programmatically (returns the process exit code)
rc = zx.main(["--token", "eyJ...", "--format", "md", "-o", "out"])

# 2. or use the pieces directly
client = zx.ZaiClient("https://chat.z.ai", token="eyJ...")
detail = client.get_chat_details("6f8a...c90")
events = zx.extract_events(message_dict, message_seq=1)
ids = zx.canonical_ids(detail)
order, roots = zx.readable_order(messages_by_id)
```

## Performance notes

- **Concurrent fetching**: `--workers N` fetches N sessions in parallel and
  absorbs results in deterministic order, so `index.jsonl` / `merged_all.jsonl`
  stay byte-identical to a serial run. The polite `--delay` pause applies only
  in serial mode; with workers, rely on the built-in 429/5xx backoff.
- **Single serialization of `raw_original`**: a message emitting k events used
  to re-encode its full raw object k times; v1.2+ encodes it once and splices
  the cached JSON into each line (byte-identical output, less CPU).
- **Streaming writes**: lines are written to the session file (and the merged
  stream) as they are built — the whole run no longer buffers every line dict
  in memory, and merged no longer re-reads each finished session file.
- **Known networking limit**: urllib is used *deliberately* so `http_proxy` /
  `https_proxy` environment variables keep working (corporate proxies). urllib
  has no connection pooling, so each request pays one TLS handshake; connection
  reuse is on the roadmap behind an opt-in flag.
- Sessions are fetched in chunks of 50 message ids with retry + exponential
  backoff; `export_complete: false` + `problems` lists anything the server
  failed to return (rerun later, or use `--resume`).

## Testing

Offline end-to-end test against a faithful mock of the chat.z.ai API:

```bash
python3 tests/test_exporter.py   # self-contained: starts its own mock API
```

The fixture covers: attachments, multi-block reasoning, multiple sequential
tool calls with results, OpenAI-style top-level `tool_calls`, `role=tool`
result messages, `reasoning_content`, regenerated-answer branches, a broken
session whose payload the server lost, and token usage — **115 checks** verify
verbatim fidelity, exit codes, `--resume` semantics (including format/filter
changes), `--session`/`--list`/`--search`/`--events`/`--workers` behavior, all
three output formats, `export_summary.json`, the 401 token hint, duplicate-id
de-duplication and stdout/stderr routing. CI runs the same suite on Linux and
Windows across Python 3.8–3.12.

## Notes, limits & known flaws (read before relying on this)

- Exporting uses only read endpoints; the tool cannot modify or delete anything.
- **The API is unofficial** (reverse-engineered). It can change at any time and
  break the tool; unknown list/detail shapes fail loudly instead of exporting
  nothing silently.
- **Attachments are metadata only** — the tool records file names/sizes but
  does not download file contents (the CDN URL/auth model is unverified;
  opt-in downloading is on the roadmap).
- **No import back into chat.z.ai.** Re-uploading could only recreate a lossy
  copy (new message ids, new timestamps, no thinking blocks), so the tool
  stays read-only by design.
- **`--resume` is format-aware and filter-aware**: switching `--format` or
  `--events` re-exports, so you never get stale mixed-format output.
- **A session's message map + serialized raw cache are held in memory** while
  that session is written (sessions are processed one at a time per worker);
  gigantic single sessions with huge embedded payloads may still need a lot of
  RAM.
- `raw_original` is verbatim *structure* (see the JSON caveat above), not a
  raw byte capture.
- Passing `--token '...'` exposes the token in your shell history and process
  list; prefer `token.txt` or the `ZAI_TOKEN` environment variable.
- `merged_all.jsonl` is only produced for `--format jsonl` (it is a JSONL
  stream of the session files); md/html runs state this once and skip it.
- `--chat-type` defaults to `default` (what the web app uses); pass `all` if
  your account keeps other chat types.
- `--from`/`--to` compare UTC calendar days of `updated_at`, not your local
  timezone.

### Roadmap

- opt-in attachment content download
- conversion to ChatGPT-compatible JSON for third-party viewers
- opt-in HTTP connection reuse (keep-alive) for handshake-free batching

## License

MIT — see [LICENSE](LICENSE).

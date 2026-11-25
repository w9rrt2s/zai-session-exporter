# zai-session-exporter

Export **every session linked to your chat.z.ai identity** into clean, precise,
human-readable **JSONL** — capturing literally every action that happened:

- **user inputs** (text + attached files)
- the model's **thinking / reasoning** produced before each reply
- the model's **final replies**
- every **tool call** and **tool result** (verbatim arguments and payloads)
- **token usage**, model names, timestamps, and branch (regenerated-answer) structure
- `raw_original` on every line: the **verbatim API message object** (parsed JSON, key order preserved)

Single-file CLI, **zero dependencies** (Python 3.8+ standard library only).

---

## How it gets your sessions

chat.z.ai stores chats server-side and exposes a clean internal web API. This
tool speaks that API directly:

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
python3 zai_exporter.py --token 'eyJ...' -o my_export

# token already saved in token.txt (default lookup)
python3 zai_exporter.py

# last 10 sessions only
python3 zai_exporter.py --last 10

# date range (UTC, by last-update)
python3 zai_exporter.py --from 2026-01-01 --to 2026-11-26

# one specific session (fetched directly, without listing everything first)
python3 zai_exporter.py --session 6f8a...c90

# list what would be exported, without exporting (prints to stdout)
python3 zai_exporter.py --list

# skip sessions already exported cleanly and unchanged since then
python3 zai_exporter.py --resume

# free automatic mode (new empty guest — see note above)
python3 zai_exporter.py --guest
```

All flags: `python3 zai_exporter.py --help`.

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
├── index.jsonl          # one summary line per exported session
├── merged_all.jsonl     # every line of every session, one stream
└── sessions/
    ├── 001_Weather_check_—_Baghdad_bbbb2222.jsonl
    ├── 002_Sales_trend_analysis_&_chart_aaaa1111.jsonl
    └── ...
```

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

Event types:

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

Tool fields are parsed into `tool.id`, `tool.name`, `tool.arguments` — and the
original block stays in `raw_block`, the whole message in `raw_original`.

**3. `session_end` footer** — event counts + completeness flag.

Nothing is reordered inside the raw data: `raw_original` keeps the server's
exact JSON structure and key order, and text fields are extracted read-only.
(One honest caveat: this is parsed JSON re-serialized, not raw bytes — a
duplicated key inside one server object would collapse, and `\u00XX` escapes
normalize to real characters.)

---

## Testing

Offline end-to-end test against a faithful mock of the chat.z.ai API:

```bash
python3 tests/test_exporter.py   # self-contained: starts its own mock API
```

The fixture covers: attachments, multi-block reasoning, multiple sequential
tool calls with results, OpenAI-style top-level `tool_calls`, `role=tool`
result messages, `reasoning_content`, regenerated-answer branches, a broken
session whose payload the server lost, and token usage — 70 checks verify
verbatim fidelity, exit codes, `--resume`/`--session`/`--list` behavior and
stdout/stderr routing.

## Notes, limits & known flaws (read before relying on this)

- Exporting uses only read endpoints; the tool cannot modify or delete anything.
- **The API is unofficial** (reverse-engineered). It can change at any time
  and break the tool; unknown list/detail shapes now fail loudly instead of
  exporting nothing silently.
- **Attachments are metadata only** — the tool records file names/sizes but
  does not download file contents.
- **No import back into chat.z.ai.** Re-uploading could only recreate a lossy
  copy (new message ids, new timestamps, no thinking blocks), so the tool
  stays read-only by design.
- **No incremental export by default** — every run re-fetches everything.
  `--resume` skips sessions already exported cleanly with unchanged
  `updated_at`; partially-broken sessions are always re-fetched.
- **A whole session is held in memory** while messages are ordered; gigantic
  sessions with huge embedded payloads may need a lot of RAM.
- `raw_original` is verbatim *structure* (see the JSON caveat above), not a
  raw byte capture.
- Passing `--token '...'` exposes the token in your shell history and process
  list; prefer `token.txt` or the `ZAI_TOKEN` environment variable.
- Very large sessions are fetched in chunks of 50 message ids with retry +
  exponential backoff; `export_complete: false` + `problems` lists anything
  the server failed to return (rerun, or use `--resume`).
- `--chat-type` defaults to `default` (what the web app uses); pass `all` if
  your account keeps other chat types.
- Rate limiting: `--delay` (default 0.4 s) pauses politely between sessions.

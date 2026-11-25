#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mock chat.z.ai API server for testing zai_exporter.py offline.

Emulates the reverse-engineered contract:
  GET  /api/v1/auths/                 (no auth -> new guest; Bearer -> identity)
  GET  /api/v1/chats/?page=N&type=T   -> session summaries (paginated, 2 per page)
  GET  /api/v1/chats/{id}             -> detail with chat.history.messages id-map
  POST /api/v1/chats/{id}/messages/batch -> {"data": {id: message}}

Run:  python3 tests/mock_zai_server.py [port]
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE_SIZE = 2

def ts(epoch):
    return epoch

def build_fixture():
    """Two sessions covering every event type the exporter understands."""
    sessions = {}

    # -- session A: rich agent-style conversation --------------------------
    a_id = "aaaa1111-2222-3333-4444-555566667777"
    a_msgs = {}
    a_msgs["m1"] = {
        "id": "m1", "role": "user", "timestamp": 1764000000,
        "content": "Help me analyze sales.csv and chart the trend.",
        "files": [{"id": "f1", "name": "sales.csv", "size": 2048, "type": "text/csv"}],
        "parent_id": None, "children_ids": ["m2"],
    }
    a_msgs["m2"] = {
        "id": "m2", "role": "assistant", "model": "glm-5.3", "timestamp": 1764000045,
        "parent_id": "m1", "children_ids": ["m4"],
        "usage": {"prompt_tokens": 912, "completion_tokens": 340, "total_tokens": 1252},
        "content_blocks": [
            {"type": "reasoning", "content": "The user wants a trend chart. First I must inspect the CSV, then compute monthly aggregates, then plot."},
            {"type": "tool_call", "id": "call_01", "name": "code_interpreter", "input": {"language": "python", "code": "import pandas as pd\ndf = pd.read_csv('sales.csv')\nprint(df.groupby(df.month)['revenue'].sum())"}},
            {"type": "tool_result", "tool_call_id": "call_01", "status": "success",
             "content": "month\n2026-01    12000\n2026-02    15300\n2026-03    17890\ndtype: int64"},
            {"type": "reasoning", "content": "Revenue grows month over month. I will generate a matplotlib line chart now."},
            {"type": "tool_call", "id": "call_02", "name": "code_interpreter", "input": {"language": "python", "code": "df.groupby('month')['revenue'].sum().plot(kind='line')"}},
            {"type": "tool_result", "tool_call_id": "call_02", "status": "success", "content": "[chart saved: trend.png]"},
            {"type": "text", "content": "Revenue grew steadily: **12,000 -> 15,300 -> 17,890** across Q1. The chart is saved as trend.png."},
        ],
        "content": "Revenue grew steadily: **12,000 -> 15,300 -> 17,890** across Q1. The chart is saved as trend.png.",
    }
    a_msgs["m3"] = {
        # a branch: regenerated answer for m1 (not on main chain)
        "id": "m3", "role": "assistant", "model": "glm-5.3-flash", "timestamp": 1764000050,
        "parent_id": "m1", "children_ids": [],
        "content": "(older draft answer, superseded)",
    }
    a_msgs["m4"] = {
        "id": "m4", "role": "user", "timestamp": 1764000300,
        "content": "Now export the same analysis as JSON.",
        "parent_id": "m2", "children_ids": ["m5"],
    }
    a_msgs["m5"] = {
        "id": "m5", "role": "assistant", "model": "glm-5.3", "timestamp": 1764000320,
        "parent_id": "m4", "children_ids": [],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 180, "total_tokens": 1380},
        "reasoning": "Simple transform: reuse the monthly aggregates and json.dumps them.",
        "content": "Here is the JSON:\n```json\n{\"months\": [12000, 15300, 17890]}\n```",
        "done": True,
    }
    sessions[a_id] = {
        "id": a_id, "title": "Sales trend analysis & chart", "type": "default",
        "created_at": 1763999000, "updated_at": 1764000320,
        "pinned": False, "folder_id": None,
        "messages": a_msgs,
    }

    # -- session B: plain chat with OpenAI-style top-level tool_calls ------
    b_id = "bbbb2222-3333-4444-5555-666677778888"
    b_msgs = {}
    b_msgs["n1"] = {
        "id": "n1", "role": "user", "timestamp": 1764100000,
        "content": "What is the weather in Baghdad?",
        "parent_id": None, "children_ids": ["n2", "n3"],
    }
    b_msgs["n2"] = {
        "id": "n2", "role": "assistant", "model": "glm-5.4", "timestamp": 1764100010,
        "parent_id": "n1", "children_ids": [],
        "reasoning_content": "Weather question -> call the weather MCP tool for Baghdad, then summarize.",
        "tool_calls": [
            {"id": "call_w1", "type": "function",
             "function": {"name": "get_weather", "arguments": "{\"city\": \"Baghdad\", \"unit\": \"celsius\"}"}},
        ],
        "content": "",
    }
    b_msgs["n3"] = {
        "id": "n3", "role": "tool", "timestamp": 1764100015,
        "parent_id": "n2", "children_ids": [],
        "tool_call_id": "call_w1", "name": "get_weather",
        "content": "{\"temp_c\": 24, \"condition\": \"sunny\"}",
    }
    sessions[b_id] = {
        "id": b_id, "title": "Weather check — Baghdad", "type": "default",
        "created_at": 1764099000, "updated_at": 1764100015,
        "pinned": True, "folder_id": None,
        "messages": b_msgs,
    }

    # -- session C: detail advertises a message id the store cannot return --
    c_id = "cccc3333-4444-5555-6666-777788889999"
    sessions[c_id] = {
        "id": c_id, "title": "Broken session — payload missing", "type": "default",
        "created_at": 1763900000, "updated_at": 1763900010,
        "pinned": False, "folder_id": None,
        "messages": {},                 # batch store has nothing ...
        "advertised_ids": ["x1"],       # ... but the detail map still lists x1
    }

    return sessions

SESSIONS = build_fixture()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default logging
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return (self.headers.get("Authorization") or "").startswith("Bearer ")

    def do_GET(self):
        if self.path == "/api/v1/auths/":
            auth = self.headers.get("Authorization") or ""
            if auth == "Bearer expired-token":
                # lets tests verify the HTTP 401 -> "token expired" hint path
                return self._json({"detail": "invalid or expired token"}, 401)
            if self._authed():
                return self._json({"id": "user-1", "email": "tester@example.com",
                                   "name": "Tester", "role": "user", "token_type": "Bearer"})
            return self._json({"id": "guest-x", "email": "guest-x@guest.com",
                               "name": "Guest-X", "role": "guest",
                               "token": "guest-token-xyz", "token_type": "Bearer"})
        path_only = self.path.split("?")[0]
        m = re.match(r"^/api/v1/chats/([^/?]+)$", path_only)
        if m and self._authed():
            sid = m.group(1)
            if sid not in SESSIONS:
                return self._json({"detail": "Not Found"}, 404)
            s = SESSIONS[sid]
            return self._json({
                "id": s["id"], "title": s["title"], "type": s["type"],
                "created_at": s["created_at"], "updated_at": s["updated_at"],
                "chat": {"id": s["id"],
                         "history": {"messages": {mid: {"id": mid} for mid in s.get("advertised_ids", s["messages"])}}},
            })
        if self.path.startswith("/api/v1/chats/"):
            page = 1
            mp = re.search(r"[?&]page=(\d+)", self.path)
            if mp:
                page = int(mp.group(1))
            items = sorted(SESSIONS.values(), key=lambda s: s["updated_at"], reverse=True)
            chunk = items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
            return self._json([{"id": s["id"], "title": s["title"], "type": s["type"],
                                "created_at": s["created_at"], "updated_at": s["updated_at"],
                                "pinned": s.get("pinned", False), "folder_id": None}
                               for s in chunk])
        return self._json({"detail": "Not Found"}, 404)

    def do_POST(self):
        m = re.match(r"^/api/v1/chats/([^/]+)/messages/batch$", self.path)
        if m and self._authed():
            sid = m.group(1)
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            ids = payload.get("ids", [])
            if sid not in SESSIONS:
                return self._json({"detail": "Not Found"}, 404)
            data = {mid: SESSIONS[sid]["messages"][mid] for mid in ids if mid in SESSIONS[sid]["messages"]}
            return self._json({"data": data})
        return self._json({"detail": "Not Found"}, 404)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8399
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("mock chat.z.ai API on http://127.0.0.1:%d" % port, flush=True)
    server.serve_forever()

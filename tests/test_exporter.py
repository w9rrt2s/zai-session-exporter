#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-contained end-to-end + unit validation of zai_exporter.

Starts its own mock chat.z.ai API server on a free port, runs the exporter
in several modes (full export, --resume, --session direct, --list, bad
flags, --guest conflict) and asserts on files, index records, exit codes
and stdout/stderr routing. Plus unit checks on the parsing internals.

Runs from a repo checkout (tests/test_exporter.py) or a dev tree.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if (HERE.parent / "zai_exporter.py").is_file():
    REPO = HERE.parent                      # running as <repo>/tests/test_exporter.py
else:
    REPO = Path("/home/z/my-project/download/zai-session-exporter")  # dev checkout
sys.path.insert(0, str(REPO))

from tests.mock_zai_server import SESSIONS       # fixture source of truth
import zai_exporter as zx                        # unit-under-test helpers

_failures = []
_passes = [0]

def check(cond, msg):
    if cond:
        _passes[0] += 1
        print("  PASS  %s" % msg)
    else:
        _failures.append(msg)
        print("  FAIL  %s" % msg)

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

def wait_port(port, secs=10):
    url = "http://127.0.0.1:%d/api/v1/auths/" % port
    for _ in range(secs * 10):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # any HTTP answer means the server is up
        except Exception:
            time.sleep(0.1)
    return False

def run(args, outdir, timeout=120):
    return subprocess.run(
        [sys.executable, "zai_exporter.py", "--base-url",
         "http://127.0.0.1:%d" % PORT, "-o", str(outdir), "--delay", "0"] + args,
        cwd=str(REPO), capture_output=True, text=True, timeout=timeout)

def read_lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

def main():
    global PORT
    PORT = free_port()
    mock = subprocess.Popen(
        [sys.executable, str(REPO / "tests" / "mock_zai_server.py"), str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PORT):
            print("FATAL: mock server did not start")
            return 1
        exercise()
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock.kill()
    print()
    if _failures:
        print("RESULT: %d FAILURE(S)  (%d passed)" % (len(_failures), _passes[0]))
        for f in _failures:
            print("  - " + f)
        return 1
    print("RESULT: ALL %d CHECKS PASSED" % _passes[0])
    return 0

def exercise():
    OUT = Path("/tmp/zai_test_out")
    shutil.rmtree(OUT, ignore_errors=True)

    # ---------- 1. full export (session C is broken on purpose -> rc 1) ----
    print("== full export ==")
    p = run(["--token", "testtok", "-q"], OUT)
    check(p.returncode == 1, "exit code is 1 when a session has problems (got %d)" % p.returncode)
    check("INCOMPLETE" in p.stderr, "stderr marks the incomplete session")
    check("Attention: 1 session(s) had problems" in p.stderr, "stderr summary counts exactly 1 problem session")

    session_files = sorted((OUT / "sessions").glob("*.jsonl"))
    check(len(session_files) == len(SESSIONS), "one file per session (%d)" % len(session_files))

    index_lines = read_lines(OUT / "index.jsonl")
    idx_recs = [i for i in index_lines if i.get("record_type") == "session_index"]
    check(len(idx_recs) == len(SESSIONS), "index.jsonl has one session_index record per session (%d)" % len(idx_recs))

    # ---------- 2. per-file structure + fidelity ---------------------------
    all_merged_lines = []
    expected_types = {"user_input", "thinking", "model_reply", "tool_call",
                      "tool_result", "attachment", "missing_message"}
    seen_types = set()

    for sf in session_files:
        sid = sf.stem.split("_")[-1]
        full_id = next(k for k in SESSIONS if k.startswith(sid))
        fixture = SESSIONS[full_id]["messages"]

        raw_lines = sf.read_text(encoding="utf-8").splitlines()
        lines = []
        ok_parse = True
        for l in raw_lines:
            try:
                lines.append(json.loads(l))
            except ValueError:
                ok_parse = False
        check(ok_parse, "%s: every line is valid JSON" % sf.name)
        all_merged_lines.extend(raw_lines)

        check(lines[0].get("record_type") == "session", "%s: first line is session record" % sf.name)
        check(lines[-1].get("record_type") == "session_end", "%s: last line is session_end" % sf.name)
        check(all(l.get("record_type") == "event" for l in lines[1:-1]),
              "%s: middle lines are events" % sf.name)
        check(lines[0].get("export_complete") is (full_id.startswith("cccc") is False),
              "%s: export_complete reflects fixture health" % sf.name)

        fidelity_ok, block_ok = True, True
        for ev in lines[1:-1]:
            seen_types.add(ev.get("event"))
            mid = ev.get("message_id")
            src = fixture.get(mid)
            if src is not None and ev.get("raw_original") != src:
                fidelity_ok = False
            rb = ev.get("raw_block")
            if rb is not None:
                blocks = src.get("content_blocks") if isinstance(src, dict) else None
                tcs = src.get("tool_calls") if isinstance(src, dict) else None
                found = (isinstance(blocks, list) and rb in blocks) or \
                        (isinstance(tcs, list) and rb in tcs)
                if not found:
                    block_ok = False
        check(fidelity_ok, "%s: raw_original is verbatim-identical for all events" % sf.name)
        check(block_ok, "%s: raw_block matches exact source block" % sf.name)

        replies_by_msg = {}
        for ev in lines[1:-1]:
            if ev.get("event") == "model_reply" and ev.get("text"):
                replies_by_msg.setdefault(ev.get("message_id"), []).append(ev["text"])
        dup = {m: t for m, t in replies_by_msg.items() if len(t) != len(set(t))}
        check(not dup, "%s: no duplicated model_reply per message" % sf.name)

        idx = next(i for i in idx_recs if i["session_id"] == full_id)
        ev_count = len(lines) - 2
        check(idx["event_count"] == ev_count, "%s: index event_count matches (%d)" % (sf.name, ev_count))

    check(expected_types.issubset(seen_types),
          "all event types present incl. missing_message (missing: %s)"
          % sorted(expected_types - seen_types))

    merged = (OUT / "merged_all.jsonl").read_text(encoding="utf-8").splitlines()
    check(merged == all_merged_lines, "merged_all.jsonl == concatenation of session files")

    # ---------- 3. semantic checks (sessions A and B) ----------------------
    weather = next(f for f in session_files if f.stem.endswith("bbbb2222"))
    wlines = read_lines(weather)
    kinds = [l.get("event") for l in wlines if l.get("record_type") == "event"]
    check("tool_call" in kinds and "tool_result" in kinds,
          "session B: tool_call + tool_result recognized (role=tool msg)")
    call_ev = next(l for l in wlines if l.get("event") == "tool_call")
    check(call_ev["tool"]["name"] == "get_weather"
          and call_ev["tool"]["arguments"] == '{"city": "Baghdad", "unit": "celsius"}',
          "session B: tool name + verbatim arguments parsed")

    sales = next(f for f in session_files if f.stem.endswith("aaaa1111"))
    slines = read_lines(sales)
    think_texts = [l["text"] for l in slines if l.get("event") == "thinking" and l.get("message_id") == "m2"]
    check(len(think_texts) == 2, "session A: both reasoning blocks of m2 captured as thinking")
    calls = [l for l in slines if l.get("event") == "tool_call" and l.get("message_id") == "m2"]
    check([c["tool"]["name"] for c in calls] == ["code_interpreter", "code_interpreter"],
          "session A: 2 tool_calls in original order")
    results = [l for l in slines if l.get("event") == "tool_result" and l.get("message_id") == "m2"]
    check(len(results) == 2 and results[0]["tool"]["id"] == "call_01",
          "session A: tool_results linked by id")
    branch = [l for l in slines if l.get("message_id") == "m3"]
    check(len(branch) == 1 and branch[0]["display_seq"] == 5,
          "session A: branch message m3 ordered after main chain")

    # ---------- 4. session C: missing payload becomes a visible event ------
    broken = next(f for f in session_files if f.stem.endswith("cccc3333"))
    blines = read_lines(broken)
    b_ev = [l for l in blines if l.get("record_type") == "event"]
    check(len(b_ev) == 1 and b_ev[0]["event"] == "missing_message" and b_ev[0]["message_id"] == "x1",
          "session C: unreturned id x1 exported as missing_message event")
    b_idx = next(i for i in idx_recs if i["session_id"].startswith("cccc3333"))
    check(b_idx["export_complete"] is False and b_idx["problems"],
          "session C: index flags export_complete=false with problems")
    check("missing ids: x1" in "".join(b_idx["problems"]),
          "session C: problems name the missing id")

    # ---------- 5. --resume skips unchanged, re-exports broken -------------
    print("== resume ==")
    p2 = run(["--token", "testtok", "-q", "--resume"], OUT)
    check(p2.returncode == 1, "resume exit code still 1 (broken session re-checked)")
    check(p2.stderr.count("skipped (--resume)") == 2, "resume skipped exactly the 2 healthy sessions")
    check("Broken session" in p2.stderr and "skipped" not in
          p2.stderr.split("Broken session")[1].split("\n")[1],
          "resume re-exported the broken session")
    index2 = read_lines(OUT / "index.jsonl")
    check(len([i for i in index2 if i.get("record_type") == "session_index"]) == 3,
          "resume keeps 3 session_index records")
    merged2 = (OUT / "merged_all.jsonl").read_text(encoding="utf-8").splitlines()
    concat2 = []
    for sf in sorted((OUT / "sessions").glob("*.jsonl")):
        concat2.extend(sf.read_text(encoding="utf-8").splitlines())
    check(merged2 == concat2, "resume keeps merged_all.jsonl a full concatenation")

    # ---------- 6. --session direct fetch (no listing) ----------------------
    print("== --session direct ==")
    OUTD = Path("/tmp/zai_test_direct")
    shutil.rmtree(OUTD, ignore_errors=True)
    a_id = next(k for k in SESSIONS if k.startswith("aaaa"))
    b_id = next(k for k in SESSIONS if k.startswith("bbbb"))
    p3 = run(["--token", "testtok", "-q", "--session", a_id, "--session", b_id], OUTD)
    check(p3.returncode == 0, "direct fetch of 2 healthy sessions exits 0")
    check("directly" in p3.stderr and "Listing sessions" not in p3.stderr,
          "direct mode skips the listing phase")
    names = sorted(f.name for f in (OUTD / "sessions").glob("*.jsonl"))
    check(names == ["001_Sales_trend_analysis_&_chart_aaaa1111.jsonl",
                    "002_Weather_check_—_Baghdad_bbbb2222.jsonl"],
          "direct mode preserves requested order in filenames: %s" % names)

    print("== --session with unknown id ==")
    OUTU = Path("/tmp/zai_test_unknown")
    shutil.rmtree(OUTU, ignore_errors=True)
    p4 = run(["--token", "testtok", "-q", "--session", "deadbeef-0000", "--session", b_id], OUTU)
    check(p4.returncode == 1, "unknown session id fails the run (rc 1)")
    check("unavailable" in p4.stderr and "deadbeef-0000" in p4.stderr,
          "unknown id warned with its value")
    check(len(list((OUTU / "sessions").glob("*.jsonl"))) == 1,
          "healthy requested session still exported alongside unknown id")

    # ---------- 7. --list goes to stdout ------------------------------------
    print("== --list ==")
    p5 = run(["--token", "testtok", "--list"], Path("/tmp/zai_test_list_unused"))
    check(p5.returncode == 0, "--list exits 0")
    check("Weather check" in p5.stdout and "Broken session" in p5.stdout,
          "--list prints session titles to stdout (scriptable)")
    check(not (Path("/tmp/zai_test_list_unused") / "sessions").exists(),
          "--list does not create output files")

    # ---------- 8. flag validation ------------------------------------------
    print("== flag validation ==")
    p6 = run(["--token", "testtok", "--from", "2026/01/01"], Path("/tmp/zai_test_baddate"))
    check(p6.returncode == 2, "invalid --from date rejected with rc 2")
    check("expected YYYY-MM-DD" in p6.stderr, "invalid date error explains the format")

    OUTG = Path("/tmp/zai_test_guest")
    shutil.rmtree(OUTG, ignore_errors=True)
    p7 = run(["--guest", "--token", "testtok", "-q"], OUTG)
    check("--guest wins" in p7.stderr, "--guest + --token conflict warns that --guest wins")

    # ---------- 9. unit checks on parsing internals -------------------------
    print("== unit checks ==")
    msg_err = {"id": "t1", "role": "assistant",
               "content_blocks": [{"type": "tool_result", "tool_call_id": "c1",
                                   "is_error": True, "content": "boom"}]}
    ev = zx.extract_events(msg_err, 1)[0]
    check(ev["tool"]["status"] == "error", "is_error=true maps to status 'error'")
    msg_noerr = {"id": "t2", "role": "assistant",
                 "content_blocks": [{"type": "tool_result", "tool_call_id": "c2",
                                     "is_error": False, "content": "fine"}]}
    ev2 = zx.extract_events(msg_noerr, 1)[0]
    check(ev2["tool"]["status"] is None, "is_error=false maps to status null (not false)")
    msg_st = {"id": "t3", "role": "assistant",
              "content_blocks": [{"type": "tool_result", "tool_call_id": "c3",
                                  "status": "success", "content": "ok"}]}
    ev3 = zx.extract_events(msg_st, 1)[0]
    check(ev3["tool"]["status"] == "success", "explicit status preserved verbatim")

    msg_dedup = {"id": "t4", "role": "assistant",
                 "content_blocks": [{"type": "text", "content": "Hello  world"}],
                 "content": "Hello world"}
    replies = [e for e in zx.extract_events(msg_dedup, 1) if e["event"] == "model_reply"]
    check(len(replies) == 1, "whitespace-variant content deduped to one model_reply")

    class FakeClient:
        def list_chats_page(self, page, chat_type):
            return {"foo": 1}
    try:
        zx.list_all_chats(FakeClient(), "default", quiet=True)
        check(False, "unknown chat-list dict shape raises ApiError")
    except zx.ApiError:
        check(True, "unknown chat-list dict shape raises ApiError")

    chain = {}
    for i in range(500):
        chain["m%d" % i] = {"id": "m%d" % i, "timestamp": 1000 + i,
                            "parent_id": None if i == 0 else "m%d" % (i - 1),
                            "children_ids": [] if i == 499 else ["m%d" % (i + 1)]}
    ordered, roots = zx.readable_order(chain)
    check(ordered == ["m%d" % i for i in range(500)] and roots == 1,
          "readable_order handles a 500-message chain correctly (fast O(n))")

    for bad in ("2026/01/01", "2026-13-01", "not-a-date"):
        try:
            zx._valid_date(bad)
            check(False, "invalid date %r rejected" % bad)
        except Exception:
            check(True, "invalid date %r rejected" % bad)
    check(zx._valid_date("2026-01-01") == "2026-01-01", "valid date accepted")

if __name__ == "__main__":
    sys.exit(main())

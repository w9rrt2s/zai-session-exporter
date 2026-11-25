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
    for i in range(1500):
        chain["m%d" % i] = {"id": "m%d" % i, "timestamp": 1000 + i,
                            "parent_id": None if i == 0 else "m%d" % (i - 1),
                            "children_ids": [] if i == 1499 else ["m%d" % (i + 1)]}
    ordered, roots = zx.readable_order(chain)
    check(ordered == ["m%d" % i for i in range(1500)] and roots == 1,
          "readable_order handles a 1500-message chain (iterative — no RecursionError, fast O(n))")

    for bad in ("2026/01/01", "2026-13-01", "not-a-date"):
        try:
            zx._valid_date(bad)
            check(False, "invalid date %r rejected" % bad)
        except Exception:
            check(True, "invalid date %r rejected" % bad)
    check(zx._valid_date("2026-01-01") == "2026-01-01", "valid date accepted")

    # ---------- 10. --format md ------------------------------------------
    print("== format md ==")
    OUTM = Path("/tmp/zai_test_md")
    shutil.rmtree(OUTM, ignore_errors=True)
    pm = run(["--token", "testtok", "-q", "--format", "md"], OUTM)
    check(pm.returncode == 1, "md export keeps rc 1 for the broken session")
    mds = sorted((OUTM / "sessions").glob("*.md"))
    check(len(mds) == len(SESSIONS), "one .md file per session (%d)" % len(mds))
    wmd = next(f for f in mds if f.stem.endswith("bbbb2222")).read_text(encoding="utf-8")
    check("# Weather check" in wmd, "md: session title is the H1")
    check("### [" in wmd and "user_input" in wmd and "tool_call" in wmd and "tool_result" in wmd,
          "md: ordered event headings with types")
    check("get_weather" in wmd and "```" in wmd, "md: tool name + fenced verbatim payload")
    smd = next(f for f in mds if f.stem.endswith("aaaa1111")).read_text(encoding="utf-8")
    check("> The user wants a trend chart" in smd, "md: thinking rendered as blockquote")
    check("Export complete: yes" in wmd and "Export complete: NO" in
          next(f for f in mds if f.stem.endswith("cccc3333")).read_text(encoding="utf-8"),
          "md: completeness stated per session")
    idxm = [r for r in read_lines(OUTM / "index.jsonl") if r.get("record_type") == "session_index"]
    check(all(r.get("format") == "md" for r in idxm), "md: index records tagged format=md")
    check(not (OUTM / "merged_all.jsonl").exists(),
          "md: merged_all.jsonl deliberately not produced")
    check("produced only with --format jsonl" in pm.stderr, "md: merge policy note printed")
    summ = json.loads((OUTM / "export_summary.json").read_text(encoding="utf-8"))
    check(summ.get("format") == "md" and summ.get("requested") == len(SESSIONS)
          and summ.get("exported_ok") == len(SESSIONS) - 1 and summ.get("incomplete"),
          "md: export_summary.json machine-readable and accurate")

    # ---------- 11. --format html -----------------------------------------
    print("== format html ==")
    OUTH = Path("/tmp/zai_test_html")
    shutil.rmtree(OUTH, ignore_errors=True)
    ph = run(["--token", "testtok", "-q", "--format", "html"], OUTH)
    check(ph.returncode == 1, "html export keeps rc 1 for the broken session")
    hts = sorted((OUTH / "sessions").glob("*.html"))
    check(len(hts) == len(SESSIONS), "one .html viewer per session (%d)" % len(hts))
    wht = next(f for f in hts if f.stem.endswith("bbbb2222")).read_text(encoding="utf-8")
    check(wht.lstrip().lower().startswith("<!doctype html>"), "html: starts with doctype")
    check('dir="auto"' in wht, "html: RTL-safe auto direction on text bodies")
    check("get_weather" in wht and "Filter events" in wht and "</html>" in wht,
          "html: tool label, live filter, complete document")
    check("__TITLE__" not in wht and "__CARDS__" not in wht and "__META__" not in wht
          and "__FOOTER__" not in wht, "html: no unresolved template placeholders")
    check(wht.count("class=\"ev") == 4, "html: weather session renders 4 event cards")
    check(len(wht.encode("utf-8")) > 2000, "html: viewer is a substantial self-contained file")
    # every user-controlled string must be escaped: raw fixture text with <> would break it
    check("<script>" in wht and wht.count("<script>") == 1, "html: exactly one script block")

    # ---------- 12. --events filter ---------------------------------------
    print("== --events filter ==")
    OUTE = Path("/tmp/zai_test_events")
    shutil.rmtree(OUTE, ignore_errors=True)
    pe = run(["--token", "testtok", "-q", "--events", "model_reply,user_input"], OUTE)
    check(pe.returncode == 1, "events filter keeps rc 1 (broken session still probed)")
    wef = next(f for f in (OUTE / "sessions").glob("*.jsonl") if f.stem.endswith("bbbb2222"))
    elines = read_lines(wef)
    evs = [l for l in elines if l.get("record_type") == "event"]
    check(all(l.get("event") in ("model_reply", "user_input") for l in evs),
          "events filter: only requested event types in file")
    check(evs and evs[0]["event_seq"] == 1 and [l["event_seq"] for l in evs] == list(range(1, len(evs) + 1)),
          "events filter: event_seq stays contiguous")
    idxe = [r for r in read_lines(OUTE / "index.jsonl") if r.get("record_type") == "session_index"]
    check(all(r.get("events") == "model_reply,user_input" for r in idxe),
          "events filter: index records carry the filter tag")
    check(all(r["event_count"] == sum(r["event_counts"].values()) for r in idxe),
          "events filter: counts consistent after filtering")

    print("== --events validation ==")
    pbad = run(["--token", "testtok", "--events", "nope"], Path("/tmp/zai_test_badev"))
    check(pbad.returncode == 2, "unknown --events value rejected with rc 2")
    check("valid types" in pbad.stderr, "--events error lists the valid types")

    # ---------- 13. --search ------------------------------------------------
    print("== --search ==")
    OUTS = Path("/tmp/zai_test_search")
    shutil.rmtree(OUTS, ignore_errors=True)
    ps = run(["--token", "testtok", "-q", "--search", "weather"], OUTS)
    check(ps.returncode == 0, "--search narrows scope to healthy session only (rc 0)")
    check(len(list((OUTS / "sessions").glob("*.jsonl"))) == 1
          and "bbbb2222" in next(iter((OUTS / "sessions").glob("*.jsonl"))).name,
          "--search exported exactly the matching session")

    # ---------- 14. --workers (concurrent fetch) ----------------------------
    print("== --workers 2 ==")
    OUTW = Path("/tmp/zai_test_workers")
    shutil.rmtree(OUTW, ignore_errors=True)
    pw = run(["--token", "testtok", "-q", "--workers", "2"], OUTW)
    check(pw.returncode == 1, "workers=2 keeps exit-code semantics (rc 1)")
    check(len(list((OUTW / "sessions").glob("*.jsonl"))) == len(SESSIONS),
          "workers=2 produced all session files")
    idxw = [r for r in read_lines(OUTW / "index.jsonl") if r.get("record_type") == "session_index"]
    check([r["session_id"] for r in idxw] == [r["session_id"] for r in idx_recs],
          "workers=2: index order matches serial order (deterministic)")
    mw = (OUTW / "merged_all.jsonl").read_text(encoding="utf-8").splitlines()
    concatw = []
    for sf in sorted((OUTW / "sessions").glob("*.jsonl")):
        concatw.extend(sf.read_text(encoding="utf-8").splitlines())
    check(mw == concatw, "workers=2: merged_all.jsonl still a full concatenation")
    check(json.loads((OUTW / "export_summary.json").read_text(encoding="utf-8")).get("workers") == 2,
          "workers=2: summary records worker count")

    # ---------- 15. --resume respects format/event-filter changes -----------
    print("== resume format guard ==")
    pr = run(["--token", "testtok", "-q", "--format", "md", "--resume"], OUT)
    check(pr.stderr.count("skipped (--resume)") == 0,
          "resume with a different --format re-exports everything (no false skips)")
    check(len(list((OUT / "sessions").glob("*.md"))) == len(SESSIONS),
          "resume+md produced the md files")

    # ---------- 16. expired token hint ---------------------------------------
    print("== 401 hint ==")
    OUTX = Path("/tmp/zai_test_expired")
    shutil.rmtree(OUTX, ignore_errors=True)
    px = run(["--token", "expired-token", "-q"], OUTX)
    check(px.returncode == 2, "expired token exits rc 2")
    check("401" in px.stderr and "expired" in px.stderr,
          "401 produces a dedicated expired-token hint")

    # ---------- 17. --list phantom column removed ----------------------------
    print("== --list column fix ==")
    p5b = run(["--token", "testtok", "--list"], Path("/tmp/zai_test_list2"))
    check("? msg(s)" not in p5b.stdout, "--list no longer prints a fake message-count column")
    check("Weather check" in p5b.stdout, "--list still lists titles")

    # ---------- 18. unit: dump_line splice equivalence -----------------------
    print("== unit: line splicing ==")
    prefix = {"record_type": "event", "a": 1, "raw_original": {"x": 1}, "b": "two"}
    rj = json.dumps({"x": 1}, ensure_ascii=False)
    line = zx.dump_line(prefix, rj)
    parsed = json.loads(line)
    check(parsed == {"record_type": "event", "a": 1, "raw_original": {"x": 1}, "b": "two"},
          "dump_line spliced output is semantically identical")
    check(list(parsed.keys()) == ["record_type", "a", "b", "raw_original"],
          "dump_line moves raw_original to the end, preserving the other keys' order")
    check(json.loads(zx.dump_line(prefix, None)) == parsed,
          "dump_line plain path matches spliced path")

    # ---------- 19. unit: root count on a branched tree ----------------------
    branched = {"a": {"id": "a", "parent_id": None},
                "b": {"id": "b", "parent_id": "a"},
                "c": {"id": "c", "parent_id": "a"}}
    ordered_b, roots_b = zx.readable_order(branched)
    check(ordered_b == ["a", "b", "c"] and roots_b == 1,
          "readable_order root count correct on a branched tree (1 root, was 2)")

    # ---------- 20. unit: duplicate listing ids de-duplicated -----------------
    class DupClient:
        def __init__(self):
            self.calls = 0
        def list_chats_page(self, page, chat_type):
            self.calls += 1
            item = {"id": "dup-1", "title": "dup", "type": "default"}
            return [item, dict(item)] if page == 1 else []
    dupes = zx.list_all_chats(DupClient(), "default", quiet=True)
    check(len(dupes) == 1, "duplicate session ids in listing are de-duplicated")

if __name__ == "__main__":
    sys.exit(main())

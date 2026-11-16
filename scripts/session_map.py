#!/usr/bin/env python3
"""session-map: render Claude Code sessions as a subway-style line map.

The main session is the trunk line, subagents branch off and rejoin, linked
sessions (started from it, or messaging it) run alongside, and milestones
(commits, PRs, test runs, published artifacts) sit on the line where they
happened. The horizontal axis is scaled to working time.

Usage:
  session_map.py --list [N]                     recent sessions with titles
  session_map.py SELECTOR [...] [-o PATH] [--open]
  session_map.py --publish-status               unknown, ok or blocked (Artifacts disallowed)
  session_map.py --history                      maps written and published so far
  session_map.py --purge [--older-than DAYS] [--include-saved] [--yes]

SELECTOR is any of: current | a session UUID or prefix | a desktop id
(local_...) | a path to a .jsonl transcript | part of a session title.
Each selector becomes a tab in the page.
"""
import argparse, glob, json, os, re, sys
from datetime import datetime

HOME = os.path.expanduser("~")
PROJECTS = os.environ.get("SESSION_MAP_PROJECTS") or os.path.join(HOME, ".claude", "projects")
DESKTOP = os.environ.get("SESSION_MAP_DESKTOP") or os.path.join(HOME, "Library", "Application Support", "Claude", "claude-code-sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.normpath(os.path.join(HERE, "..", "assets", "template.html"))

MAIN_IDLE_CAP = 15 * 60   # one gap longer than this inside a turn counts as idle
AGENT_GAP = 10 * 60       # pause longer than this splits a subagent's working stretch

# $ per million tokens: input, output, cache read. Cache writes are 1.25x input
# (5-minute) and 2x input (1-hour). Source: Claude API reference, cached 2026-11-03.
PRICES_AS_OF = "2026-11-03"
PRICES = {
    "claude-fable-5-2": (8, 40, 0.20), "claude-mythos-5-2": (8, 40, 0.20),
    "claude-fable-5-1": (10, 50, 0.25), "claude-mythos-5-1": (10, 50, 0.25),
    "claude-fable-5": (10, 50, 1.0), "claude-mythos-5": (10, 50, 1.0),
    "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.5),
    "claude-opus-4-8": (5, 25, 0.5), "claude-opus-4-7": (5, 25, 0.5), "claude-opus-4-6": (5, 25, 0.5),
    "claude-sonnet-5-5": (2, 10, 0.10), "claude-sonnet-5": (2, 10, 0.2), "claude-sonnet-4-6": (3, 15, 0.3),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1, 5, 0.1),
}

TEST_RE = re.compile(r"(?<![\w-])(pytest|jest|vitest|mocha|go test|cargo test|swift test|xcodebuild\b[^|;&]*\btest\b|"
                     r"(?:npm|pnpm|yarn|bun)(?: run)? test|npx playwright test|rspec|phpunit|tox|make test|rake test|turbo(?: run)? test)\b")
COMMIT_RE = re.compile(r"(?<![\w-])git\s+(?:-C\s+\S+\s+)?commit\b")
LOCAL_RE = re.compile(r"(?<![\w-])(local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    return out


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "tool_result":
                parts.append(text_of(c.get("content")))
    return "\n".join(parts)


def clean(t, n):
    t = re.sub(r"<system-reminder>.*?</system-reminder>", "", t or "", flags=re.S)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]


# ---------------------------------------------------------------- pricing
def price_key(model):
    m = re.match(r"(claude-[a-z]+-\d+(?:-\d{1,2})?)", model or "")
    if not m:
        return None
    k = m.group(1)
    if k in PRICES:
        return k
    k2 = re.match(r"(claude-[a-z]+-\d+)", k).group(1)
    return k2 if k2 in PRICES else None


def cost_of(usage, model):
    k = price_key(model)
    if not k or not usage:
        return 0.0
    rin, rout, rread = PRICES[k]
    inp = usage.get("input_tokens") or 0
    cr = usage.get("cache_read_input_tokens") or 0
    cc = usage.get("cache_creation_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    br = usage.get("cache_creation") or {}
    c1 = br.get("ephemeral_1h_input_tokens") or 0
    c5 = br.get("ephemeral_5m_input_tokens")
    c5 = (cc - c1) if c5 is None else c5
    if k == "claude-haiku-5-5" and inp + cr + cc > 100_000:
        rin, rout, rread = 0.50, 2.50, 0.05
    mult = 2.0 if usage.get("speed") == "fast" else 1.0
    return mult * (inp * rin + c5 * rin * 1.25 + c1 * rin * 2 + cr * rread + out * rout) / 1e6


# ---------------------------------------------------------------- line analysis
def classify(e):
    if e["type"] == "assistant":
        return "assistant"
    to = e.get("turnOrigin")
    o = e.get("origin") or {}
    if to == "human" or o.get("kind") == "human":
        return "human"
    if to == "peer" or o.get("kind") == "peer":
        return "peer"
    if to == "task_notification" or o.get("kind") == "task-notification":
        return "notify"
    c = e.get("message", {}).get("content")
    if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
        return "tool_result"
    if isinstance(c, str) and ("<command-name>" in c or "<local-command" in c):
        return "local"
    if isinstance(c, str) and "<side-sessions-event>" in c:
        return "sidenote"
    return "other"


def messages(rows):
    """Deduplicated assistant API responses with tokens and cost."""
    by_id = {}
    for r in rows:
        if r.get("type") != "assistant" or not r.get("timestamp"):
            continue
        m = r.get("message") or {}
        mid = m.get("id") or r.get("uuid")
        u = m.get("usage") or {}
        cur = by_id.get(mid)
        if cur is None:
            by_id[mid] = {"t": ts(r["timestamp"]), "model": m.get("model"), "usage": u}
        elif (u.get("output_tokens") or 0) > (cur["usage"].get("output_tokens") or 0):
            cur["usage"] = u
    out = []
    for v in by_id.values():
        if not v["model"] or v["model"].startswith("<"):
            continue
        u = v["usage"]
        v["cost"] = cost_of(u, v["model"])
        v["tok"] = [
            (u.get("input_tokens") or 0),
            (u.get("cache_creation_input_tokens") or 0),
            (u.get("cache_read_input_tokens") or 0),
            (u.get("output_tokens") or 0),
        ]
        out.append(v)
    out.sort(key=lambda v: v["t"])
    return out


def token_totals(msgs):
    tot = [0, 0, 0, 0]
    for m in msgs:
        for i in range(4):
            tot[i] += m["tok"][i]
    return {"input": tot[0], "cacheWrite": tot[1], "cacheRead": tot[2], "output": tot[3]}


def model_changes(msgs):
    out, last = [], None
    for m in msgs:
        if m["model"] != last:
            out.append([m["t"], m["model"]])
            last = m["model"]
    return out


def seg_costs(segs, msgs):
    costs = [0.0] * len(segs)
    if not segs:
        return costs
    for m in msgs:
        best, bd = 0, None
        for i, (a, b) in enumerate(segs):
            d = 0 if a <= m["t"] <= b else min(abs(m["t"] - a), abs(m["t"] - b))
            if bd is None or d < bd:
                best, bd = i, d
                if d == 0:
                    break
        costs[best] += m["cost"]
    return [round(c, 4) for c in costs]


def merge(iv, tol=1):
    iv = sorted(iv)
    out = []
    for a, b in iv:
        if out and a <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def main_segments(evs):
    work, wait_user, prev = [], 0.0, None
    for e in evs:
        k = classify(e)
        t = ts(e["timestamp"])
        if prev is not None:
            gap = t - prev
            if k == "human":
                wait_user += gap
            elif k in ("peer", "notify", "local", "other", "sidenote"):
                pass
            elif gap <= MAIN_IDLE_CAP:
                work.append([prev, t])
        prev = t
    segs = merge(work)
    return segs, wait_user


def cluster_segments(times):
    times = sorted(times)
    if not times:
        return []
    segs = [[times[0], times[0]]]
    for t in times[1:]:
        if t - segs[-1][1] > AGENT_GAP:
            segs.append([t, t])
        else:
            segs[-1][1] = t
    return segs


def commit_message(cmd, out):
    m = re.search(r"\[([^\]\s]+) (?:\(root-commit\) )?([0-9a-f]{7,40})\] (.+)", out or "")
    if m:
        return m.group(3).strip()[:90], m.group(2)
    m = re.search(r"<<\s*'?\"?EOF'?\"?\s*\n(.*?)\n", cmd, re.S) or re.search(r"-m\s+\"([^\"]+)\"", cmd) or re.search(r"-m\s+'([^']+)'", cmd)
    return ((m.group(1).strip()[:90]) if m else "commit"), None


def milestones(rows):
    pending, out, seen_pr = {}, [], set()
    rows = sorted([r for r in rows if r.get("timestamp")], key=lambda r: r["timestamp"])
    for r in rows:
        t = ts(r["timestamp"])
        if r.get("type") == "pr-link" and r.get("prUrl") and r["prUrl"] not in seen_pr:
            seen_pr.add(r["prUrl"])
            out.append({"t": t, "kind": "pr", "label": f"PR #{r.get('prNumber')} · {r.get('prRepository', '')}", "url": r["prUrl"]})
            continue
        if r.get("type") == "assistant":
            for c in (r.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    pending[c["id"]] = (c.get("name"), c.get("input") or {})
            continue
        if r.get("type") != "user":
            continue
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_result" or c.get("tool_use_id") not in pending:
                continue
            name, inp = pending.pop(c["tool_use_id"])
            err = bool(c.get("is_error"))
            res = text_of(c.get("content")) if not isinstance(c.get("content"), str) else c["content"]
            if name == "Bash":
                cmd = inp.get("command") or ""
                if COMMIT_RE.search(cmd) and not err and "nothing to commit" not in res:
                    msg, h = commit_message(cmd, res)
                    out.append({"t": t, "kind": "commit", "label": msg, "hash": h})
                if re.search(r"(?<![\w-])gh\s+pr\s+create\b", cmd) and not err:
                    u = re.search(r"https://github\.com/\S+?/pull/\d+", res)
                    if u and u.group(0) not in seen_pr:
                        seen_pr.add(u.group(0))
                        out.append({"t": t, "kind": "pr", "label": "Opened " + u.group(0).split("github.com/")[1], "url": u.group(0)})
                m = TEST_RE.search(cmd)
                if m:
                    out.append({"t": t, "kind": "testfail" if err else "testpass", "label": clean(m.group(0), 60) + (" failed" if err else " passed")})
            elif name == "Artifact" and not err and inp.get("action") in (None, "publish") and not inp.get("asset") and (inp.get("file_path") or inp.get("type_url")):
                u = re.search(r"https://claude\.ai/(?:code/)?artifact/[\w-]+", res)
                label = os.path.basename(inp.get("file_path") or "") or inp.get("title") or "artifact"
                out.append({"t": t, "kind": "artifact", "label": "Published " + label, "url": u.group(0) if u else None})
    return out


def analyze_agents(path, sid, evs, agent_calls, sends):
    notify_at, launch_of = {}, {}
    for e in evs:
        if classify(e) == "notify":
            m = re.search(r"<task-id>([a-z0-9]+)</task-id>", text_of(e["message"]["content"]) or json.dumps(e["message"]["content"]))
            if m:
                notify_at.setdefault(m.group(1), []).append(ts(e["timestamp"]))
        tur = e.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("agentId"):
            c = e["message"]["content"]
            tid = next((x.get("tool_use_id") for x in c if isinstance(x, dict) and x.get("tool_use_id")), None) if isinstance(c, list) else None
            launch_of[tur["agentId"]] = {"tid": tid, "async": bool(tur.get("isAsync")), "model": tur.get("resolvedModel")}
    agents = []
    for f in sorted(glob.glob(os.path.join(os.path.dirname(path), sid, "subagents", "agent-*.jsonl"))):
        aid = os.path.basename(f)[6:-6]
        meta = {}
        mp = f[:-6] + ".meta.json"
        if os.path.exists(mp):
            try:
                meta = json.load(open(mp))
            except Exception:
                pass
        rows = load(f)
        ar = [r for r in rows if r.get("type") in ("user", "assistant") and r.get("timestamp")]
        if not ar:
            continue
        segs = cluster_segments([ts(r["timestamp"]) for r in ar])
        msgs = messages(rows)
        lo = launch_of.get(aid, {})
        call = agent_calls.get(meta.get("toolUseId") or lo.get("tid"), {})
        joins = sorted(notify_at.get(aid, []))
        ntools = sum(1 for r in ar if r["type"] == "assistant" for c in ((r.get("message") or {}).get("content") or []) if isinstance(c, dict) and c.get("type") == "tool_use")
        mc = model_changes(msgs)
        agents.append({
            "id": aid,
            "desc": meta.get("description") or call.get("desc") or aid,
            "type": meta.get("agentType") or call.get("type") or "",
            "model": (mc[0][1] if mc else None) or lo.get("model"),
            "models": mc,
            "bg": (meta.get("requestShape") == "background") or lo.get("async", False),
            "start": min(call.get("t", segs[0][0]), segs[0][0]),
            "end": max(segs[-1][1], joins[-1] if joins else 0),
            "segs": segs, "segCost": seg_costs(segs, msgs),
            "active": sum(b - a for a, b in segs),
            "tools": ntools,
            "joins": joins,
            "resumes": [s["t"] for s in sends if s["to"] == aid],
            "tokens": token_totals(msgs), "cost": round(sum(m["cost"] for m in msgs), 4),
            "milestones": milestones(rows),
        })
    agents.sort(key=lambda a: a["start"])
    return agents


# ---------------------------------------------------------------- map-making turns
SKILL_RE = re.compile(r"(?:^|:)session-map$")


def is_map_signal(e):
    """True when this event starts session-map work: the /session-map command, the Skill tool
    invoking it, or the skill's instructions being loaded. Running the script by hand doesn't count,
    so developing this skill still shows up as real work."""
    c = (e.get("message") or {}).get("content")
    if e["type"] == "assistant":
        return any(isinstance(x, dict) and x.get("type") == "tool_use" and x.get("name") == "Skill"
                   and SKILL_RE.search(str((x.get("input") or {}).get("skill", ""))) for x in c or [])
    # Only the user's own message or the skill text the harness injects, never tool output that
    # happens to quote these strings (reading a transcript that used the skill, say).
    if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
        return False
    txt = c if isinstance(c, str) else text_of(c)
    if classify(e) == "human" and "<command-name>/session-map</command-name>" in txt:
        return True
    return txt.lstrip().startswith("Base directory for this skill:") and re.search(r"skills/session-map\b", txt[:300]) is not None


def map_spans(evs):
    """[start, end] spans from a map signal to the end of its turn."""
    spans, open_at, prev = [], None, None
    for e in evs:
        t = ts(e["timestamp"])
        if classify(e) == "human" and open_at is not None:
            spans.append([open_at, prev])
            open_at = None
        if open_at is None and is_map_signal(e):
            open_at = t
        prev = t
    if open_at is not None:
        spans.append([open_at, prev])
    return spans


def in_spans(t, spans):
    return any(a <= t <= b for a, b in spans)


def subtract(segs, spans):
    out = []
    for a, b in segs:
        pieces = [[a, b]]
        for x, y in spans:
            nxt = []
            for p, q in pieces:
                if y < p or x > q:
                    nxt.append([p, q])
                    continue
                if p < x:
                    nxt.append([p, x])
                if y < q:
                    nxt.append([y, q])
            pieces = nxt
        out.extend(pieces)
    return out


def analyze_session(path, with_agents=True, include_map_work=False):
    rows = load(path)
    sid = os.path.basename(path)[:-6]
    title, cwd = None, None
    for r in rows:
        if r.get("type") == "custom-title" and r.get("customTitle"):
            title = r["customTitle"]
        if not cwd and r.get("cwd") and "/scratch-workspaces/" not in r["cwd"]:
            cwd = r["cwd"]
    evs = sorted([r for r in rows if r.get("type") in ("user", "assistant") and r.get("timestamp") and not r.get("isSidechain")], key=lambda r: r["timestamp"])
    if not evs:
        return None
    segs, wait_user = main_segments(evs)
    msgs = messages([r for r in rows if not r.get("isSidechain")])
    spans = [] if include_map_work else map_spans(evs)
    excluded = []
    if spans:
        for a, b in spans:
            excluded.append({"start": a, "end": b,
                             "active": sum(max(0, min(q, b) - max(p, a)) for p, q in segs),
                             "cost": round(sum(m["cost"] for m in msgs if a <= m["t"] <= b), 4)})
        segs = subtract(segs, spans)
        msgs = [m for m in msgs if not in_spans(m["t"], spans)]
    stations, peers, sends, tools, agent_calls, starts, notes = [], [], [], {}, {}, [], []
    pending = {}
    for e in evs:
        k = classify(e)
        t = ts(e["timestamp"])
        if spans and in_spans(t, spans):
            continue
        if k == "human":
            stations.append({"t": t, "text": clean(text_of(e["message"]["content"]), 160)})
        elif k == "peer":
            o = e.get("origin") or {}
            peers.append({"t": t, "local": o.get("fromSession"), "name": o.get("name") or "another session", "text": clean(o.get("body") or "", 200)})
        elif k == "sidenote":
            txt = text_of(e["message"]["content"])
            for lid in set(LOCAL_RE.findall(txt)):
                notes.append({"t": t, "local": lid})
        elif k == "assistant":
            for c in e["message"].get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                n = c["name"]
                tools[n] = tools.get(n, 0) + 1
                inp = c.get("input") or {}
                pending[c["id"]] = n
                if n in ("Agent", "Task"):
                    agent_calls[c["id"]] = {"t": t, "desc": inp.get("description", ""), "type": inp.get("subagent_type", "")}
                elif n == "SendMessage" or n.endswith("__send_message"):
                    sends.append({"t": t, "to": inp.get("to") or inp.get("session_id") or "", "text": clean(inp.get("summary") or inp.get("message") or "", 200)})
        elif k == "tool_result":
            for c in e["message"]["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result" and pending.get(c.get("tool_use_id"), "").endswith("start_session"):
                    for lid in set(LOCAL_RE.findall(text_of(c.get("content")) if not isinstance(c.get("content"), str) else c["content"])):
                        starts.append({"t": t, "local": lid})
    agents = analyze_agents(path, sid, evs, agent_calls, sends) if with_agents else []
    if spans:
        agents = [a for a in agents if not in_spans(a["start"], spans)]
    main_ms = [m for m in milestones([r for r in rows if not r.get("isSidechain")]) if not in_spans(m["t"], spans)]
    return {
        "id": sid, "path": path, "title": title or sid[:8], "project": os.path.basename(cwd) if cwd else os.path.basename(os.path.dirname(path)),
        "t0": ts(evs[0]["timestamp"]), "t1": ts(evs[-1]["timestamp"]),
        "main": {"segs": segs, "segCost": seg_costs(segs, msgs), "active": sum(b - a for a, b in segs), "waitUser": wait_user,
                 "tools": tools, "tokens": token_totals(msgs), "cost": round(sum(m["cost"] for m in msgs), 4),
                 "models": model_changes(msgs), "milestones": main_ms, "excluded": excluded},
        "stations": stations, "agents": agents,
        "_peers": peers, "_sends": sends, "_starts": starts, "_notes": notes,
    }


# ---------------------------------------------------------------- linked sessions
_IDX = None


def desktop_index():
    global _IDX
    if _IDX is None:
        _IDX = {}
        for f in glob.glob(os.path.join(DESKTOP, "*", "*", "local_*.json")):
            try:
                j = json.load(open(f))
            except Exception:
                continue
            if j.get("sessionId") and j.get("cliSessionId"):
                _IDX[j["sessionId"]] = {"cli": j["cliSessionId"], "title": j.get("title") or "", "last": j.get("lastActivityAt") or 0, "cwd": j.get("cwd")}
    return _IDX


def transcript_for(cli):
    hits = glob.glob(os.path.join(PROJECTS, "*", cli + ".jsonl"))
    return hits[0] if hits else None


def link_sessions(s, include_map_work=False):
    idx = desktop_index()
    own = {l for l, v in idx.items() if v["cli"] == s["id"]}
    agent_ids = {a["id"] for a in s["agents"]}
    by_title = {}
    for l, v in idx.items():
        by_title.setdefault(v["title"], []).append(l)
    links, unresolved = {}, []

    def add(lid, ev):
        if lid and lid not in own and lid in idx:
            links.setdefault(lid, []).append(ev)
            return True
        return False

    for p in s["_peers"]:
        if not add(p["local"], {"t": p["t"], "dir": "in", "text": p["text"]}):
            unresolved.append({"t": p["t"], "from": p["name"], "text": p["text"]})
    for m in s["_sends"]:
        to = m["to"]
        if to in agent_ids:
            continue
        lid = to if to.startswith("local_") else None
        if not lid and to in by_title:
            cands = sorted(by_title[to], key=lambda l: -idx[l]["last"])
            lid = cands[0]
        add(lid, {"t": m["t"], "dir": "out", "text": m["text"]})
    for st in s["_starts"]:
        add(st["local"], {"t": st["t"], "dir": "start", "text": "Started from this session"})
    for n in s["_notes"]:
        add(n["local"], {"t": n["t"], "dir": "in", "text": "Report from this session"})

    out = []
    for lid, evs in links.items():
        p = transcript_for(idx[lid]["cli"])
        if not p:
            continue
        ls = analyze_session(p, with_agents=True, include_map_work=include_map_work)
        if not ls:
            continue
        lo, hi = s["t0"], max(s["t1"], max(a["end"] for a in s["agents"]) if s["agents"] else s["t1"])
        segs = [[max(a, lo), min(b, hi)] for a, b in ls["main"]["segs"] if b >= lo and a <= hi]
        msgs_cost = ls["main"]["segCost"]
        seg_cost = [c for (a, b), c in zip(ls["main"]["segs"], msgs_cost) if b >= lo and a <= hi]
        out.append({
            "local": lid, "id": ls["id"], "title": ls["title"],
            "start": max(ls["t0"], lo), "end": min(ls["t1"], hi),
            "before": ls["t0"] < lo, "after": ls["t1"] > hi,
            "startedHere": any(e["dir"] == "start" for e in evs),
            "segs": segs, "segCost": seg_cost,
            "active": sum(b - a for a, b in segs), "activeTotal": ls["main"]["active"],
            "cost": round(sum(seg_cost), 4), "costTotal": ls["main"]["cost"] + sum(a["cost"] for a in ls["agents"]),
            "tokens": ls["main"]["tokens"], "models": ls["main"]["models"],
            "agents": len(ls["agents"]), "agentActive": sum(a["active"] for a in ls["agents"]),
            "messages": len(ls["stations"]),
            "milestones": [m for m in ls["main"]["milestones"] if lo <= m["t"] <= hi],
            "events": sorted(evs, key=lambda e: e["t"]),
        })
    out.sort(key=lambda x: x["start"])
    return out, unresolved


# ---------------------------------------------------------------- selection
def all_transcripts():
    return glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))


def resolve(sel):
    if os.path.isfile(sel):
        return os.path.abspath(sel)
    if sel in ("current", "latest", "."):
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
        cands = glob.glob(os.path.join(PROJECTS, slug, "*.jsonl")) or all_transcripts()
        return max(cands, key=os.path.getmtime) if cands else None
    idx = desktop_index()
    if sel.startswith("local_"):
        v = idx.get(sel)
        return transcript_for(v["cli"]) if v else None
    if re.fullmatch(r"[0-9a-f-]{6,36}", sel):
        hits = glob.glob(os.path.join(PROJECTS, "*", sel + "*.jsonl"))
        if hits:
            return max(hits, key=os.path.getmtime)
    q = sel.lower()
    matches = sorted([v for v in idx.values() if q in v["title"].lower()], key=lambda v: -v["last"])
    for v in matches:
        p = transcript_for(v["cli"])
        if p:
            if len(matches) > 1:
                print(f"note: '{sel}' matched {len(matches)} sessions; using \"{v['title']}\"", file=sys.stderr)
            return p
    for p in sorted(all_transcripts(), key=os.path.getmtime, reverse=True):
        with open(p) as f:
            for line in f:
                if '"custom-title"' in line and q in line.lower():
                    return p
    return None


def list_sessions(n):
    idx = desktop_index()
    titles = {v["cli"]: v["title"] for v in idx.values()}
    rows = []
    for p in sorted(all_transcripts(), key=os.path.getmtime, reverse=True)[:n]:
        sid = os.path.basename(p)[:-6]
        subs = len(glob.glob(os.path.join(os.path.dirname(p), sid, "subagents", "agent-*.jsonl")))
        proj = os.path.basename(os.path.dirname(p)).split("-Repos-")[-1][-40:]
        when = datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
        rows.append(f"{when}  {sid[:8]}  {os.path.getsize(p) / 1e6:6.1f}MB  {subs:3d} agents  {proj:<28}  {titles.get(sid, '')}")
    print("\n".join(rows))


def strip_private(s):
    for k in [k for k in s if k.startswith("_")]:
        del s[k]
    s.pop("path", None)
    return s


# ---------------------------------------------------------------- history and purge
STATE = os.path.join(HOME, ".claude", "session-map")
MAPS_DIR = os.path.join(STATE, "maps")
HISTORY = os.path.join(STATE, "history.json")
TEMP_MARKERS = ("/scratch-workspaces/", "/scratchpad/", "/tmp/", "/private/var/folders/", "/.session-map/")
PUBLISH_RECHECK_DAYS = 7


def load_history():
    try:
        h = json.load(open(HISTORY))
    except Exception:
        h = {}
    h.setdefault("maps", [])
    h.setdefault("artifacts", [])
    h.setdefault("publish", {})
    return h


def save_history(h):
    os.makedirs(STATE, exist_ok=True)
    tmp = HISTORY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f, indent=1)
    os.replace(tmp, HISTORY)


def slugify(t):
    return re.sub(r"[^a-z0-9]+", "-", (t or "session").lower()).strip("-")[:40] or "session"


def map_kind(path):
    """'map' = throwaway (default folder or a temp/scratch dir); 'saved' = a place the user chose."""
    path = os.path.abspath(path)
    if path.startswith(MAPS_DIR + os.sep) or any(m in path for m in TEMP_MARKERS):
        return "map"
    return "saved"


def record_map(path, titles):
    h = load_history()
    path = os.path.abspath(path)
    h["maps"] = [m for m in h["maps"] if m["path"] != path]
    h["maps"].append({"path": path, "created": datetime.now().isoformat(timespec="seconds"), "sessions": titles, "kind": map_kind(path)})
    save_history(h)


def record_artifact(url, sessions, path=None):
    h = load_history()
    h["artifacts"] = [a for a in h["artifacts"] if a["url"] != url]
    h["artifacts"].append({"url": url, "sessions": sessions or "", "path": os.path.abspath(path) if path else None,
                           "published": datetime.now().isoformat(timespec="seconds")})
    h["publish"] = {"state": "ok", "at": datetime.now().isoformat(timespec="seconds"), "reason": ""}
    save_history(h)
    print(f"recorded {url}")


def forget_artifact(url):
    h = load_history()
    n = len(h["artifacts"])
    h["artifacts"] = [a for a in h["artifacts"] if a["url"] != url]
    save_history(h)
    print(f"forgot {url}" if len(h["artifacts"]) < n else f"not in history: {url}")


def age_days(iso):
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 86400
    except Exception:
        return 1e9


def show_history():
    h = load_history()
    print(f"Local maps ({len(h['maps'])}):")
    for m in sorted(h["maps"], key=lambda m: m["created"]):
        state = "" if os.path.exists(m["path"]) else "  [missing]"
        print(f"  {m['created'][:16]}  {m['kind']:<5}  {' + '.join(m['sessions'])}\n      {m['path']}{state}")
    print(f"Published maps ({len(h['artifacts'])}):")
    for a in sorted(h["artifacts"], key=lambda a: a["published"]):
        print(f"  {a['published'][:16]}  {a['sessions']}\n      {a['url']}")


def purge(older_than, include_saved, yes):
    h = load_history()
    doomed, kept, missing, size = [], [], [], 0
    for m in h["maps"]:
        if not os.path.exists(m["path"]):
            missing.append(m)
        elif age_days(m["created"]) >= older_than and (m["kind"] == "map" or include_saved):
            doomed.append(m)
            size += os.path.getsize(m["path"])
        else:
            kept.append(m)
    # stray files in the default folder that history doesn't know about
    known = {m["path"] for m in h["maps"]}
    strays = [p for p in glob.glob(os.path.join(MAPS_DIR, "*.html")) if p not in known
              and (datetime.now().timestamp() - os.path.getmtime(p)) / 86400 >= older_than]
    size += sum(os.path.getsize(p) for p in strays)
    arts = [a for a in h["artifacts"] if age_days(a["published"]) >= older_than]

    verb = "Deleted" if yes else "Would delete"
    print(f"{verb} {len(doomed) + len(strays)} local map file(s), {size / 1e6:.1f} MB"
          + (f" older than {older_than:g} days" if older_than else "") + ":")
    for m in doomed:
        print(f"  {m['path']}")
    for p in strays:
        print(f"  {p}")
    saved = [m for m in kept if m["kind"] == "saved"]
    if saved and not include_saved:
        print(f"Kept {len(saved)} saved export(s) (pass --include-saved to delete them too).")
    if missing:
        print(f"{'Dropped' if yes else 'Would drop'} {len(missing)} history entr{'y' if len(missing) == 1 else 'ies'} whose files are already gone.")
    if arts:
        print(f"Published maps in this range ({len(arts)}). The script can't delete these; delete them through the Artifact tool, then run --forget-artifact URL:")
        for a in arts:
            print(f"  {a['published'][:16]}  {a['url']}  {a['sessions']}")
    if not yes:
        if doomed or strays or missing:
            print("Dry run. Re-run with --yes to delete the local files.")
        return
    for m in doomed:
        try:
            os.remove(m["path"])
        except OSError as e:
            print(f"  could not delete {m['path']}: {e}", file=sys.stderr)
            kept.append(m)
    for p in strays:
        try:
            os.remove(p)
        except OSError as e:
            print(f"  could not delete {p}: {e}", file=sys.stderr)
    h["maps"] = kept
    save_history(h)


def note_publish(state, reason=""):
    h = load_history()
    h["publish"] = {"state": state, "at": datetime.now().isoformat(timespec="seconds"), "reason": reason}
    save_history(h)
    print(f"publishing marked {state}")


def publish_status():
    """Prints 'unknown', 'ok' or 'blocked'. A blocked note expires after PUBLISH_RECHECK_DAYS so policy changes are noticed."""
    p = load_history()["publish"]
    if not p:
        print("unknown")
    elif p.get("state") == "blocked" and age_days(p.get("at", "")) >= PUBLISH_RECHECK_DAYS:
        print(f"unknown (blocked on {p.get('at', '')[:10]}, worth trying again)")
    else:
        print(p.get("state") + (f" since {p.get('at', '')[:10]}" if p.get("at") else "") + (f": {p['reason']}" if p.get("reason") else ""))



def open_file(path):
    import webbrowser
    from pathlib import Path
    webbrowser.open(Path(path).resolve().as_uri())


def build(selectors, out_html=None, out_json=None, open_after=False, include_map_work=False):
    data, titles = {}, []
    for sel in selectors:
        p = resolve(sel)
        if not p:
            print(f"error: no session matches '{sel}'", file=sys.stderr)
            sys.exit(2)
        s = analyze_session(p, include_map_work=include_map_work)
        if not s:
            print(f"error: '{p}' has no messages", file=sys.stderr)
            sys.exit(2)
        s["linked"], s["peers"] = link_sessions(s, include_map_work)
        titles.append(s["title"])
        data[s["id"][:8]] = strip_private(s)
        a = s["agents"]
        print(f"{s['title']}: {len(s['stations'])} messages, {len(a)} subagents, {len(s['linked'])} linked sessions, "
              f"{sum(len(x['milestones']) for x in a) + len(s['main']['milestones'])} milestones, "
              f"${s['main']['cost'] + sum(x['cost'] for x in a):,.2f} API-equivalent", file=sys.stderr)
        ex = s["main"]["excluded"]
        if ex:
            secs = sum(x["active"] for x in ex)
            print(f"  left out {len(ex)} map-making turn(s): {secs / 60:.0f}m working" if secs >= 90 else f"  left out {len(ex)} map-making turn(s): {secs:.0f}s working", end="", file=sys.stderr)
            print(f", "
                  f"${sum(x['cost'] for x in ex):,.2f} (use --include-map-work to keep them)", file=sys.stderr)
    meta = {"pricesAsOf": PRICES_AS_OF, "generated": datetime.now().isoformat(timespec="minutes"),
            "params": {"mainIdleCap": MAIN_IDLE_CAP, "agentGap": AGENT_GAP}}
    payload = json.dumps({"meta": meta, "sessions": data}, separators=(",", ":")).replace("</", "<\\/")
    html = open(TEMPLATE).read().replace("/*DATA*/", payload)
    if not out_html:
        os.makedirs(MAPS_DIR, exist_ok=True)
        out_html = os.path.join(MAPS_DIR, f"{slugify(titles[0])}{'-and-more' if len(titles) > 1 else ''}-{datetime.now():%Y%m%d-%H%M%S}.html")
    out_html = os.path.abspath(os.path.expanduser(out_html))
    if os.path.isdir(out_html):
        out_html = os.path.join(out_html, f"session-map-{slugify(titles[0])}.html")
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    with open(out_html, "w") as f:
        f.write(html)
    record_map(out_html, titles)
    if out_json:
        with open(out_json, "w") as f:
            json.dump({"meta": meta, "sessions": data}, f, indent=1)
    from pathlib import Path
    print(out_html)
    print("url: " + Path(out_html).as_uri())
    if open_after:
        open_file(out_html)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("selectors", nargs="*", help="sessions to map; each becomes a tab")
    ap.add_argument("-o", "--out", help="where to write the page (a file or a folder). Default: ~/.claude/session-map/maps/")
    ap.add_argument("--open", action="store_true", help="open the page in your default browser after writing it")
    ap.add_argument("--publish-status", action="store_true", help="print whether publishing Artifacts worked last time: unknown, ok or blocked")
    ap.add_argument("--note-publish", choices=["ok", "blocked"], help="remember whether publishing an Artifact worked")
    ap.add_argument("--reason", default="", help="with --note-publish blocked: the error, briefly")
    ap.add_argument("--json", help="also write the parsed data to this file")
    ap.add_argument("--projects-dir", help="read transcripts from here instead of ~/.claude/projects (e.g. demo/data/projects)")
    ap.add_argument("--desktop-dir", help="read desktop session records from here instead of the Claude app's folder")
    ap.add_argument("--include-map-work", action="store_true", help="keep turns spent making session maps (left out by default)")
    ap.add_argument("--list", nargs="?", const=20, type=int, metavar="N", help="list the N most recent sessions")
    ap.add_argument("--history", action="store_true", help="list maps written and published so far")
    ap.add_argument("--purge", action="store_true", help="delete old map files (dry run unless --yes)")
    ap.add_argument("--older-than", type=float, default=0, metavar="DAYS", help="with --purge: only maps at least this old")
    ap.add_argument("--include-saved", action="store_true", help="with --purge: also delete maps saved to a folder you chose")
    ap.add_argument("--yes", action="store_true", help="with --purge: actually delete")
    ap.add_argument("--record-artifact", metavar="URL", help="remember a published map's link")
    ap.add_argument("--sessions", default="", help="with --record-artifact: what the map covers, e.g. 'auth refactor + current'")
    ap.add_argument("--file", help="with --record-artifact: the local file that was published")
    ap.add_argument("--forget-artifact", metavar="URL", help="drop a published map from history (after deleting it)")
    a = ap.parse_args()
    global PROJECTS, DESKTOP
    if a.projects_dir:
        PROJECTS = os.path.abspath(os.path.expanduser(a.projects_dir))
    if a.desktop_dir:
        DESKTOP = os.path.abspath(os.path.expanduser(a.desktop_dir))
    if a.list:
        return list_sessions(a.list)
    if a.history:
        return show_history()
    if a.purge:
        return purge(a.older_than, a.include_saved, a.yes)
    if a.publish_status:
        return publish_status()
    if a.note_publish:
        return note_publish(a.note_publish, a.reason)
    if a.record_artifact:
        return record_artifact(a.record_artifact, a.sessions, a.file)
    if a.forget_artifact:
        return forget_artifact(a.forget_artifact)
    if not a.selectors:
        ap.error("give at least one session selector, or --list / --history / --purge")
    build(a.selectors, a.out, a.json, a.open, a.include_map_work)


if __name__ == "__main__":
    main()

"""Local session browser for session-map.

Started with `session_map.py --serve`. Lists every Claude Code session on this machine with quick
stats, and builds a session's map when you open it. Listens on 127.0.0.1 only: transcripts are private.
Uses only the Python standard library.
"""
import glob
import json
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import session_map as sm

BROWSER = os.path.normpath(os.path.join(sm.HERE, "..", "assets", "browser.html"))
ID_RE = re.compile(r"^[0-9a-f][0-9a-f-]{7,35}$")
TS_RE = re.compile(rb'"timestamp":"([0-9T:.\-]+Z)"')
CWD_RE = re.compile(rb'"cwd":"([^"]+)"')
TITLE_RE = re.compile(rb'"customTitle":"((?:[^"\\]|\\.)*)"')
MODEL_RE = re.compile(rb'"model":"(claude-[a-z]+-[0-9]+(?:-[0-9]{1,2})?)')
HUMAN = b'"turnOrigin":"human"'
# Only a user's own /session-map message starts its content with this; quoted copies inside tool
# output are JSON-escaped (\"content\":\"…) and don't match.
MAP_CMD = b'"content":"<command-message>session-map</command-message>'

_quick = {}            # path -> (size, mtime, stats)
_lock = threading.Lock()

# Full stats (working time, cost, milestones) need the real parser, so they're computed in the
# background and kept on disk. Bump CACHE_VERSION whenever the parser's numbers change.
CACHE_VERSION = 1
CACHE_DIR = os.path.join(sm.STATE, "cache")
_full = {}             # sid -> cache entry {key, stats|error}
_work = {"queue": [], "busy": None, "event": threading.Event()}


def _ts(b):
    try:
        return datetime.fromisoformat(b.decode().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def quick_stats(path, idx_by_cli):
    """Stats from one byte scan of the transcript: no JSON parsing, so the whole list builds in about a second."""
    st = os.stat(path)
    with _lock:
        hit = _quick.get(path)
        if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
            return hit[2]
    with open(path, "rb") as f:
        data = f.read()
    sid = os.path.basename(path)[:-6]
    head, tail = data[:262144], data[-262144:]
    desk = idx_by_cli.get(sid, {})
    title = desk.get("title")
    if not title:
        found = TITLE_RE.findall(tail) or TITLE_RE.findall(data)
        title = json.loads(b'"' + found[-1] + b'"') if found else None
    cwd = desk.get("cwd")
    if not cwd:
        for c in CWD_RE.findall(head):
            if b"/scratch-workspaces/" not in c:
                cwd = c.decode(errors="replace")
                break
    first = next((t for t in (_ts(m) for m in TS_RE.findall(head)) if t), None)
    last = next((t for t in (_ts(m) for m in reversed(TS_RE.findall(tail))) if t), None)
    models = MODEL_RE.findall(data)
    counts = {}
    for m in models:
        k = m.decode()
        counts[k] = counts.get(k, 0) + 1
    subs = len(glob.glob(os.path.join(os.path.dirname(path), sid, "subagents", "agent-*.jsonl")))
    stats = {
        "id": sid,
        "path": path,
        "title": title or f"Untitled ({sid[:8]})",
        "untitled": not title,
        "project": os.path.basename(cwd) if cwd else os.path.basename(os.path.dirname(path)).split("-Repos-")[-1],
        "projectDir": os.path.basename(os.path.dirname(path)),
        "first": first, "last": last or st.st_mtime,
        "modified": st.st_mtime,
        "size": st.st_size,
        "messages": data.count(HUMAN),
        "mapRuns": data.count(MAP_CMD),
        "subagents": subs,
        "models": sorted(counts, key=lambda k: -counts[k]),
        "modelCounts": counts,
    }
    with _lock:
        _quick[path] = (st.st_size, st.st_mtime, stats)
    return stats


def cache_key(path):
    """Changes whenever the transcript, any of its subagents, the parser version or the price table changes."""
    sid = os.path.basename(path)[:-6]
    st = os.stat(path)
    subs = glob.glob(os.path.join(os.path.dirname(path), sid, "subagents", "agent-*.jsonl"))
    sub_m = max((os.path.getmtime(f) for f in subs), default=0)
    return [CACHE_VERSION, sm.PRICES_AS_OF, st.st_size, round(st.st_mtime, 3), len(subs), round(sub_m, 3)]


def cache_path(sid):
    return os.path.join(CACHE_DIR, sid + ".json")


def cached_full(path):
    """The cached full stats for a transcript if still valid, else None."""
    sid = os.path.basename(path)[:-6]
    try:
        key = cache_key(path)
    except OSError:
        return None
    with _lock:
        e = _full.get(sid)
    if e is None:
        try:
            with open(cache_path(sid)) as f:
                e = json.load(f)
        except Exception:
            e = None
        if e:
            with _lock:
                _full[sid] = e
    return e if e and e.get("key") == key else None


def compute_full(path):
    """Run the real parser and keep the numbers the list needs."""
    sid = os.path.basename(path)[:-6]
    key = cache_key(path)
    try:
        s = sm.analyze_session(path)
        if not s:
            stats = {"work": 0, "main": 0, "agents": 0, "cost": 0, "wait": 0, "output": 0, "models": [], "ms": {}}
        else:
            ms = {}
            for m in s["main"]["milestones"] + [m for a in s["agents"] for m in a["milestones"]]:
                ms[m["kind"]] = ms.get(m["kind"], 0) + 1
            models = {}
            for _, m in s["main"]["models"]:
                models[m] = models.get(m, 0) + 1
            for a in s["agents"]:
                if a.get("model"):
                    models[a["model"]] = models.get(a["model"], 0) + 1
            agent_active = sum(a["active"] for a in s["agents"])
            stats = {
                "main": round(s["main"]["active"]), "agents": round(agent_active),
                "work": round(s["main"]["active"] + agent_active),
                "cost": round(s["main"]["cost"] + sum(a["cost"] for a in s["agents"]), 2),
                "wait": round(s["main"]["waitUser"]),
                "output": s["main"]["tokens"]["output"] + sum(a["tokens"]["output"] for a in s["agents"]),
                "models": sorted(models, key=lambda k: -models[k]),
                "ms": ms,
                "excluded": round(sum(x["active"] for x in s["main"].get("excluded", []))),
            }
        e = {"key": key, "stats": stats, "at": time.time()}
    except Exception as ex:                       # keep going; retry only when the file changes
        e = {"key": key, "error": repr(ex), "at": time.time()}
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = cache_path(sid) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(e, f)
    os.replace(tmp, cache_path(sid))
    with _lock:
        _full[sid] = e
    return e


def worker():
    """Computes stale sessions, newest first. list_sessions() refills the queue on every request."""
    while True:
        _work["event"].wait()
        while True:
            with _lock:
                if not _work["queue"]:
                    _work["event"].clear()
                    _work["busy"] = None
                    break
                path = _work["queue"].pop(0)
                _work["busy"] = path
            try:
                if cached_full(path) is None:
                    compute_full(path)
            except OSError:
                pass


def list_sessions():
    sm._IDX = None                      # pick up sessions created since the last request
    idx = sm.desktop_index()
    by_cli = {v["cli"]: v for v in idx.values()}
    out = []
    for p in sm.all_transcripts():
        try:
            out.append(quick_stats(p, by_cli))
        except OSError:
            continue
    out.sort(key=lambda s: -(s["last"] or 0))
    stale = []
    for s in out:
        path = s["path"]
        e = cached_full(path)
        s["full"] = e.get("stats") if e else None
        s["fullError"] = e.get("error") if e else None
        if e is None:
            stale.append(path)
    with _lock:
        busy = _work["busy"]
        _work["queue"] = [p for p in stale if p != busy]
        if _work["queue"]:
            _work["event"].set()
    return [{k: v for k, v in s.items() if k != "path"} for s in out], len(stale)


class Ambiguous(Exception):
    pass


def path_for(sid):
    """A full session id, or a unique prefix of one (as the CLI selectors accept)."""
    hits = glob.glob(os.path.join(sm.PROJECTS, "*", sid + ".jsonl"))
    if hits:
        return hits[0]
    hits = glob.glob(os.path.join(sm.PROJECTS, "*", sid + "*.jsonl"))
    if len(hits) > 1:
        raise Ambiguous(f"Session id prefix {sid} matches {len(hits)} sessions; use more of the id")
    return hits[0] if hits else None


class Handler(BaseHTTPRequestHandler):
    server_version = "session-map"

    def log_message(self, fmt, *args):         # keep the terminal quiet except for errors
        if not str(args[1] if len(args) > 1 else "").startswith(("2", "3")):
            super().log_message(fmt, *args)

    def send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj), "application/json; charset=utf-8")

    def host_ok(self):
        """Only answer requests addressed to this server by its loopback name.

        Binding to 127.0.0.1 keeps other machines out, but a web page can still reach the server through
        DNS rebinding: its own hostname is re-pointed at 127.0.0.1, so the browser treats our responses as
        same-origin. Those requests always carry the attacker's hostname in Host, so refuse anything else.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, "Forbidden: open the session browser at http://127.0.0.1:%d/" % self.server.server_address[1],
                             "text/plain; charset=utf-8")
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, open(BROWSER, encoding="utf-8").read())
            if u.path == "/api/sessions":
                t = time.time()
                rows, pending = list_sessions()
                return self.send_json(200, {"sessions": rows, "pending": pending, "took": round(time.time() - t, 3),
                                            "projectsDir": sm.PROJECTS, "generated": time.time()})
            if u.path == "/map":
                ids = [i for i in ",".join(q.get("ids", [])).split(",") if i]
                if not ids or not all(ID_RE.match(i) for i in ids):
                    return self.send(400, "Give one or more session ids: /map?ids=…")
                paths = []
                for i in ids:
                    p = path_for(i)
                    if not p:
                        return self.send(404, f"No transcript for session {i}")
                    paths.append(p)
                html, _, _ = sm.render(paths, include_map_work="all" in q.get("work", []), quiet=True,
                                       extra_meta={"backUrl": "/"})
                return self.send(200, html)
            if u.path == "/favicon.ico":
                return self.send(204, b"", "image/x-icon")
            return self.send(404, "Not found")
        except sm.NoSession as e:
            return self.send(404, str(e))
        except Ambiguous as e:
            return self.send(400, str(e), "text/plain; charset=utf-8")
        except Exception as e:                  # surface parser bugs in the page instead of a dropped connection
            self.log_error("error on %s: %r", self.path, e)
            return self.send(500, f"session-map hit an error: {e!r}")


def run(port=8765, open_browser=False):
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if not srv:
        raise SystemExit(f"no free port between {port} and {port + 19}")
    threading.Thread(target=worker, daemon=True, name="session-stats").start()
    threading.Thread(target=list_sessions, daemon=True).start()      # start filling the cache right away
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Session browser: {url}  (Ctrl-C to stop)", flush=True)
    print(f"Reading transcripts from {sm.PROJECTS}", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()

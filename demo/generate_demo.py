#!/usr/bin/env python3
"""Generate a fictional Claude Code session to demo session-map without exposing real work.

The story: over two days, someone builds "Kestrel", a small self-hosted status page
(Go API, Postgres, Next.js). It exercises everything the map draws:

- the main session switching models (Opus 5.5 -> Fable 5.2 for the scheduler -> back to Opus)
- 10 subagents on four models, run in parallel bursts, one paused by a rate limit and resumed
- a linked session (landing page and brand) started from the main one, trading messages with it
- waits on the person: a few minutes, a lunch break, and a night
- commits, failing and passing test runs, a pull request, published artifacts
- a /session-map turn at the end, which the map leaves out by default

Output is deterministic. Usage:

  python3 demo/generate_demo.py                 # writes demo/data/
  python3 scripts/session_map.py --projects-dir demo/data/projects \\
      --desktop-dir demo/data/desktop "Kestrel status page" --open
"""
import argparse
import json
import os
import random
import shutil
import uuid
from datetime import datetime, timedelta, timezone

R = random.Random(7)
OPUS, FABLE, SONNET, HAIKU = "claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5", "claude-haiku-5-5"
CWD = "/Users/demo/Development/kestrel"
PROJECT_SLUG = "-Users-demo-Development-kestrel"
REPO = "demo-org/kestrel"
MAIN_TITLE = "Kestrel status page v1"
BRAND_TITLE = "Kestrel landing page and brand"
START = datetime(2026, 11, 9, 14, 58, 0, tzinfo=timezone.utc)   # a Monday morning in New York


def uid():
    return str(uuid.UUID(int=R.getrandbits(128), version=4))


def hexid(n):
    return "".join(R.choice("0123456789abcdef") for _ in range(n))


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


class Line:
    """One transcript (a session or a subagent) with its own clock, model and context size."""

    PACE = {HAIKU: 0.7, SONNET: 2.0, OPUS: 2.3, FABLE: 2.8}   # how long each model "thinks" per step

    def __init__(self, sid, start, model, agent_id=None, ctx=52000):
        self.sid, self.t, self.model, self.aid, self.ctx = sid, start, model, agent_id, ctx
        self.rows, self.last = [], None

    def think(self, out):
        """Seconds spent producing `out` tokens: a base delay plus generation time, scaled by model."""
        return (R.uniform(4, 14) + out / 55) * self.PACE.get(self.model, 2)

    # ---- low level
    def row(self, typ, **kw):
        r = {"type": typ, "uuid": uid(), "parentUuid": self.last, "timestamp": iso(self.t),
             "sessionId": self.sid, "cwd": CWD, "isSidechain": self.aid is not None}
        if self.aid:
            r["agentId"] = self.aid
        r.update(kw)
        self.rows.append(r)
        self.last = r["uuid"]
        return r

    def wait(self, secs):
        self.t += timedelta(seconds=secs)

    def until(self, t):
        if t > self.t:
            self.t = t

    def usage(self, out):
        write = R.randint(900, 7000)
        self.ctx = min(self.ctx + write + R.randint(800, 3500), 420000)
        return {"input_tokens": R.randint(1, 6), "cache_creation_input_tokens": write,
                "cache_read_input_tokens": self.ctx, "output_tokens": out,
                "cache_creation": {"ephemeral_1h_input_tokens": write, "ephemeral_5m_input_tokens": 0},
                "service_tier": "standard", "speed": "standard"}

    def assistant(self, content, out):
        self.row("assistant", message={"id": "msg_" + hexid(24), "type": "message", "role": "assistant",
                                       "model": self.model, "content": content, "usage": self.usage(out)})

    # ---- conversation
    def human(self, text, after):
        self.wait(after)
        self.row("user", turnOrigin="human", origin={"kind": "human"}, message={"role": "user", "content": text})

    def command(self, name, args="", after=20):
        self.wait(after)
        self.row("user", message={"role": "user", "content":
                 f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>" +
                 (f"\n<command-args>{args}</command-args>" if args else "")})

    def say(self, text, secs=None, out=None):
        out = out or len(text) // 3 + R.randint(120, 900)
        self.wait(secs if secs is not None else self.think(out))
        self.assistant([{"type": "text", "text": text}], out)

    def tool(self, name, inp, result, secs=None, err=False, extra=None, think=None, out=None):
        out = out or R.randint(90, 800)
        self.wait(think if think is not None else self.think(out))
        tid = "toolu_" + hexid(24)
        self.assistant([{"type": "tool_use", "id": tid, "name": name, "input": inp}], out)
        self.wait(secs if secs is not None else R.uniform(1, 6))
        kw = {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": err}]}}
        if extra:
            kw["toolUseResult"] = extra
        self.row("user", **kw)
        return tid

    # ---- common tools
    def read(self, path, n=120):
        self.tool("Read", {"file_path": f"{CWD}/{path}"}, f"     1\t// {path}\n     2\t…\n({n} lines)", secs=R.uniform(0.5, 2))

    def grep(self, pattern, hits):
        self.tool("Grep", {"pattern": pattern, "path": CWD}, f"Found {hits} files", secs=R.uniform(0.5, 2))

    def write(self, path, out=None):
        self.tool("Write", {"file_path": f"{CWD}/{path}", "content": "…"}, f"File created successfully at: {CWD}/{path}",
                  secs=R.uniform(0.5, 2), out=out or R.randint(900, 4200))

    def edit(self, path):
        self.tool("Edit", {"file_path": f"{CWD}/{path}", "old_string": "…", "new_string": "…"},
                  f"The file {CWD}/{path} has been updated successfully.", secs=R.uniform(0.5, 2), out=R.randint(300, 1500))

    def bash(self, cmd, out, secs=None, err=False):
        self.tool("Bash", {"command": cmd, "description": cmd.split()[0]}, ("Exit code 1\n" if err else "") + out,
                  secs=secs if secs is not None else R.uniform(2, 12), err=err)

    def test(self, cmd, passed, detail, secs):
        self.bash(cmd, detail, secs=secs, err=not passed)

    def commit(self, msg, branch="main", files=None):
        files = files or R.randint(2, 9)
        self.bash(f'git add -A && git commit -q -m "{msg}"',
                  f"[{branch} {hexid(7)}] {msg}\n {files} files changed, {R.randint(40, 420)} insertions(+), {R.randint(0, 60)} deletions(-)",
                  secs=1.2)

    def artifact(self, path, url):
        self.tool("Artifact", {"file_path": f"{CWD}/{path}", "icon": "document"},
                  f"Published {CWD}/{path} at {url} (Version 1)", secs=R.uniform(2, 4))


class Demo:
    def __init__(self):
        self.main_sid, self.brand_sid = uid(), uid()
        self.main_local, self.brand_local = "local_" + uid(), "local_" + uid()
        self.main = Line(self.main_sid, START, OPUS)
        self.brand = None
        self.agents = []        # (Line, meta)

    # ---- subagents
    def launch(self, desc, model, kind, prompt):
        m = self.main
        aid = "a" + hexid(16)
        tid = m.tool("Agent", {"description": desc, "subagent_type": kind, "prompt": prompt, "run_in_background": True},
                     f"Async agent launched successfully.\nagentId: {aid}", secs=0.4,
                     extra={"isAsync": True, "status": "async_launched", "agentId": aid, "description": desc, "resolvedModel": model},
                     think=R.uniform(2, 8), out=R.randint(300, 900))
        a = Line(self.main_sid, m.t, model, agent_id=aid, ctx=R.randint(22000, 36000))
        a.row("user", message={"role": "user", "content": prompt})
        self.agents.append((a, {"agentType": kind, "isBuiltIn": True, "description": desc, "toolUseId": tid,
                                "requestShape": "background", "requestNonInteractive": True, "pickedEffortModel": model, "finished": True}))
        return a, tid

    def report(self, a, tid, summary, status="completed"):
        """The agent's final message, then the notification landing in the main session."""
        a.say(summary, out=R.randint(400, 1400))
        at = a.t + timedelta(seconds=R.uniform(1, 3))
        self.main.rows.append({"type": "user", "uuid": uid(), "timestamp": iso(at), "sessionId": self.main_sid, "cwd": CWD,
                               "isSidechain": False, "turnOrigin": "task_notification", "origin": {"kind": "task-notification"},
                               "message": {"role": "user", "content":
                                           f"<task-notification>\n<task-id>{a.aid}</task-id>\n<tool-use-id>{tid}</tool-use-id>\n"
                                           f"<status>{status}</status>\n<summary>{summary[:200]}</summary>\n</task-notification>"}})
        return at

    # ---- messages between sessions
    def peer_to_main(self, text):
        b = self.brand
        b.tool("SendMessage", {"to": MAIN_TITLE, "summary": text[:60], "message": text}, "Message delivered.", secs=0.5)
        self.main.rows.append({"type": "user", "uuid": uid(), "timestamp": iso(b.t), "sessionId": self.main_sid, "cwd": CWD,
                               "isSidechain": False, "turnOrigin": "peer",
                               "origin": {"kind": "peer", "fromSession": self.brand_local, "name": BRAND_TITLE, "body": text},
                               "message": {"role": "user", "content": text}})
        return b.t

    def main_to_brand(self, text):
        self.main.tool("SendMessage", {"to": BRAND_TITLE, "summary": text[:60], "message": text}, "Message delivered.", secs=0.5)

    # ---- the story
    def run(self):
        m = self.main
        m.row("custom-title", customTitle=MAIN_TITLE)

        # Day 1, 10:00 — kickoff and a parallel survey on Haiku
        m.human("I want to build Kestrel, a small self-hosted status page: uptime checks for HTTP and TCP targets, incidents with a "
                "public timeline, and email subscribers. Go API, Postgres, Next.js front end. Look at what's in the repo and propose "
                "a plan before writing any code.", after=0)
        m.say("I'll survey the repo first, in three quick read-only passes running in parallel.", out=420)
        a1, t1 = self.launch("Map the Go packages and entry points", HAIKU, "Explore", "List the Go packages under cmd/ and internal/ ...")
        a2, t2 = self.launch("Survey the Next.js app routes", HAIKU, "Explore", "Walk web/app and list every route ...")
        a3, t3 = self.launch("Check Postgres migrations and sqlc setup", HAIKU, "Explore", "Read migrations/ and sqlc.yaml ...")
        a1.bash("go list ./...", "kestrel/cmd/kestrel\nkestrel/internal/api\nkestrel/internal/store", secs=3)
        for p in ("cmd/kestrel/main.go", "internal/api/router.go", "internal/store/db.go"):
            a1.read(p)
        a1.grep("http.HandleFunc|chi.NewRouter", 4)
        n1 = self.report(a1, t1, "Three packages: cmd/kestrel (entry point), internal/api (chi router, 6 handlers), internal/store (pgx pool). No job queue yet.")
        a2.grep("export default function", 9)
        for p in ("web/app/layout.tsx", "web/app/page.tsx", "web/app/admin/page.tsx"):
            a2.read(p)
        n2 = self.report(a2, t2, "App Router with 9 routes; the public page is a placeholder. Tailwind, no component library.")
        a3.bash("ls migrations", "0000_init.sql", secs=1)
        a3.read("migrations/0000_init.sql")
        a3.read("sqlc.yaml", n=24)
        n3 = self.report(a3, t3, "One migration (users only). sqlc is configured for internal/store but no queries exist yet.")
        for n in sorted([n1, n2, n3]):
            m.until(n)
            m.say(R.choice(["That's the Go side.", "Front end mapped.", "Database checked."]), out=180)
        m.read("README.md")
        m.write("docs/plan.md", out=5200)
        m.artifact("docs/plan.html", "https://claude.ai/artifact/demoKestrelPlan")
        m.say("Plan is up: three stages. 1) schema and an uptime checker on a job queue, 2) public page and incident timeline, "
              "3) email subscribers. The scheduler is the riskiest piece.", out=1600)

        # 10:40 — switch to Fable for the scheduler, two Sonnet builders in parallel
        m.command("model", FABLE, after=17 * 60)
        m.model = FABLE
        m.human("Plan looks right. Use River for the job queue instead of hand-rolling one. The scheduler is the risky bit, so Fable "
                "is on it now. Start with the schema and the checker.", after=35)
        m.say("Before handing out work, the scheduler design: per-monitor jittered intervals, River periodic jobs, and a lease so "
              "two workers never check the same target at once. Writing it down so both builders share it.", secs=70, out=3400)
        m.write("docs/scheduler.md", out=6100)
        a4, t4 = self.launch("Postgres schema and migrations (1.1)", SONNET, "general-purpose", "Create migrations for monitors, checks, incidents ...")
        a5, t5 = self.launch("Uptime checker worker on River (1.2)", SONNET, "general-purpose", "Implement HTTP and TCP checks as River jobs ...")
        a4.read("docs/scheduler.md")
        a4.read("migrations/0000_init.sql")
        a4.say("Monitors own many checks; incidents reference monitors but outlive them, so no cascade there.", out=1400)
        a4.write("migrations/0001_monitors.sql")
        a4.write("migrations/0002_incidents.sql")
        a4.write("internal/store/queries/monitors.sql")
        a4.bash("sqlc generate", "", secs=4)
        a4.test("go test ./internal/store/...", True, "ok  \tkestrel/internal/store\t2.114s", secs=24)
        a4.commit("feat(store): monitors, checks and incidents schema")
        n4 = self.report(a4, t4, "Two migrations and sqlc queries for monitors, checks and incidents. Store tests pass.")
        a5.read("docs/scheduler.md")
        a5.write("internal/checker/http.go")
        a5.write("internal/checker/tcp.go")
        a5.write("internal/checker/scheduler.go")
        a5.bash("go get github.com/riverqueue/river@v0.14.2", "go: added github.com/riverqueue/river v0.14.2", secs=11)
        a5.write("internal/checker/jobs.go")
        a5.test("go build ./...", True, "", secs=14)
        a5.write("internal/checker/http_test.go")
        a5.write("internal/checker/tcp_test.go")
        a5.test("go test ./internal/checker/...", False, "--- FAIL: TestTCPCheck_Timeout (5.00s)\n    tcp_test.go:41: expected timeout after 2s, got success\nFAIL", secs=19)
        a5.edit("internal/checker/tcp.go")
        a5.wait(40)
        n5 = self.report(a5, t5, "Stopped: API rate limit reached. HTTP checks pass; TestTCPCheck_Timeout still fails.", status="failed")
        for n, text in sorted([(n4, "Schema is in and the store tests pass."),
                               (n5, "The checker agent hit the rate limit with one TCP test still failing. I'll resume it once the limit resets.")]):
            m.until(n)
            m.say(text, out=380)

        # 11:35 — the person nudges after the limit resets
        m.human("Limit should be reset now, carry on.", after=41 * 60)
        m.tool("SendMessage", {"to": a5.aid, "summary": "Resume the checker", "message":
               "The rate limit has reset. Continue from TestTCPCheck_Timeout: the dialer ignores the context deadline."}, "Message delivered.", secs=0.5)
        a5.until(m.t + timedelta(seconds=3))
        a5.read("internal/checker/tcp.go")
        a5.edit("internal/checker/tcp.go")
        a5.test("go test ./internal/checker/...", True, "ok  \tkestrel/internal/checker\t3.902s", secs=21)
        a5.commit("feat(checker): HTTP and TCP checks on River with jittered scheduling")
        n5b = self.report(a5, t5, "Fixed: DialContext now honours the deadline. All checker tests pass; committed.")
        m.until(n5b)
        m.test("go test ./...", True, "ok  \tkestrel/internal/api\t0.611s\nok  \tkestrel/internal/checker\t3.877s\nok  \tkestrel/internal/store\t2.020s", secs=38)
        m.bash("go vet ./...", "", secs=9)
        m.say("Stage 1 is done: schema, checker and scheduler, all tests green.", out=900)

        # 12:45 after lunch — back to Opus, a linked session for the brand, three more builders
        m.command("model", OPUS, after=65 * 60)
        m.model = OPUS
        m.human("Nice. Public status page and the incident timeline next. Also start a separate session for the landing page copy "
                "and brand, so it doesn't clutter this one.", after=40)
        m.tool("mcp__ccd_session__start_session",
               {"title": BRAND_TITLE, "initiation": "user_asked", "context": "fresh", "prompt": "Write landing page copy and a small palette for Kestrel ..."},
               f'Started "{BRAND_TITLE}" ({self.brand_local}). It runs on its own; you\'ll get a note when it ends a turn.', secs=2)
        self.brand = b = Line(self.brand_sid, m.t + timedelta(seconds=6), SONNET, ctx=18000)
        b.row("custom-title", customTitle=BRAND_TITLE)
        b.human("Write landing page copy and a small palette for Kestrel, a self-hosted status page. Plain, technical, no hype.", after=0)
        a6, t6 = self.launch("Public status page (2.1)", SONNET, "general-purpose", "Build web/app/(public)/page.tsx from the monitors API ...")
        a7, t7 = self.launch("Incident timeline API (2.2)", SONNET, "general-purpose", "Add incident and update endpoints ...")

        b.read("README.md")
        b.read("docs/plan.md")
        b.say("The voice should be plain and operational: people read a status page when something is broken.", secs=55, out=1900)
        b.write("docs/brand/copy.md", out=2600)
        b.say("Three palette options; going with the cool grey and signal colours that hold up in dark mode.", secs=140, out=2400)
        b.write("docs/brand/palette.md", out=1800)
        b.artifact("docs/brand/palette.html", "https://claude.ai/artifact/demoKestrelPalette")
        p1 = self.peer_to_main("Hero copy and a palette draft are in docs/brand. Should the status colours follow the page's semantic tokens?")

        a6.read("web/app/layout.tsx")
        a6.read("internal/api/monitors.go")
        for p in ("web/app/(public)/page.tsx", "web/components/StatusBadge.tsx", "web/components/UptimeBars.tsx", "web/lib/api.ts"):
            a6.write(p)
        a6.test("pnpm tsc --noEmit", False, "web/components/UptimeBars.tsx(31,18): error TS2345: Argument of type 'Check[] | undefined' is not assignable", secs=16)
        a6.edit("web/components/UptimeBars.tsx")
        a6.write("web/components/UptimeBars.test.tsx")
        a6.test("pnpm test", True, " ✓ web/components/UptimeBars.test.tsx (6)\n Test Files  4 passed (4)", secs=31)
        a6.commit("feat(web): public status page with 90-day uptime bars")
        n6 = self.report(a6, t6, "Public page renders monitors with 90-day uptime bars and current status. Tests pass.")
        a7.read("migrations/0002_incidents.sql")
        a7.write("internal/incidents/handler.go")
        a7.write("internal/incidents/store.go")
        a7.write("internal/incidents/store_test.go")
        a7.edit("internal/api/router.go")
        a7.test("go test -race ./internal/incidents/...", False,
                "WARNING: DATA RACE\nWrite at 0x00c000142010 by goroutine 18:\n  kestrel/internal/incidents.(*Store).AppendUpdate()\n--- FAIL: TestAppendUpdate_Concurrent\nFAIL", secs=27)
        a7.edit("internal/incidents/store.go")
        a7.test("go test -race ./internal/incidents/...", True, "ok  \tkestrel/internal/incidents\t1.733s", secs=26)
        a7.commit("feat(incidents): timeline API with ordered updates")
        n7 = self.report(a7, t7, "Incident and update endpoints done. Fixed a race in AppendUpdate with a per-incident mutex.")

        events = sorted([(p1, "peer"), (n6, "n6"), (n7, "n7")])
        for at, what in events:
            m.until(at)
            if what == "peer":
                m.say("The brand session is asking about status colours.", out=200)
                self.main_to_brand("Yes: operational, degraded, partial outage and major outage, matching web/styles/tokens.css.")
            elif what == "n6":
                m.say("Public page is in.", out=220)
            else:
                m.say("Incident API is in. A mutex fixed the race, but ordering under concurrent writers deserves a second look.", out=520)
                a8, t8 = self.launch("Review incident API for races and ordering", OPUS, "general-purpose", "Review internal/incidents for ...")
                for p in ("internal/incidents/store.go", "internal/incidents/handler.go", "migrations/0002_incidents.sql"):
                    a8.read(p)
                a8.grep("seq|ORDER BY", 3)
                a8.say("Checking how sequence numbers behave with two API replicas writing at once.", out=2600)
                a8.read("internal/incidents/store_test.go")
                n8 = self.report(a8, t8, "One real issue: update sequence numbers are assigned in Go, so two API replicas can collide. "
                                         "Assign them in Postgres with a per-incident counter.")
        m.until(n8)
        m.edit("migrations/0002_incidents.sql")
        m.edit("internal/incidents/store.go")
        m.test("go test -race ./internal/incidents/...", True, "ok  \tkestrel/internal/incidents\t1.802s", secs=25)
        m.commit("fix(incidents): assign update sequence in Postgres")
        m.say("Stage 2 is done. The reviewer caught sequence numbers being assigned in Go; Postgres assigns them now.", out=1100)

        b.human("Make the hero line shorter, and less 'enterprise'.", after=26 * 60)
        b.edit("docs/brand/copy.md")
        b.commit("docs(brand): landing copy and palette", branch="brand")
        b.say("Shorter hero: \"Know when it's down. Tell people fast.\" Committed on the brand branch.", out=500)

        # Day 2, 09:10 — a night away, then email subscribers and a PR
        m.human("Morning! Email subscriptions with double opt-in next, then open a PR.", after=15 * 3600 + 20 * 60)
        a9, t9 = self.launch("Find everything that sends email", HAIKU, "Explore", "Find every place that sends or templates email ...")
        a9.grep("smtp|SendMail|resend", 2)
        a9.read("internal/api/auth.go")
        n9 = self.report(a9, t9, "Only the password reset sends mail, through net/smtp in internal/api/auth.go. No templates yet.")
        m.until(n9)
        m.say("Only password reset sends mail today, so I'll add a small mailer package and build subscriptions on it.", out=600)
        a10, t10 = self.launch("Email subscriptions with double opt-in (3.1)", SONNET, "general-purpose", "Add subscribers with double opt-in ...")
        b.until(m.t)
        b.human("Now build the landing page itself from the copy, in web/app/(marketing).", after=R.uniform(150, 300))
        b.say("Building it from docs/brand/copy.md with the shared status tokens.", secs=35, out=700)
        for p in ("internal/mailer/mailer.go", "internal/subscribers/handler.go", "migrations/0003_subscribers.sql",
                  "web/app/(public)/subscribe/page.tsx"):
            a10.write(p)
        a10.write("internal/subscribers/tokens.go")
        a10.write("internal/subscribers/handler_test.go")
        a10.edit("internal/api/router.go")
        a10.test("go test ./internal/subscribers/...", True, "ok  \tkestrel/internal/subscribers\t1.207s", secs=18)
        a10.test("pnpm test", False, " ❯ web/app/(public)/subscribe/page.test.tsx (2)\n   × renders the confirmation state\n Snapshot mismatched", secs=29)
        a10.edit("web/app/(public)/subscribe/page.test.tsx")
        a10.test("pnpm test", True, " Test Files  5 passed (5)", secs=28)
        a10.commit("feat(subscribers): email subscriptions with double opt-in")
        a10.commit("feat(mailer): small mailer with SMTP and log transports")
        n10 = self.report(a10, t10, "Subscribers with double opt-in: tokenised confirm links, 48-hour expiry, unsubscribe in every mail. Tests pass.")
        for p in ("web/app/(marketing)/page.tsx", "web/app/(marketing)/Hero.tsx"):
            b.write(p)
        b.bash("pnpm build", "✓ Compiled successfully\n✓ Generating static pages (12/12)", secs=48)
        b.commit("feat(web): landing page", branch="brand")
        p2 = self.peer_to_main("Landing page is up on the brand branch; screenshots are in docs/brand/preview.")
        b.say("Landing page built and committed; I let the main session know.", out=300)

        for at, what in sorted([(n10, "n10"), (p2, "peer")]):
            m.until(at)
            m.say("Subscriptions are in." if what == "n10" else "The landing page is ready on the brand branch.", out=260)
        m.test("go test ./...", True, "ok  \tkestrel/internal/api\t0.640s\nok  \tkestrel/internal/checker\t3.911s\nok  \tkestrel/internal/incidents\t1.790s\n"
                                      "ok  \tkestrel/internal/store\t2.050s\nok  \tkestrel/internal/subscribers\t1.188s", secs=44)
        m.test("pnpm test", True, " Test Files  5 passed (5)", secs=27)
        m.bash("git push -u origin feat/status-page-v1", "branch 'feat/status-page-v1' set up to track 'origin/feat/status-page-v1'.", secs=3)
        m.bash('gh pr create --title "Kestrel status page v1" --body-file /tmp/pr.md', f"https://github.com/{REPO}/pull/12", secs=4)
        m.rows.append({"type": "pr-link", "sessionId": self.main_sid, "prNumber": 12, "prUrl": f"https://github.com/{REPO}/pull/12",
                       "prRepository": REPO, "timestamp": iso(m.t)})
        m.say("PR #12 is open with all three stages.", out=700)

        # 09:55 — CI is red: a flaky test under -race
        m.human("CI is red on the PR, the checker test looks flaky under -race.", after=27 * 60)
        m.test("go test -race -count=20 ./internal/checker/...", False,
               "--- FAIL: TestScheduler_Jitter (0.02s)\n    scheduler_test.go:88: next run 14:00:03, want within [14:00:00, 14:00:02]\nFAIL", secs=74)
        m.read("internal/checker/scheduler_test.go")
        m.edit("internal/checker/scheduler_test.go")
        m.test("go test -race -count=20 ./internal/checker/...", True, "ok  \tkestrel/internal/checker\t41.380s", secs=71)
        m.commit("fix(checker): stop sharing the fake clock across test cases", branch="feat/status-page-v1")
        m.bash("git push", "To github.com:demo-org/kestrel.git\n   9c1e2f4..b77a0d3  feat/status-page-v1 -> feat/status-page-v1", secs=3)
        m.say("The test cases shared one fake clock, so jitter from one leaked into the next. Fixed and pushed; CI should go green.", out=900)

        # 10:20 — making a map of the session (left out of the map by default)
        m.command("session-map", after=9 * 60)
        m.rows[-1].update(turnOrigin="human", origin={"kind": "human"})
        m.wait(0.2)
        m.row("user", message={"role": "user", "content": [{"type": "text", "text":
              "Base directory for this skill: /Users/demo/.claude/skills/session-map\n\n# Session map\n\nRenders one or more Claude Code sessions ..."}]})
        m.bash("python3 ~/.claude/skills/session-map/scripts/session_map.py current -o /tmp/session-map-kestrel.html",
               "Kestrel status page v1: 9 messages, 10 subagents, 1 linked sessions …", secs=6)
        m.tool("Artifact", {"file_path": "/tmp/session-map-kestrel.html", "icon": "map"},
               "Published /tmp/session-map-kestrel.html at https://claude.ai/artifact/demoKestrelMap (Version 1)", secs=3)
        m.say("Here's the map of this session.", out=500)

        m.human("Love it. Merging.", after=4 * 60)
        m.bash("gh pr merge 12 --squash --delete-branch", f"✓ Squashed and merged pull request {REPO}#12", secs=5)
        m.say("Merged. Kestrel v1 is on main.", out=200)

    # ---- output
    def write(self, out):
        if os.path.exists(out):
            shutil.rmtree(out)
        proj = os.path.join(out, "projects", PROJECT_SLUG)
        sub = os.path.join(proj, self.main_sid, "subagents")
        os.makedirs(sub)

        def dump(path, rows):
            rows = sorted(rows, key=lambda r: r.get("timestamp", ""))
            with open(path, "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")

        dump(os.path.join(proj, self.main_sid + ".jsonl"), self.main.rows)
        dump(os.path.join(proj, self.brand_sid + ".jsonl"), self.brand.rows)
        for a, meta in self.agents:
            dump(os.path.join(sub, f"agent-{a.aid}.jsonl"), a.rows)
            with open(os.path.join(sub, f"agent-{a.aid}.meta.json"), "w") as f:
                json.dump(meta, f)
        desk = os.path.join(out, "desktop", "demo-account", "demo-org")
        os.makedirs(desk)
        for local, cli, title, line in ((self.main_local, self.main_sid, MAIN_TITLE, self.main),
                                        (self.brand_local, self.brand_sid, BRAND_TITLE, self.brand)):
            with open(os.path.join(desk, local + ".json"), "w") as f:
                json.dump({"sessionId": local, "cliSessionId": cli, "title": title, "cwd": CWD,
                           "lastActivityAt": int(line.t.timestamp() * 1000)}, f)
        return proj


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default=os.path.join(here, "data"), help="where to write the demo (default: demo/data)")
    a = ap.parse_args()
    d = Demo()
    d.run()
    proj = d.write(a.out)
    print(f"Wrote {len(d.agents)} subagents and 2 sessions to {proj}")
    print(f"Map it with:\n  python3 {os.path.relpath(os.path.join(here, '..', 'scripts', 'session_map.py'))} "
          f"--projects-dir {os.path.relpath(os.path.join(a.out, 'projects'))} --desktop-dir {os.path.relpath(os.path.join(a.out, 'desktop'))} "
          f"\"{MAIN_TITLE}\" --open")


if __name__ == "__main__":
    main()

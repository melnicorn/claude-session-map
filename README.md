# session-map

A Claude Code skill that draws a session as a subway map.

The main session is the trunk line. Subagents branch off where they were launched and rejoin where they reported back. Other sessions it started or exchanged messages with run alongside, connected at each exchange. Commits, pull requests, test runs and published artifacts sit on the line where they happened. The horizontal axis is scaled to working time, so an overnight pause takes no more room than a coffee break.

Each line also carries its model and its API-equivalent token cost, so you can see where the time went and where the money went.

## What you get

- **Totals**: working time for the main session and for all subagents, elapsed time, time spent waiting on you, and API-equivalent cost.
- **The map**:
  - **Lines**: thick where Claude was working, dotted while it waited or a subagent was paused (for example by a rate limit).
  - **Stops**: your messages are stations on the main line. Model switches are tagged above it.
  - **Navigation**: drag across the map to zoom into a stretch, ⌘/Ctrl + scroll to zoom, and click a branch to focus on just that line and its connections.
  - **Cost rate mode**: line width follows spend per minute.
  - **Filters**: chips show or hide commits, PRs, tests and artifacts.
- **Line directory**: one row per line, with model, working time, elapsed time, output tokens, cost, tool calls and milestones.
- **Milestone log**: every commit, PR, test run and artifact in time order.

## Install

Clone into your Claude Code skills folder:

```bash
git clone https://github.com/melnicorn/claude-session-map.git ~/.claude/skills/session-map
```

Or keep it elsewhere and symlink it:

```bash
ln -s "$PWD" ~/.claude/skills/session-map
```

It needs only Python 3, with no packages.

## Use

In Claude Code, ask for it: "map this session", "map the auth refactor session and this one", or `/session-map`. Claude finds the sessions, builds the page and publishes it as a private Artifact.

Or run the script directly:

```bash
python3 scripts/session_map.py --list 15
```

```bash
python3 scripts/session_map.py current "auth refactor" -o map.html
```

A selector can be `current`, a session UUID or prefix, a desktop session id (`local_…`), a path to a transcript `.jsonl`, or part of a session's title. Each selector becomes a tab. `--json data.json` also writes the parsed data.

The page is one self-contained HTML file with data, styles and code inline, so you can open, email or attach it anywhere. The only thing it loads from outside is a web font, and it falls back to system fonts when offline.

| Command | What it does |
|---|---|
| `session_map.py current --open` | Writes the map to `~/.claude/session-map/maps/` and opens it in your browser |
| `session_map.py current -o ~/Downloads` | Saves it where you choose (a file or a folder) |
| `session_map.py --history` | Lists maps written and published so far |
| `session_map.py --purge` | Dry run: shows which old map files would be deleted |
| `session_map.py --purge --older-than 7 --yes` | Deletes map files at least a week old |

Purge only deletes files in the default maps folder or in temporary folders. Maps you saved somewhere yourself are kept unless you add `--include-saved`. Published Artifacts can't be deleted by the script: `--purge` lists them so Claude can delete them for you, one confirmation each. History lives in `~/.claude/session-map/history.json`, so any session can clean up maps made in another.

## Demo

`demo/generate_demo.py` writes a fictional session to `demo/data/`, so you can try the map, or take screenshots, without exposing your own work. Over two days, someone builds Kestrel, a small self-hosted status page in Go, Postgres and Next.js. It covers everything the map draws:

- **Model switches:** the main session moves from Opus 5.5 to Fable 5.2 for the scheduler, then back.
- **Ten subagents** across Haiku, Sonnet and Opus, in parallel bursts. One is paused by a rate limit and later resumed.
- **A linked session** for the landing page and brand, started from the main one, trading messages with it.
- **Waits on the person:** minutes, a lunch break and a night.
- **Milestones:** commits, failing and passing tests, a pull request and published artifacts.
- **A `/session-map` turn** at the end, which the map leaves out.

```bash
python3 demo/generate_demo.py
```

```bash
python3 scripts/session_map.py --projects-dir demo/data/projects --desktop-dir demo/data/desktop "Kestrel status page v1" --open
```

The output is deterministic, so screenshots stay reproducible. `--projects-dir` and `--desktop-dir`, or the `SESSION_MAP_PROJECTS` and `SESSION_MAP_DESKTOP` environment variables, point the script at any other transcript folders.

## How it's measured

Everything comes from the transcripts Claude Code keeps in `~/.claude/projects/<project>/<session>.jsonl`, plus one file per subagent in `<session>/subagents/`.

- **Main session working time** adds up the gaps between events while Claude was mid-turn.
  - Gaps that end in your next message count as waiting on you.
  - Gaps that end with a subagent reporting back, or a message from another session, count as waiting too.
  - A single gap over 15 minutes counts as idle.
- **Subagent working time** adds up stretches of activity. A pause over 10 minutes splits the line.
- **Cost** prices every API response at list prices: input, 5-minute and 1-hour cache writes, cache reads, and output, per model. On a Claude subscription it shows what the same work would cost on the API, not what you paid. The price table is `PRICES` in `scripts/session_map.py`, dated by `PRICES_AS_OF`. Models it doesn't know count as $0.
- **Milestones**:
  - a commit is a `git commit` that succeeded
  - a test run is a recognised test command, marked failed when it exits with an error
  - PRs come from `gh pr create` and the app's PR links
  - artifacts are successful publishes
- **Making maps isn't counted.** When a session runs `/session-map`, or asks for a map in words, that turn is left out from that point on: its time, cost, tool calls and milestones. It shows on the main line as a dashed outline, so you can see something was removed. Pass `--include-map-work` to count it. Running the script by hand doesn't trigger this.
- **Linked sessions** are found through the desktop app's session records in `~/Library/Application Support/Claude/claude-code-sessions/`. Without them, as in the plain CLI, messages from other sessions show as markers but aren't drawn as lines.

Timestamps mark when events were written, so model thinking time and slow tool calls count as working time.

## Privacy

Generated maps include the first 160 characters of each of your messages and your commit messages. They're published as private Artifacts; check what's on the page before sharing one. `.gitignore` keeps generated maps and `published.json`, which holds your artifact links, out of this repo.

## License

MIT. See [LICENSE](LICENSE).

## Layout

```
SKILL.md              instructions Claude follows
scripts/session_map.py  transcript parser and page builder
assets/template.html  the map page (data is injected at /*DATA*/)
```

---
name: session-map
description: Draw a Claude Code session as a subway-style line map — the main session as the trunk line, subagents branching off and rejoining, linked sessions alongside, commits/PRs/tests/artifacts as stops, scaled to real working time with token cost per line. Use when the user asks to map, visualize, chart or summarize the work done in a session ("map this session", "show me what the agents did", "where did the time go in yesterday's session", "/session-map"), to save or open such a map as a file, or to clean up old maps ("purge my session maps").
---

# Session map

Renders one or more Claude Code sessions as an interactive HTML line map. By default it is published as a private Artifact. Where an organization doesn't allow Artifacts, it is written to a local file, with a one-line command that opens it in the system browser. It can also be saved as a standalone HTML file.

## What it shows

- **Main line**: the session itself. Solid where Claude was working, dotted where it waited on the user or on agents. Each user message is a station. Model switches are tagged above the line.
- **Branches**: each subagent leaves the main line where it was launched and rejoins where it reported back. Dotted stretches are pauses (for example rate limits). Diamonds mark the main session nudging it with a message.
- **Linked sessions**: sessions this one started, messaged or heard from, drawn as double lines with interchanges at each exchange.
- **Milestones**: commits, PRs, test runs (passed and failed) and published artifacts, placed on the line where they happened and grouped when close together.
- **Cost**: API-equivalent cost per line from the transcript's token usage, at list prices. "Cost rate" weight mode makes line width follow spend per minute.
- **Navigation**: drag across the map to zoom into a stretch, zoom buttons or Cmd/Ctrl+scroll, and click a branch to focus on it (Esc goes back).

The script is `~/.claude/skills/session-map/scripts/session_map.py` (`$SM` below). It needs only Python 3.

## Pick the sessions

Selectors: `current` (the newest transcript for the working directory, normally this session), a session UUID or prefix, a desktop id (`local_…`), a transcript path, or part of a session title. Each selector becomes a tab. If the user is vague, run `python3 $SM --list 15` and pick, or ask.

Every build prints a one-line summary per session to stderr (messages, subagents, linked sessions, milestones, cost). To stdout it prints the output path, then `url: file://…`, then `open: <command>`. Use the summary in your reply.

**Map-making is left out by default.** A turn that runs this skill, through `/session-map`, the Skill tool or the skill's instructions loading, doesn't count. From that point to the end of the turn, its time, cost, tool calls, milestones and the user's `/session-map` message are removed. On the map it shows as a dashed outline on the main line. The summary adds a "left out N map-making turn(s)" line. Add `--include-map-work` only when the user wants that work counted ("include the map runs", "count everything"). Running the script by hand doesn't trigger this, so a session spent developing this skill still counts as real work.

## Choose what the user asked for

**Show it (default)**: publish it as a private Artifact when this session can, and show it locally when it can't. Some organizations turn Artifacts off.

1. **Decide whether to try publishing.** Skip straight to step 3 if:
   - this session has no Artifact tool, or
   - `python3 $SM --publish-status` prints `blocked`.

   `unknown` or `ok` means try. A `blocked` note expires after a week and reads `unknown` again, so a policy change gets noticed.

2. **Publish.** Build into the scratchpad directory (or the working directory if there is none):

   ```bash
   python3 $SM "auth refactor" current -o <scratchpad>/session-map-<slug>.html
   ```

   Run `python3 $SM --history`. If the same sessions were published before, pass that URL as `url` to update it in place; read the artifact first, as the tool requires. Publish with `icon: "map"` and a one-sentence description naming the sessions. Then:
   - **On success**, record the link, which also marks publishing as working:

     ```bash
     python3 $SM --record-artifact <url> --sessions "<selectors joined by ' + '>" --file <the html path>
     ```

   - **If the publish is refused** because Artifacts or publishing are disabled, not allowed, or blocked by an organization or admin policy, record that, then continue with step 3:

     ```bash
     python3 $SM --note-publish blocked --reason "<the error, briefly>"
     ```

     Other failures, such as a version conflict or a missing file, aren't a policy block. Fix those and publish again.

3. **Link to it locally.** Build without `-o`. The map goes to `~/.claude/session-map/maps/`, which purge cleans up:

   ```bash
   python3 $SM "auth refactor" current
   ```

   The script prints the file path, then a line starting `url: file://…`, then a line starting `open: `. That last line is a command, already quoted for this platform, that opens the map in the default browser.

   In your reply, give the `open:` command, without the `open: ` label, as a single-line `bash` block. In the Claude desktop app this shows a Run button that opens the map in the system browser:

   ```bash
   open '/Users/you/.claude/session-map/maps/<map>.html'
   ```

   - **Keep it on one line.** The Run button only appears on single-line shell blocks.
   - **Don't make the `file://` URL the main way to open it.** The Claude app handles every `file:` link itself, even with Cmd held, and refuses files outside the session's folders. Add the URL on its own line after the block, for viewers outside the app such as a terminal.

   Say in one line that publishing isn't available for this account, so the map is a local file. Say it only the first time in a conversation.

   Don't try to open the file inside the Claude app's browser: it doesn't run local `file:` pages. Don't start a local web server to get around that. If the user asks you to open it for them, run the build with `--open`, which opens the system browser.

**Save it as a file** ("save", "export", "download", "give me the HTML"): write it where the user says. With no place named, use `~/Downloads`, since a folder works as `-o`. Don't publish unless they also asked to see it there. Saved files are kept by a plain purge.

```bash
python3 $SM current -o ~/Downloads
```

**Open it locally** ("open it in my browser", "just let me look", or a quick throwaway view without a published page): leave out `-o`. The map goes to `~/.claude/session-map/maps/`, which purge cleans up, and opens in the default browser:

```bash
python3 $SM current --open
```

`--open` can be combined with `-o`.

Then reply briefly: the link or file path, the headline numbers (working time, subagents, cost), and anything notable, such as a long pause, a cluster of failed tests or a model switch.

## Clean up old maps

History lives in `~/.claude/session-map/history.json`, so any session can purge maps made in another. Everything happens in this order:

1. **See what's there**: `python3 $SM --history`. For a dry run, `python3 $SM --purge`, optionally with `--older-than DAYS`. The dry run lists:
   - local map files it would delete
   - saved exports it keeps (unless `--include-saved`)
   - history entries whose files are already gone
   - published maps in that range
2. **Tell the user** what the dry run found: the number of files and MB, and how many published maps there are. Then ask what to remove. Local files and published maps are separate questions.
3. **Local files**: on a yes, run the same command with `--yes`.
4. **Published maps**: the script can't delete them. Delete each one the user picked with the Artifact tool's `delete` action. That's permanent, the link stops working, and the user confirms each delete. Then run `python3 $SM --forget-artifact <url>`. If a delete fails because the artifact is already gone, still forget it.

Never pass `--include-saved` or delete published maps unless the user asked for that specifically.

## Notes

- Transcripts are read from `~/.claude/projects/<project>/<session>.jsonl` and `<session>/subagents/`. Desktop session records in `~/Library/Application Support/Claude/claude-code-sessions/` map desktop ids and titles to transcripts and resolve linked sessions. Without those records (CLI only), linked sessions show as unattached message markers.
- Prices are in the `PRICES` table in the script, with the date they were taken (`PRICES_AS_OF`). If the models in use aren't listed, or the date is old, refresh the table from the `claude-api` skill's model table before trusting cost figures. Unknown models count as zero cost. (Avoid writing a dollar sign before a digit anywhere in this file: the skill loader treats it as an argument placeholder.)
- Measurement rules: a gap over 15 minutes inside a turn counts as idle (`MAIN_IDLE_CAP`), and a subagent pause over 10 minutes splits its line (`AGENT_GAP`).
- The page embeds prompt excerpts (first 160 characters of each user message) and commit messages. Mention this when the user saves or shares a map.
- A saved file is self-contained: data, styles and code are inline. Only the font comes from Google Fonts, and offline it falls back to system fonts.
- `--json out.json` also writes the parsed data, which is useful for debugging the parser.

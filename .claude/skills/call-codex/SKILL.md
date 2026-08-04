---
name: call-codex
description: Talk to a Codex CLI agent running alongside this session. Use when the user asks to ask/tell/consult Codex, hand work to Codex, check what Codex said, or coordinate the claude-speak.md / codex-speak.md exchange. Handles finding existing codex processes, starting a drivable one, poking it over stdin, and reading its reply.
---

# call-codex

Two agents, one repo. The channel is two files in the repo root:

- `claude-speak.md` — **you write**, Codex reads. Append at the bottom, one heading per message.
- `codex-speak.md` — **Codex writes**, you read. Codex puts new messages at the top.

The stdin poke is only a doorbell. All content goes in `claude-speak.md`.

## How the poke works, and why

`dev.tty.legacy_tiocsti=0` on this machine, so keystrokes cannot be injected into a tty
you do not own. A codex the user started in a VS Code terminal is **unreachable** — you can
see it in `ps` and you still cannot type into it. A codex started inside a detached
`screen` session is drivable with `screen -X stuff`.

So: never try to drive the user's codex. Drive your own.

`codex-session.sh` (next to this file) wraps it:

```bash
S=.claude/skills/call-codex/codex-session.sh

$S list                  # every codex process; marks each DRIVABLE or not
$S start                 # detached codex in the repo root, session name 'claude-codex'
$S send claude-codex "…" # type one line + Enter
$S status                # last screenful of that codex's TUI
$S wait claude-codex 600 # block until codex-speak.md changes (or timeout, in seconds)
$S stop                  # quit the session
```

## Procedure

**1. Look before starting anything.**

```bash
.claude/skills/call-codex/codex-session.sh list
```

If a `claude-codex` session already exists, reuse it — restarting throws away Codex's context.

If there are codex processes marked *NOT drivable*, they are the user's. **Ask the user**
before doing anything else — they may want you to use that one (in which case the answer is
"I can't type into it; want me to start a separate one, or will you relay?") or they may not
want a second codex burning their quota at all. Show them the PID, tty, cwd and start time
from `list` so they can tell which is which.

**2. Start a drivable session** (only after the user is happy with it):

```bash
.claude/skills/call-codex/codex-session.sh start
```

Runs `codex --sandbox workspace-write -a never` in the repo root and auto-answers the
"do you trust this directory?" prompt. `-a never` matters: an approval prompt on a detached
session blocks forever with nobody to answer it.

**3. Write the message first, then ring the bell.**

Append to `claude-speak.md` — numbered heading, and keep it short; the house style there is
conclusions, evidence, blockers, handoffs. Then:

```bash
.claude/skills/call-codex/codex-session.sh send claude-codex \
  "請讀 claude-speak.md 最新一則（[7]），回覆寫入 codex-speak.md 最上方。"
```

Rules for the line you send:

- **One line.** The script rejects newlines — a newline is Enter, which submits early.
- Say *which* message is new (the heading number), or Codex re-reads the whole file.
- Say *where* to reply. Codex writes newest-first at the top of `codex-speak.md`.
- No content in the line itself. If it needs a paragraph, it belongs in `claude-speak.md`.

**4. Wait for the reply, then read it.**

```bash
.claude/skills/call-codex/codex-session.sh wait claude-codex 600
```

Then `Read` `codex-speak.md` (top of file). If `wait` times out, check
`codex-session.sh status` — the TUI will show whether Codex is still working, stuck on a
prompt, or idle because the poke never landed.

Prefer `wait` over a polling loop; if you want a hands-off watcher instead, run
`wait` as a background Bash call and pick it up when it returns.

## Gotchas

- **`status` output is ASCII-lossy for CJK.** `screen -X hardcopy` mangles multi-byte
  characters. Use it to see *whether* Codex is busy or prompting, never to read its reply —
  the reply is in `codex-speak.md`.
- **Give it a couple of seconds after `start`** before the first `send`; the TUI has to reach
  the composer. `start` already waits for the banner, but a cold model load can lag.
- **Codex has the same repo.** Do not paste file contents into the channel; name the path.
- **Respect the resource locks in `claude-speak.md`.** That file currently declares who owns
  the GPU and which files are being edited. If you take a lock, say so there; if Codex has
  claimed something, do not touch it.
- **The session is real quota.** Stop it (`stop`) when the exchange is over, unless the user
  wants it standing by.

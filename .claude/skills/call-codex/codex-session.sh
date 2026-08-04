#!/usr/bin/env bash
# Drive a Codex CLI session over its stdin, using GNU screen as the pty owner.
#
# Why screen: the kernel here has dev.tty.legacy_tiocsti=0, so there is no way to
# inject keystrokes into a tty we do not own (a codex running in a VS Code
# terminal is unreachable). A codex we start ourselves inside a detached screen
# session can be driven with `screen -X stuff`.
#
# Usage:
#   codex-session.sh list                 # every codex process, and whether it is drivable
#   codex-session.sh start [name]         # start a detached codex in the repo root
#   codex-session.sh send [name] <line>   # type one line + Enter into that codex
#   codex-session.sh status [name]        # last screenful (ASCII-lossy for CJK)
#   codex-session.sh wait [name] [secs]   # block until codex-speak.md changes
#   codex-session.sh stop [name]
#
# Default session name: claude-codex

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../../.." && pwd)"
DEFAULT_NAME="claude-codex"
REPLY_FILE="$REPO_ROOT/codex-speak.md"

die() { echo "ERROR: $*" >&2; exit 1; }

session_exists() {
  screen -ls "$1" 2>/dev/null | grep -qE "[0-9]+\.$1[[:space:]]"
}

# ---------------------------------------------------------------- list

cmd_list() {
  local screen_pids=() sp
  while read -r sp; do screen_pids+=("$sp"); done < <(
    screen -ls 2>/dev/null | grep -oE '^\s+[0-9]+\.[^[:space:]]+' | tr -d ' '
  )

  echo "screen sessions:"
  if [ ${#screen_pids[@]} -eq 0 ]; then
    echo "  (none)"
  else
    printf '  %s\n' "${screen_pids[@]}"
  fi
  echo

  echo "codex processes:"
  local found=0
  local all_pids; all_pids=" $(ps -eo pid=,args= | grep -E '(^|/)codex( |$)|codex-linux-x64.*bin/codex$' | grep -v grep | awk '{print $1}' | tr '\n' ' ')"
  while read -r pid tty args; do
    [ -z "$pid" ] && continue
    # collapse the node wrapper -> native binary pair into one entry
    local ppid; ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
    case "$all_pids" in *" $ppid "*) continue;; esac
    found=1
    local owner="" p="$pid" hop=0
    while [ "$p" != "1" ] && [ -n "$p" ] && [ $hop -lt 12 ]; do
      for s in "${screen_pids[@]:-}"; do
        [ "${s%%.*}" = "$p" ] && owner="${s#*.}"
      done
      [ -n "$owner" ] && break
      p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
      hop=$((hop + 1))
    done
    local cwd; cwd="$(readlink /proc/"$pid"/cwd 2>/dev/null || echo '?')"
    local started; started="$(ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^ *//')"
    if [ -n "$owner" ]; then
      echo "  PID $pid  tty=$tty  DRIVABLE via screen session '$owner'"
    else
      echo "  PID $pid  tty=$tty  NOT drivable (no screen pty) -- assume it is the user's"
    fi
    echo "      cwd=$cwd  started=$started"
    echo "      $args"
  done < <(ps -eo pid=,tty=,args= | grep -E '(^|/)codex( |$)|codex-linux-x64.*bin/codex$' | grep -v grep | grep -v codex-code-mode-host | sed 's/^ *//')
  [ $found -eq 0 ] && echo "  (none)"
  return 0
}

# ---------------------------------------------------------------- start

cmd_start() {
  local name="${1:-$DEFAULT_NAME}"
  if session_exists "$name"; then
    echo "session '$name' already running"; return 0
  fi
  screen -U -dmS "$name" bash -lc "cd '$REPO_ROOT' && exec codex --sandbox workspace-write -a never" \
    || die "failed to start screen session"

  # Answer the "Do you trust this directory?" prompt if it appears.
  local tmp; tmp="$(mktemp)"
  for _ in $(seq 1 20); do
    sleep 1
    screen -S "$name" -X hardcopy "$tmp" 2>/dev/null
    if grep -qi 'trust the contents' "$tmp"; then
      screen -S "$name" -X stuff $'\r'
      sleep 2
      break
    fi
    grep -q 'OpenAI Codex' "$tmp" && break
  done
  rm -f "$tmp"
  session_exists "$name" || die "codex exited during startup (check 'codex doctor')"
  echo "started '$name' in $REPO_ROOT"
}

# ---------------------------------------------------------------- send

cmd_send() {
  local name="${1:-$DEFAULT_NAME}"; shift || true
  local msg="$*"
  [ -n "$msg" ] || die "nothing to send"
  session_exists "$name" || die "no screen session '$name' (run: $0 start $name)"
  case "$msg" in *$'\n'*) die "message must be a single line; put the content in claude-speak.md";; esac

  # screen's `stuff` interprets backslash escapes -- neutralise them.
  local esc="${msg//\\/\\\\}"
  screen -S "$name" -X stuff "$esc" || die "stuff failed"
  sleep 1
  screen -S "$name" -X stuff $'\r' || die "submit failed"
  echo "sent to '$name': $msg"
}

# ---------------------------------------------------------------- status

cmd_status() {
  local name="${1:-$DEFAULT_NAME}"
  session_exists "$name" || die "no screen session '$name'"
  local tmp; tmp="$(mktemp)"
  screen -S "$name" -X hardcopy "$tmp"
  cat "$tmp"
  rm -f "$tmp"
}

# ---------------------------------------------------------------- wait

cmd_wait() {
  local name="${1:-$DEFAULT_NAME}"
  local timeout="${2:-600}"
  local before; before="$(md5sum "$REPLY_FILE" 2>/dev/null | cut -d' ' -f1)"
  local waited=0
  while [ $waited -lt "$timeout" ]; do
    sleep 10; waited=$((waited + 10))
    local now; now="$(md5sum "$REPLY_FILE" 2>/dev/null | cut -d' ' -f1)"
    if [ "$now" != "$before" ]; then
      echo "codex-speak.md changed after ${waited}s"; return 0
    fi
    session_exists "$name" || { echo "session '$name' died after ${waited}s"; return 2; }
  done
  echo "timeout: codex-speak.md unchanged after ${timeout}s"; return 1
}

# ---------------------------------------------------------------- stop

cmd_stop() {
  local name="${1:-$DEFAULT_NAME}"
  session_exists "$name" || { echo "no session '$name'"; return 0; }
  screen -S "$name" -X quit
  sleep 1
  screen -wipe >/dev/null 2>&1
  echo "stopped '$name'"
}

case "${1:-list}" in
  list)   cmd_list ;;
  start)  shift; cmd_start "$@" ;;
  send)   shift; cmd_send "$@" ;;
  status) shift; cmd_status "$@" ;;
  wait)   shift; cmd_wait "$@" ;;
  stop)   shift; cmd_stop "$@" ;;
  *) die "unknown command '${1}'; use list|start|send|status|wait|stop" ;;
esac

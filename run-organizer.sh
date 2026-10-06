#!/bin/zsh
# run-organizer.sh — the autonomous brain-aware Finder organizer cycle.
# Driven by launchd (com.joseph.finder-organizer, weekly). Runs via /bin/zsh so
# it holds Full-Disk-Access for ~/Desktop + ~/Documents (the TCC/FDA rule).
#
#   preflight → scan → codex classify → SAFE apply → audit → daily-note beacon
#
# The agent (Codex headless) only RETURNS A JSON PLAN. organizer.py performs
# every move behind its safety gates (A=0 ref-audit, pin-skip, never-delete,
# dest-allowlist, confidence threshold, manifest). Fully reversible: `undo`.
#
# Flags:  --dry-run     classify + audit but move nothing (first runs use this)
#         --preflight   ONLY check the codex CLI + config; no scan, no codex
#                       exec, no moves, no vault writes. Safe to run any time.
#                       Writes state/last_preflight.json, NEVER last_status.json.
#
# Status files:  state/last_status.json    = last real cycle (live / dry-run)
#                state/last_preflight.json = last --preflight check
# Self-test of the failure notification path: tests/test-notify.sh
#
# 2026-09-23 repair (why this file changed): every Sunday 2026-07-12 → 09-20
# died at "codex classify FAILED". Cause: this script called the npm CLI
# /opt/homebrew/bin/codex (0.133.0, installed 2026-05-24, never updated) while
# the ChatGPT/Codex desktop app keeps rewriting ~/.codex/config.toml to its
# newest model (gpt-5.6-sol, -luna, gpt-6-astra) and effort levels ("ultra",
# "max") that only a newer client understands → server 400 "requires a newer
# version of Codex", or a config parse error. Fix: prefer the codex binary the
# app ships (it always matches the config the app writes), refuse to call a
# CLI older than the app's, and fail LOUDLY (notification + last_status.json)
# instead of only appending a log line nobody read for 11 weeks.
# Pre-repair copies: run-organizer.sh.pre-r2-20260923 and organizer.py.pre-r2-20260923 (this dir).
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
BASE="$HOME/.config/finder-organizer"
PY="/opt/homebrew/bin/python3"
SCHEMA="$BASE/plan.schema.json"
VAULT="${FINDER_ORGANIZER_VAULT:-$HOME/vault}"
AUDIT_DIR="$VAULT/20 Areas/Organizer-Audits"
STATE="${FINDER_ORGANIZER_STATE:-$BASE/state}"
LOG="$STATE/cycle.log"
STATUS="$STATE/last_status.json"
MODELS_CACHE="$HOME/.codex/models_cache.json"
DRY=""
PREFLIGHT_ONLY=0
case "${1:-}" in
  --dry-run)   DRY="--dry-run" ;;
  --preflight) PREFLIGHT_ONLY=1 ;;
  "") ;;
  *) echo "unknown flag: $1 (use --dry-run or --preflight)" >&2; exit 2 ;;
esac
# A preflight-only check records itself SEPARATELY, so running it after a
# failed Sunday can never overwrite that failure with "ok".
# last_status.json = the last real (live or dry-run) cycle, nothing else.
[ $PREFLIGHT_ONLY = 1 ] && STATUS="$STATE/last_preflight.json"
mkdir -p "$STATE"

log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }
DATE="$(date +%Y-%m-%d)"
STAGE="start"

write_status(){  # $1=ok|failed  $2=reason   → $STATUS (atomic replace)
  "$PY" - "$STATUS" "$1" "$STAGE" "$2" "${DRY:-live}" "${CODEX:-}" <<'PYEOF' 2>>"$LOG" \
    || log "WARN: could not write $STATUS (status '$1' at stage ${STAGE} is recorded ONLY in this log)"
import json, os, sys, datetime
path, status, stage, reason, mode, codex = sys.argv[1:]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump({"status": status, "stage": stage, "reason": reason, "mode": mode,
               "codex": codex,
               "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")},
              f, indent=1)
os.replace(tmp, path)
PYEOF
}

# notify TITLE BODY — the text travels as osascript ARGUMENTS, never spliced
# into AppleScript source. (Round-one bug: splicing turned every " into \' ,
# which AppleScript cannot compile, so the codex 400 message — JSON full of
# double quotes — could never be shown: the one failure this was built for.)
# FINDER_ORGANIZER_OSASCRIPT swaps the binary for tests; FINDER_ORGANIZER_TEST_TAG
# prefixes the title so a self-test notification is not mistaken for a real one.
notify(){
  local title="${FINDER_ORGANIZER_TEST_TAG:+[${FINDER_ORGANIZER_TEST_TAG}] }$1" body="${2:0:180}" oerr
  oerr="$("${FINDER_ORGANIZER_OSASCRIPT:-/usr/bin/osascript}" \
    -e 'on run argv' \
    -e 'display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv) sound name "Basso"' \
    -e 'end run' \
    "$body" "$title" "~/.config/finder-organizer/state/cycle.log" 2>&1 >/dev/null)" \
    || { log "WARN: could not post the failure notification: ${oerr:0:200}"; return 1; }
  log "notification posted: ${title}"
}

# fail = the ONLY way out on error: log line + status file + a macOS
# notification a human actually sees. (Set FINDER_ORGANIZER_NO_NOTIFY=1 in tests.)
fail(){
  local reason="$1"
  log "FAILED at ${STAGE}: ${reason} — abort (no moves made unless stage=audit)"
  write_status failed "$reason"
  if [ "${FINDER_ORGANIZER_NO_NOTIFY:-0}" != "1" ]; then
    notify "Finder organizer FAILED (${STAGE})" "$reason"
  fi
  exit 1
}

# version_lt A B → exit 0 when A < B (numeric x.y.z; pre-release tags ignored)
version_lt(){
  "$PY" - "$1" "$2" <<'PYEOF'
import re, sys
def v(s):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", s or "")
    return tuple(int(x) for x in m.groups()) if m else None
a, b = v(sys.argv[1]), v(sys.argv[2])
sys.exit(0 if (a and b and a < b) else 1)
PYEOF
}

# ---- codex CLI resolution -------------------------------------------------
# An explicit FINDER_ORGANIZER_CODEX wins. Otherwise pick the NEWEST of every
# codex binary on the machine: the one shipped inside the desktop app, the
# VS Code extension's, and the npm/brew CLI. The desktop app and the VS Code
# extension both rewrite ~/.codex/config.toml and models_cache.json, so the
# newest client is the one most likely to understand what they wrote.
resolve_codex(){
  if [ -n "${FINDER_ORGANIZER_CODEX:-}" ]; then
    [ -x "$FINDER_ORGANIZER_CODEX" ] && { echo "$FINDER_ORGANIZER_CODEX"; return 0; }
    echo "FINDER_ORGANIZER_CODEX=$FINDER_ORGANIZER_CODEX is not executable" >&2
    return 1
  fi
  local c v best="" bestv="0.0.0"
  for c in /Applications/ChatGPT.app/Contents/Resources/codex \
           /Applications/Codex.app/Contents/Resources/codex \
           $HOME/.vscode/extensions/openai.chatgpt-*/bin/macos-aarch64/codex(N) \
           /opt/homebrew/bin/codex \
           "$(command -v codex 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    v="$("$c" --version 2>/dev/null | head -1)"
    [ -n "$v" ] || continue
    if [ -z "$best" ] || version_lt "$bestv" "$v"; then best="$c"; bestv="$v"; fi
  done
  [ -n "$best" ] && { echo "$best"; return 0; }
  return 1
}

preflight(){
  STAGE="preflight"
  CODEX="$(resolve_codex)" || fail "no usable codex CLI found (checked ChatGPT/Codex app bundle, VS Code extension, /opt/homebrew/bin, PATH)"
  local ver
  ver="$("$CODEX" --version 2>&1 | head -1)"
  [ -n "$ver" ] || fail "codex at $CODEX printed no version"
  log "codex: $CODEX ($ver)"
  # The desktop app records the client version its model catalog was fetched
  # for. A CLI older than that is exactly the 2026-07..09 failure: the server
  # rejects the app's default model for an old client.
  local appver=""
  if [ -r "$MODELS_CACHE" ]; then
    appver="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("client_version",""))' "$MODELS_CACHE" 2>/dev/null)"
  fi
  if [ -z "$appver" ]; then
    log "WARN: cannot read client_version from $MODELS_CACHE — version gate NOT CHECKED"
  elif version_lt "$ver" "$appver"; then
    fail "codex CLI $ver is older than the Codex app's $appver; the app's default model in ~/.codex/config.toml would be rejected. Update the CLI or set FINDER_ORGANIZER_CODEX."
  else
    log "codex version gate ok (cli $ver >= app catalog $appver)"
  fi
  # Parse ~/.codex/config.toml with THIS binary (local only, no network):
  # catches e.g. model_reasoning_effort="ultra" that an old client rejects.
  local perr="$STATE/codex_preflight.err"
  if ! "$CODEX" features list >/dev/null 2>"$perr"; then
    fail "codex cannot load ~/.codex/config.toml: $(grep -v '^[[:space:]]*$' "$perr" | head -2 | tr '\n' ' ' | cut -c1-220)"
  fi
  log "codex config loads ok"
}

if [ $PREFLIGHT_ONLY = 1 ]; then log "=== finder-organizer PREFLIGHT-ONLY (no scan, no classify, no moves) ==="; else log "=== finder-organizer cycle start ${DRY:-(LIVE)} ==="; fi

# 0) preflight — before touching anything
preflight
if [ $PREFLIGHT_ONLY = 1 ]; then
  STAGE="preflight"; DRY="preflight-only"; write_status ok "preflight only: codex usable, nothing scanned or moved"
  log "=== preflight done (no scan, no classify, no moves) ==="
  exit 0
fi

# 0b) Zeta Mac downloads — deterministic, no model (added 2026-10-02). Keeps the
# best dashboard + newest DB in ~/Downloads and files older copies; never deletes.
# Non-fatal on purpose: a failure here must not stop the organizer cycle.
STAGE="zeta-tidy"
"$PY" "$BASE/zeta-tidy.py" $DRY >> "$LOG" 2>&1 || log "WARN: zeta-tidy.py exited non-zero (non-fatal; organizer cycle continues)"

# 1) scan
STAGE="scan"
"$PY" "$BASE/organizer.py" scan >> "$LOG" 2>&1 || fail "organizer.py scan exited non-zero (see traceback in cycle.log)"

# 2) build prompt + classify via Codex headless (agent only returns a JSON plan)
PROMPT="$STATE/prompt.txt"
PLAN="$STATE/plan_${DATE}.json"
RAW="$STATE/codex_raw_${DATE}.json"
STAGE="prompt"
"$PY" "$BASE/organizer.py" prompt --out "$PROMPT" >> "$LOG" 2>&1 || fail "prompt build exited non-zero"

STAGE="classify"
log "classifying via codex exec (read-only sandbox, schema-enforced) ..."
# Codex is locally authenticated (~/.codex/auth.json). read-only sandbox = it
# can reason/read but CANNOT move files; only organizer.py moves, behind gates.
# Remove any same-day RAW first so a run that writes nothing cannot be
# mistaken for a plan from an earlier run.
rm -f "$RAW"
COUT="$STATE/codex_out_${DATE}.log"
"$CODEX" exec --skip-git-repo-check -s read-only --ephemeral \
  -C "$BASE" --output-schema "$SCHEMA" -o "$RAW" - < "$PROMPT" > "$COUT" 2>&1
CRC=$?
cat "$COUT" >> "$LOG"
if [ $CRC -ne 0 ]; then
  fail "codex classify FAILED (exit $CRC): $(grep '^ERROR: ' "$COUT" | tail -1 | cut -c1-220)"
fi
[ -s "$RAW" ] || fail "codex exec exited 0 but wrote no plan to $RAW"

# the last agent message ($RAW) should already be schema-valid JSON; tolerate fences
STAGE="parse"
"$PY" - "$RAW" "$PLAN" <<'PYEOF' >> "$LOG" 2>&1 || fail "plan parse failed (no moves made)"
import json, re, sys
text = open(sys.argv[1]).read()
m = re.search(r'\{.*"plan".*\}', text, re.S)
obj = json.loads(m.group(0) if m else text)
assert isinstance(obj.get("plan"), list), "no plan array"
json.dump(obj, open(sys.argv[2], "w"), indent=1)
print(f"parsed plan: {len(obj['plan'])} item(s)")
PYEOF

# 3) apply behind the safety gates (or dry-run)
STAGE="apply"
"$PY" "$BASE/organizer.py" apply "$PLAN" $DRY >> "$LOG" 2>&1 || fail "organizer.py apply exited non-zero; check state/manifest_${DATE}.tsv for any moves made"

# 4) audit → vault
STAGE="audit"
mkdir -p "$AUDIT_DIR" 2>/dev/null
"$PY" "$BASE/organizer.py" audit --out "$AUDIT_DIR/${DATE}.md" >> "$LOG" 2>&1 \
  || fail "audit note not written (apply already ran; moves are in state/manifest_${DATE}.tsv, undo works)"
[ -s "$AUDIT_DIR/${DATE}.md" ] || fail "audit exited 0 but $AUDIT_DIR/${DATE}.md is missing or empty"

STAGE="beacon"
# 5) daily-note beacon
DAILY="$VAULT/50 Daily/${DATE}.md"
if [ -f "$DAILY" ]; then
  MOVED=$("$PY" -c "import json;print(len(json.load(open('$STATE/apply_report_${DATE}.json'))['moved']))" 2>/dev/null || echo "?")
  FLAG=$("$PY" -c "import json;print(len(json.load(open('$STATE/apply_report_${DATE}.json'))['flagged']))" 2>/dev/null || echo "?")
  "$PY" - "$DAILY" "$DATE" "${DRY:-}" "$MOVED" "$FLAG" <<'PYEOF' >> "$LOG" 2>&1
import sys

path, date, dry, moved, flagged = sys.argv[1:]
mode = "dry-run" if dry else "live"
verb = "would move" if dry else "moved"
line = (
    f"- {date} — **Finder Organizer** ran {mode}: {moved} {verb}, "
    f"{flagged} flagged for review. Audit: [[Organizer-Audits/{date}]]. "
    "Reversible via `organizer.py undo`."
)

text = open(path).read()
heading = "## Changes Codex made today"
if line in text:
    raise SystemExit(0)
idx = text.find(heading)
if idx == -1:
    text = text.rstrip() + f"\n\n{heading}\n\n{line}\n"
else:
    insert_at = text.find("\n## ", idx + len(heading))
    if insert_at == -1:
        insert_at = len(text)
    text = text[:insert_at].rstrip() + "\n\n" + line + "\n" + text[insert_at:]
open(path, "w").write(text)
print(f"daily note updated under {heading}")
PYEOF
else
  log "no daily note at 50 Daily/${DATE}.md — beacon skipped (audit note was written)"
fi
STAGE="done"
write_status ok "${DRY:-live} cycle complete; audit 20 Areas/Organizer-Audits/${DATE}.md"
log "=== cycle done ==="

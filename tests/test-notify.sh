#!/bin/zsh
# test-notify.sh — self-test for run-organizer.sh's failure path.
# Runs ONLY in a throwaway sandbox: fake codex, stub organizer.py, scratch
# state + vault. It never scans, never calls a real codex, never moves a file,
# never touches the real state/ or the real vault.
#
#   tests/test-notify.sh          compile-only: osascript is a stub that
#                                 osacompiles the wrapper's exact AppleScript
#                                 and records the text it would have shown.
#   tests/test-notify.sh --real   also posts ONE real notification titled
#                                 "[SELF-TEST] Finder organizer FAILED (classify)".
#
# Exit 0 = every check passed. Any failure prints FAIL and exits 1.
set -u
REAL=0; [ "${1:-}" = "--real" ] && REAL=1
SRC="${0:A:h:h}/run-organizer.sh"
[ -r "$SRC" ] || { echo "FAIL: cannot read $SRC"; exit 1; }
T="$(mktemp -d "${TMPDIR:-/tmp}/finder-organizer-test.XXXXXX")" || exit 1
trap 'rm -rf "$T"' EXIT
PASS=0; FAILS=0
ok(){ echo "ok   - $*"; PASS=$((PASS+1)); }
no(){ echo "FAIL - $*"; FAILS=$((FAILS+1)); }

# The exact server text from the 2026-09-20 failure, plus a backslash and a
# single quote so every AppleScript-hostile character is present.
ERRLINE='ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The '\''gpt-5.6-sol'\'' model requires a newer version of Codex. Please upgrade to the latest app or CLI and try again."}} C:\path\x'

mkdir -p "$T/base/state" "$T/vault/50 Daily" "$T/bin"
print -r -- "$ERRLINE" > "$T/errline.txt"
cp "$SRC" "$T/base/run-organizer.sh"
# point the copy at the sandbox (these two lines are the only difference)
/usr/bin/sed -i '' \
  -e "s|^BASE=.*|BASE=\"$T/base\"|" \
  -e "s|^VAULT=.*|VAULT=\"$T/vault\"|" "$T/base/run-organizer.sh"
grep -q "^BASE=\"$T/base\"" "$T/base/run-organizer.sh" && grep -q "^VAULT=\"$T/vault\"" "$T/base/run-organizer.sh" \
  || { echo "FAIL: could not sandbox BASE/VAULT"; exit 1; }
echo '{}' > "$T/base/plan.schema.json"
# stub organizer.py: scan/prompt succeed; anything else is a test bug
cat > "$T/base/organizer.py" <<'EOF'
import sys
cmd = sys.argv[1]
if cmd == "scan": print("stub scan"); sys.exit(0)
if cmd == "prompt": open(sys.argv[sys.argv.index("--out")+1], "w").write("stub prompt"); sys.exit(0)
print("STUB organizer.py called with unexpected", sys.argv[1:]); sys.exit(97)
EOF

# fake codex. MODE=badconfig → `features list` fails; MODE=fail400 → exec fails.
cat > "$T/bin/codex" <<EOF
#!/bin/zsh
case "\$1" in
  --version) echo "codex-cli 999.0.0"; exit 0 ;;
  features) [ "\${FAKE_MODE:-}" = badconfig ] && { cat "$T/errline.txt" >&2; exit 1; }; exit 0 ;;
  exec) cat "$T/errline.txt"; exit 1 ;;
esac
exit 9
EOF
chmod +x "$T/bin/codex"

# stub osascript: compile the wrapper's exact source (must succeed), record the
# body argument, optionally hand off to the real osascript.
cat > "$T/bin/osascript" <<EOF
#!/bin/zsh
src=(); while [ "\$1" = "-e" ]; do src+=("-e" "\$2"); shift 2; done
/usr/bin/osacompile -o "$T/n.scpt" "\${src[@]}" 2> "$T/compile.err" || exit 11
print -rn -- "\$1" > "$T/body.txt"
print -rn -- "\$2" > "$T/title.txt"
[ "\${REAL_NOTIFY:-0}" = 1 ] && exec /usr/bin/osascript "\${src[@]}" "\$@"
exit 0
EOF
chmod +x "$T/bin/osascript"

run(){  # $1 = FAKE_MODE, rest = wrapper args
  local mode="$1"; shift
  rm -f "$T/compile.err" "$T/body.txt" "$T/title.txt"
  env -i HOME="$HOME" PATH=/usr/bin:/bin FAKE_MODE="$mode" \
    FINDER_ORGANIZER_CODEX="$T/bin/codex" FINDER_ORGANIZER_OSASCRIPT="$T/bin/osascript" \
    FINDER_ORGANIZER_TEST_TAG="SELF-TEST" REAL_NOTIFY="${REAL_NOTIFY:-0}" \
    /bin/zsh "$T/base/run-organizer.sh" "$@" > "$T/out.txt" 2>&1
}
field(){ /opt/homebrew/bin/python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2" 2>/dev/null; }

# ---- 1. classify fails with the real 400 text → notification must compile
rm -f "$T/body.txt"
REAL_NOTIFY=$REAL run fail400; rc=$?
[ $rc = 1 ] && ok "fail400: wrapper exit 1" || no "fail400: wrapper exit $rc (want 1)"
[ "$(field "$T/base/state/last_status.json" stage)" = classify ] && [ "$(field "$T/base/state/last_status.json" status)" = failed ] \
  && ok "fail400: last_status.json = failed/classify" || no "fail400: last_status.json wrong"
if [ -s "$T/compile.err" ] || [ ! -e "$T/body.txt" ]; then no "fail400: notifier not called, or AppleScript did not compile"; else ok "fail400: osacompile of the notification script rc=0"; fi
body="$(cat "$T/body.txt" 2>/dev/null)"
expect="$(field "$T/base/state/last_status.json" reason)"
[ -n "$body" ] && [ "$body" = "${expect:0:180}" ] && ok "fail400: notification body = reason[:180], quotes intact" || no "fail400: body mismatch or missing"
case "$body" in *'"status":400'*) ok "fail400: body still contains the JSON double quotes" ;; *) no "fail400: double quotes were mangled" ;; esac
grep -q 'WARN: could not post' "$T/base/state/cycle.log" && no "fail400: cycle.log says the notification failed" || ok "fail400: no 'could not post' WARN"
grep -q 'notification posted: \[SELF-TEST\] Finder organizer FAILED (classify)' "$T/base/state/cycle.log" && ok "fail400: 'notification posted' logged" || no "fail400: no 'notification posted' line"
[ $REAL = 1 ] && echo "     (--real: a [SELF-TEST] notification should now be on screen)"

# ---- 2. a notifier that fails must be LOGGED, not swallowed
printf '#!/bin/sh\necho "boom: no notification center" >&2\nexit 5\n' > "$T/bin/badosa"; chmod +x "$T/bin/badosa"
env -i HOME="$HOME" PATH=/usr/bin:/bin FAKE_MODE=fail400 FINDER_ORGANIZER_CODEX="$T/bin/codex" \
  FINDER_ORGANIZER_OSASCRIPT="$T/bin/badosa" /bin/zsh "$T/base/run-organizer.sh" >/dev/null 2>&1
grep -q 'WARN: could not post the failure notification: boom' "$T/base/state/cycle.log" \
  && ok "broken notifier: WARN with its stderr is logged" || no "broken notifier: failure was silent"

# ---- 3. --preflight never overwrites last_status.json
cp "$T/base/state/last_status.json" "$T/before.json"
run good --preflight; rc=$?
[ $rc = 0 ] && ok "preflight ok: exit 0" || no "preflight ok: exit $rc"
cmp -s "$T/before.json" "$T/base/state/last_status.json" && ok "preflight ok: last_status.json untouched (still the failed cycle)" || no "preflight ok: last_status.json was overwritten"
[ "$(field "$T/base/state/last_preflight.json" status)" = ok ] && ok "preflight ok: last_preflight.json = ok" || no "preflight ok: last_preflight.json missing/wrong"

run badconfig --preflight; rc=$?
[ $rc = 1 ] && ok "preflight bad config: exit 1" || no "preflight bad config: exit $rc"
cmp -s "$T/before.json" "$T/base/state/last_status.json" && ok "preflight bad config: last_status.json untouched" || no "preflight bad config: last_status.json overwritten"
[ "$(field "$T/base/state/last_preflight.json" status)" = failed ] && ok "preflight bad config: last_preflight.json = failed" || no "preflight bad config: last_preflight.json wrong"
if [ -s "$T/compile.err" ] || [ ! -e "$T/body.txt" ]; then no "preflight bad config: notifier not called, or AppleScript did not compile"; else ok "preflight bad config: notification compiles"; fi
case "$(cat "$T/body.txt" 2>/dev/null)" in *'"status":400'*) ok "preflight bad config: notifier got the double-quoted text intact" ;; *) no "preflight bad config: notifier never called or text mangled" ;; esac

echo "---- $PASS passed, $FAILS failed"
[ $FAILS = 0 ]

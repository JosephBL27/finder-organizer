#!/opt/homebrew/bin/python3
"""zeta-tidy.py — deterministic filing of Zeta Mac downloads (added 2026-10-02).

Why: every ZETA session ends with a fresh `zetamac_dashboard_latest*.html`
(and often a DB / handoff note) landing in ~/Downloads. The LLM classifier in
organizer.py always answered "leave" for them, so 57 dashboards piled up by
2026-10-02. This step is plain code, no model: it keeps the dashboard with the MOST
trials plus the newest file of each kind in ~/Downloads (Joseph uploads it to the
next chat) and files the rest.

  dashboards / databases, older than the newest:
      verified superseded  → ~/Downloads/_Sorted/_Review-for-deletion/ZETA superseded copies/
      could not verify     → ~/Downloads/_Sorted/ZETA snapshots/           (kept)
  handoff notes (NEXT_CHAT_HANDOFF*.md, PROJECT_BRIEF*.md with a ZETA header),
  older than the newest of each → ~/Downloads/_Sorted/ZETA handoff history/ (kept)

"Verified superseded" means: every trial (`capturedAt`) in the old copy is in
the newest dashboard with the same number of `records` and the same
`scoreObserved`. Anything that fails to parse is treated as NOT verified.

Safety: never deletes, never overwrites, skips files younger than 2 hours,
writes each manifest line (flushed) BEFORE the move. Reverse a run by moving
`new_path` back to `original_path` from state/zeta_manifest_<date>.tsv.
The canonical workspace ~/Documents/ZETA is never written by this script.

Usage: zeta-tidy.py [--dry-run]     (ZETA_TIDY_ROOT / ZETA_TIDY_STATE override paths for tests)
"""
import datetime
import fnmatch
import json
import os
import re
import sys
import time

ROOT = os.path.expanduser(os.environ.get("ZETA_TIDY_ROOT", "~/Downloads"))
STATE = os.path.expanduser(os.environ.get("ZETA_TIDY_STATE", "~/.config/finder-organizer/state"))
SORTED = os.path.join(ROOT, "_Sorted")
DEST_SUPERSEDED = os.path.join(SORTED, "_Review-for-deletion", "ZETA superseded copies")
DEST_SNAPSHOTS = os.path.join(SORTED, "ZETA snapshots")
DEST_NOTES = os.path.join(SORTED, "ZETA handoff history")
MIN_AGE_SEC = 2 * 3600
DRY = "--dry-run" in sys.argv[1:]

FAMILIES = {
    "dashboard": "zetamac_dashboard_latest*.html",
    "database": "zetamac_running_database*.json",
    "handoff": "NEXT_CHAT_HANDOFF*.md",
    "brief": "PROJECT_BRIEF*.md",
}
_DEC = json.JSONDecoder()


def trials_of(path):
    """Trial list of a dashboard (SEED_TRIALS) or database (root['trials']); None if unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if path.endswith(".html"):
            m = re.search(r"\bSEED_TRIALS\s*=\s*", text)
            if not m:
                return None
            trials = _DEC.raw_decode(text, m.end())[0]
        else:
            trials = json.loads(text).get("trials")
        if not isinstance(trials, list) or not all(isinstance(t, dict) and t.get("capturedAt") for t in trials):
            return None
        return trials
    except Exception:
        return None


def superseded_by(old_trials, newest_by_key):
    if old_trials is None or newest_by_key is None:
        return False
    for t in old_trials:
        b = newest_by_key.get(t["capturedAt"])
        if b is None:
            return False
        if len(t.get("records") or []) != len(b.get("records") or []):
            return False
        if t.get("scoreObserved") != b.get("scoreObserved"):
            return False
    return True


def is_zeta_note(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return bool(re.search(r"zeta", fh.read(400), re.I))
    except Exception:
        return False


def unique_dest(folder, name):
    dest = os.path.join(folder, name)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(name)
    return os.path.join(folder, f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}{ext}")


def main():
    now = time.time()
    groups = {k: [] for k in FAMILIES}
    try:
        names = os.listdir(ROOT)
    except OSError as e:
        print(f"zeta-tidy: cannot list {ROOT}: {e}")
        return 1
    for name in names:
        full = os.path.join(ROOT, name)
        if name.startswith(".") or not os.path.isfile(full) or os.path.islink(full):
            continue
        for fam, pat in FAMILIES.items():
            if fnmatch.fnmatch(name, pat):
                if fam in ("handoff", "brief") and not is_zeta_note(full):
                    break
                groups[fam].append((os.path.getmtime(full), name))
                break

    plan = []  # (src, dest_folder, reason)
    # Reference dashboard = the readable copy with the MOST trials (ties: newest).
    # The newest file by date is kept as well, so a broken or half-finished latest
    # download can never cause the real latest dashboard to be filed away.
    parsed = {name: trials_of(os.path.join(ROOT, name)) for _, name in groups["dashboard"]}
    readable = []
    for mtime, name in groups["dashboard"]:
        tr = parsed.get(name)
        if tr:
            readable.append((len(tr), mtime, name))
    reference = max(readable)[2] if readable else None
    newest_by_key = {t["capturedAt"]: t for t in (parsed.get(reference) or [])} if reference else None
    keep = {"dashboard": {reference} if reference else set(), "database": set()}
    for fam in ("dashboard", "database"):
        if groups[fam]:
            keep[fam].add(max(groups[fam])[1])

    for fam in ("dashboard", "database"):
        for mtime, name in sorted(groups[fam]):
            if name in keep[fam] or now - mtime < MIN_AGE_SEC:
                continue
            old_trials = parsed.get(name) if fam == "dashboard" else trials_of(os.path.join(ROOT, name))
            ok = superseded_by(old_trials, newest_by_key)
            plan.append((name, DEST_SUPERSEDED if ok else DEST_SNAPSHOTS,
                         f"older zeta {fam}; " + (f"every trial verified present in {reference}" if ok
                                                  else "NOT verified against the reference dashboard, kept as a snapshot")))
    for fam in ("handoff", "brief"):
        items = sorted(groups[fam])
        for mtime, name in items[:-1]:
            if now - mtime < MIN_AGE_SEC:
                continue
            plan.append((name, DEST_NOTES, f"older zeta {fam} note (history, kept)"))

    kept = {fam: sorted(keep[fam]) if fam in keep else ([max(v)[1]] if v else []) for fam, v in groups.items()}
    print(f"zeta-tidy{' (dry-run)' if DRY else ''}: found "
          + ", ".join(f"{len(v)} {k}" for k, v in groups.items())
          + f"; to file: {len(plan)}; kept in place: {kept}")
    if not plan:
        return 0
    if DRY:
        for name, folder, reason in plan:
            print(f"  would move {name} -> {folder} ({reason})")
        return 0

    os.makedirs(STATE, exist_ok=True)
    manifest = os.path.join(STATE, f"zeta_manifest_{datetime.date.today().isoformat()}.tsv")
    new = not os.path.exists(manifest)
    moved = 0
    with open(manifest, "a", encoding="utf-8") as man:
        if new:
            man.write("original_path\tnew_path\treason\tbytes\tmoved_at\n")
        for name, folder, reason in plan:
            src = os.path.join(ROOT, name)
            try:
                os.makedirs(folder, exist_ok=True)
                dest = unique_dest(folder, name)
                man.write(f"{src}\t{dest}\t{reason}\t{os.path.getsize(src)}\t"
                          f"{datetime.datetime.now().isoformat(timespec='seconds')}\n")
                man.flush()
                os.rename(src, dest)  # same volume; never overwrites (dest is unique)
                moved += 1
            except OSError as e:
                print(f"  SKIP {name}: {e}")
    print(f"zeta-tidy: moved {moved} file(s); manifest {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

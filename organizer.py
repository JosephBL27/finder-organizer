#!/usr/bin/env python3
"""
Finder Organizer — deterministic SAFETY HARNESS for the brain-aware autonomous
Finder organizer (2026-06-25).

This script never decides *where* a file goes — a high-context agent (Codex
headless, see run-organizer.sh) does that. This script's whole job is to make
those moves SAFE, REVERSIBLE, and AUDITED:

  scan   → enumerate loose candidate files in the managed roots, extract a
           content preview + macOS download-source URL + metadata for each,
           write state/proposal_<ts>.json  (the agent reads this).
  apply  → take the agent's plan_<ts>.json and execute ONLY approved moves,
           each gated by: file-unchanged-since-scan (sha), pin-skip,
           A=0 ref-audit (not referenced by any agent/script), dest-allowlist,
           min-age, confidence-threshold, NEVER-delete. Logs every action +
           every skip to state/manifest_<date>.tsv.
  audit  → render the run (moved / flagged / skipped) as markdown for the brain.
  undo   → reverse a manifest (dest → src) — full reversibility.

Invariants (enforced here, regardless of what the agent says):
  * NEVER deletes anything. Junk is quarantined, not removed.
  * NEVER moves a pinned file or a file referenced anywhere (A=0 rule).
  * NEVER moves into a path outside the destination allowlist.
  * NEVER acts on a file whose bytes changed since scan.
  * Every move is appended to a dated manifest → undo restores it.
"""

import argparse
import datetime as _dt
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".config", "finder-organizer")
# The Obsidian vault the organizer reads its filing taxonomy from.
VAULT = os.path.expanduser(os.environ.get("FINDER_ORGANIZER_VAULT", "~/vault"))
STATE = os.path.join(BASE, "state")
CONFIG_PATH = os.path.join(BASE, "config.json")

# ---------------------------------------------------------------- defaults ---
DEFAULTS = {
    # roots whose depth-1 loose files are candidates for organization
    "roots": [
        {"path": "~/Downloads", "managed_sink": "~/Downloads/_Sorted"},
        {"path": "~/Desktop",   "managed_sink": "~/Desktop/_Sorted"},
    ],
    # exact basenames / globs that must NEVER be moved (functionally pinned)
    "pins": [
        ".*", "_Sorted", "Makefile",
        "~$*",
        "Resume_Master*.pdf",
        "*.ical*.zip", "client_secret*", "credentials*",
        "Active Study Folder",
        "*.bundle.lock", "vault-*.bundle*",
    ],
    # directory basenames we never descend into or move (live agent workspaces)
    "pinned_dirs": [
        "_Sorted", "Active Study Folder",
        "Resume-Archive", "Library", "Obsidian Vault",
    ],
    # an agent-proposed destination must resolve under one of these roots
    "dest_allowlist": [
        "~/Downloads/_Sorted", "~/Desktop/_Sorted", "~/Documents/_Sorted",
        "~/Documents/Resume-Archive", "~/Coursework", "~/Career", "~/Projects",
    ],
    # destinations needing extra caution → require higher confidence
    "high_caution_dests": ["~/Projects", "~/Coursework", "~/Career"],
    # where junk/installers get quarantined (NEVER auto-deleted)
    "quarantine": "~/Downloads/_Sorted/_Review-for-deletion",
    "min_age_hours": 2.0,          # don't grab in-progress downloads
    "confidence_threshold": 0.75,  # below this → flag, don't move
    "high_caution_threshold": 0.90,
    "preview_chars": 1500,
    # ref-audit haystacks (A=0 rule). Tight scope = fast + avoids agent-log bloat.
    # MUST NOT include this tool's own state dir (proposal/prompt/plan all list
    # every candidate's name → would self-reference every file as "pinned").
    "ref_audit_dirs": [
        "~/.codex/automations", "~/.codex/config.toml", "~/.gemini/config",
        "~/.local/bin",
        os.path.join(VAULT, "90 Meta"),
    ],
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"WARN: bad config.json ({e}); using defaults", file=sys.stderr)
    return cfg


def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def ts_now():
    return _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def today():
    return _dt.datetime.now().strftime("%Y-%m-%d")


# ------------------------------------------------------------- file helpers ---
def is_pinned(name, cfg):
    for pat in cfg["pins"]:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def fingerprint(st):
    """Cheap change-detector: size:mtime. Deliberately does NOT read bytes —
    reading would force a synchronous iCloud download of dataless placeholders
    (Desktop/Documents are iCloud-synced) and block indefinitely."""
    return f"{st.st_size}:{int(st.st_mtime)}"


def fingerprint_path(path):
    try:
        return fingerprint(os.stat(path))
    except OSError:
        return ""


def mdls_raw(path, attr, timeout=4):
    try:
        out = subprocess.run(["/usr/bin/mdls", "-name", attr, "-raw", path],
                             capture_output=True, text=True, timeout=timeout).stdout
        return out.strip()
    except Exception:
        return ""


def mdls_meta(path):
    """One mdls call → {where_froms:[...], title, authors}. Fast (no text content)."""
    meta = {"where_froms": [], "title": "", "authors": ""}
    try:
        out = subprocess.run(
            ["/usr/bin/mdls", "-name", "kMDItemWhereFroms",
             "-name", "kMDItemTitle", "-name", "kMDItemAuthors", path],
            capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return meta
    meta["where_froms"] = re.findall(r'https?://[^\s",)]+', out)[:3]
    mt = re.search(r'kMDItemTitle\s*=\s*"(.*?)"', out, re.S)
    if mt and mt.group(1) != "(null)":
        meta["title"] = mt.group(1)[:120]
    ma = re.search(r'kMDItemAuthors\s*=\s*\(\s*"(.*?)"', out, re.S)
    if ma:
        meta["authors"] = ma.group(1)[:120]
    return meta


MAX_PREVIEW_FILE_BYTES = 30 * 1024 * 1024  # skip text-extraction on huge files


TEXT_EXTS = {"md", "txt", "csv", "tsv", "html", "htm", "json", "py", "js",
             "sh", "tex", "xml", "yaml", "yml", "ics", "ical", "log"}
OFFICE_EXTS = {"rtf", "doc", "docx", "odt", "html", "htm"}


def extract_preview(path, ext, size, meta, cfg):
    n = cfg["preview_chars"]
    ext = ext.lower()
    meta_note = ((" title=" + meta["title"]) if meta["title"] else "") + \
                ((" author=" + meta["authors"]) if meta["authors"] else "")
    try:
        if ext in TEXT_EXTS:
            with open(path, "r", errors="replace") as f:
                return f.read(n * 3)[:n]
        if size and size > MAX_PREVIEW_FILE_BYTES:
            return f"[large file {size//1024//1024}MB — metadata only]{meta_note}"[:n]
        if ext in OFFICE_EXTS:
            out = subprocess.run(["/usr/bin/textutil", "-convert", "txt",
                                  "-stdout", path],
                                 capture_output=True, text=True, timeout=6).stdout
            if out.strip():
                return out.strip()[:n]
        if ext in {"pdf", "pptx", "xlsx", "numbers", "key", "pages"}:
            txt = mdls_raw(path, "kMDItemTextContent", timeout=4)
            if txt and txt != "(null)":
                return txt[:n]
            return (f"[no extractable text]{meta_note}")[:n]
    except subprocess.TimeoutExpired:
        return f"[text-extract timeout — metadata only]{meta_note}"[:n]
    except Exception as e:
        return f"[preview error: {e}]"
    return f"[binary/image/media — no text]{meta_note}"[:n]


def listdir_retry(path, tries=5):
    """os.listdir that retries EINTR. 2026-08-16's scan died on
    InterruptedError listing ~/Downloads (a signal landed mid-readdir; PEP 475
    does not cover every path). Retries a few times, then re-raises LOUDLY."""
    for i in range(tries):
        try:
            return os.listdir(path)
        except InterruptedError:
            if i == tries - 1:
                raise
            print(f"WARN: EINTR listing {path}; retry {i + 1}/{tries - 1}",
                  file=sys.stderr)
            import time
            time.sleep(0.5 * (i + 1))


def candidate_files(cfg):
    """Loose depth-1 entries in each managed root, excluding pins/dotfiles and
    files younger than min_age_hours."""
    out = []
    cutoff = _dt.datetime.now().timestamp() - cfg["min_age_hours"] * 3600
    for root in cfg["roots"]:
        rp = expand(root["path"])
        if not os.path.isdir(rp):
            # was a silent `continue`: a missing/unreadable root looked like
            # "nothing to organize". Say so on stderr (lands in cycle.log).
            print(f"WARN: managed root not a readable directory, skipped: {rp}",
                  file=sys.stderr)
            continue
        for name in sorted(listdir_retry(rp)):
            full = os.path.join(rp, name)
            if name.startswith("."):
                continue
            if is_pinned(name, cfg):
                continue
            if os.path.isdir(full) and name in cfg["pinned_dirs"]:
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_mtime > cutoff:
                continue  # too new — maybe still downloading
            out.append((root, full, name, st))
    return out


# ---------------------------------------------------------------- commands ---
def cmd_scan(cfg, args):
    items = []
    for root, full, name, st in candidate_files(cfg):
        isdir = os.path.isdir(full)
        ext = "" if isdir else name.rsplit(".", 1)[-1] if "." in name else ""
        age_h = round((_dt.datetime.now().timestamp() - st.st_mtime) / 3600, 1)
        meta = mdls_meta(full)
        size = None if isdir else st.st_size
        if isdir:
            try:
                kids = os.listdir(full)
                preview = f"[directory, {len(kids)} item(s): " + \
                          ", ".join(sorted(kids)[:6]) + "]"
            except OSError:
                preview = "[directory]"
        else:
            preview = extract_preview(full, ext, size, meta, cfg)
        item = {
            "path": full,
            "name": name,
            "root": expand(root["path"]),
            "is_dir": isdir,
            "ext": ext,
            "size_bytes": size,
            "age_hours": age_h,
            "modified": _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "fp": fingerprint(st),
            "download_source": meta["where_froms"],
            "preview": preview,
        }
        items.append(item)
    proposal = {
        "generated": ts_now(),
        "roots": [expand(r["path"]) for r in cfg["roots"]],
        "dest_allowlist": [expand(d) for d in cfg["dest_allowlist"]],
        "count": len(items),
        "files": items,
    }
    os.makedirs(STATE, exist_ok=True)
    out_path = args.out or os.path.join(STATE, "proposal_latest.json")
    with open(out_path, "w") as f:
        json.dump(proposal, f, indent=1)
    # also timestamped copy
    with open(os.path.join(STATE, f"proposal_{proposal['generated']}.json"), "w") as f:
        json.dump(proposal, f, indent=1)
    print(f"scanned {len(items)} candidate(s) → {out_path}")
    return 0


def ref_audit_count(name, cfg):
    """A=0 rule: how many agent/script surfaces reference this exact basename.
    0 ⇒ safe to move. Whole-name fixed-string grep (never word-split)."""
    paths = [expand(p) for p in cfg["ref_audit_dirs"]]
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return 0
    try:
        r = subprocess.run(["grep", "-rIlF", "--", name, *paths],
                           capture_output=True, text=True, timeout=40)
        hits = [ln for ln in r.stdout.splitlines() if ln.strip()]
        return len(hits)
    except Exception:
        return 1  # fail safe: treat as referenced → don't move


def dest_is_allowed(dest, cfg):
    d = expand(dest)
    for allowed in cfg["dest_allowlist"]:
        a = expand(allowed)
        if d == a or d.startswith(a + os.sep):
            return True
    return False


def is_high_caution(dest, cfg):
    d = expand(dest)
    for hc in cfg["high_caution_dests"]:
        a = expand(hc)
        if d == a or d.startswith(a + os.sep):
            return True
    return False


def cmd_apply(cfg, args):
    with open(args.plan) as f:
        plan = json.load(f)
    prop_path = args.proposal or os.path.join(STATE, "proposal_latest.json")
    with open(prop_path) as f:
        proposal = json.load(f)
    by_path = {it["path"]: it for it in proposal["files"]}

    manifest = os.path.join(STATE, f"manifest_{today()}.tsv")
    results = {"moved": [], "flagged": [], "skipped": [], "dry_run": args.dry_run}
    # Open the manifest up front and append+flush per move, so a crash/timeout
    # mid-run never leaves moved files untracked (reversibility is durable).
    man_fh = None
    if not args.dry_run:
        new_manifest = not os.path.exists(manifest)
        man_fh = open(manifest, "a")
        if new_manifest:
            man_fh.write("timestamp\taction\toriginal\tmoved_to\tconfidence\treason\n")
            man_fh.flush()
        results["manifest"] = manifest

    for item in plan.get("plan", plan if isinstance(plan, list) else []):
        src = item.get("path", "")
        action = item.get("action", "leave")
        dest = item.get("dest", "")
        conf = float(item.get("confidence", 0))
        reason = item.get("reason", "").replace("\t", " ").replace("\n", " ")
        name = os.path.basename(src)

        def skip(why):
            results["skipped"].append({"path": src, "why": why, "reason": reason})

        if action == "leave":
            results["flagged"].append({"path": src, "why": "agent: leave in place",
                                       "reason": reason, "confidence": conf})
            continue
        if action not in ("move", "quarantine"):
            skip(f"unknown action '{action}'"); continue
        # 1) file must be the exact one we scanned (unchanged)
        scanned = by_path.get(src)
        if not scanned:
            skip("not in latest proposal"); continue
        if not os.path.exists(src):
            skip("vanished since scan"); continue
        if fingerprint_path(src) != scanned.get("fp", ""):
            skip("changed since scan (size/mtime)"); continue
        # 2) pin / dotfile guard (defense in depth)
        if name.startswith(".") or is_pinned(name, cfg):
            skip("pinned/dotfile"); continue
        # 3) resolve destination
        if action == "quarantine":
            dest = cfg["quarantine"]
        if not dest:
            skip("no destination"); continue
        if not dest_is_allowed(dest, cfg):
            skip(f"dest outside allowlist: {dest}"); continue
        # 4) confidence gate
        thr = cfg["high_caution_threshold"] if is_high_caution(dest, cfg) else cfg["confidence_threshold"]
        if conf < thr:
            results["flagged"].append({"path": src, "why": f"confidence {conf} < {thr}",
                                       "dest": dest, "reason": reason, "confidence": conf})
            continue
        # 5) A=0 ref-audit
        refs = ref_audit_count(name, cfg)
        if refs > 0:
            skip(f"referenced by {refs} surface(s) (A>0) — pinned by use"); continue
        # 6) never overwrite
        dest_dir = expand(dest)
        target = os.path.join(dest_dir, name)
        if os.path.exists(target):
            skip(f"target exists: {target}"); continue

        if args.dry_run:
            results["moved"].append({"path": src, "dest": target, "confidence": conf,
                                     "reason": reason, "refs": refs, "dry": True})
            continue
        try:
            os.makedirs(dest_dir, exist_ok=True)
            # log the INTENT before the move, then confirm — so even a hard kill
            # mid-copy leaves a manifest line pointing at where the file is going.
            man_fh.write("\t".join([ts_now(), action, src, target,
                                    f"{conf:.2f}", reason]) + "\n")
            man_fh.flush()
            shutil.move(src, target)
        except Exception as e:
            skip(f"move failed: {e}"); continue
        results["moved"].append({"path": src, "dest": target, "confidence": conf,
                                 "reason": reason, "refs": refs})

    if man_fh:
        man_fh.close()
    out = args.report or os.path.join(STATE, f"apply_report_{today()}.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=1)
    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}apply: {len(results['moved'])} moved, "
          f"{len(results['flagged'])} flagged, {len(results['skipped'])} skipped "
          f"→ {out}")
    return 0


def cmd_audit(cfg, args):
    rep_path = args.report or os.path.join(STATE, f"apply_report_{today()}.json")
    if not os.path.exists(rep_path):
        print(f"no apply report at {rep_path}"); return 1
    with open(rep_path) as f:
        r = json.load(f)
    L = []
    L.append(f"# Finder Organizer — audit {today()}")
    L.append("")
    dry = " (DRY-RUN — nothing actually moved)" if r.get("dry_run") else ""
    L.append(f"> Autonomous brain-aware organization run.{dry} "
             f"Reversible via `organizer.py undo`; manifest: `{r.get('manifest','(none)')}`.")
    L.append("")
    L.append(f"## ✅ Moved ({len(r['moved'])})")
    for m in r["moved"]:
        L.append(f"- `{os.path.basename(m['path'])}` → `{m['dest']}` "
                 f"(conf {m['confidence']}) — {m['reason']}")
    if not r["moved"]:
        L.append("- (none)")
    L.append("")
    L.append(f"## 🚩 Flagged for human review ({len(r['flagged'])}) — NOT moved")
    for fl in r["flagged"]:
        d = f" → would-be `{fl.get('dest')}`" if fl.get("dest") else ""
        L.append(f"- `{os.path.basename(fl['path'])}`{d} — {fl['why']}; {fl.get('reason','')}")
    if not r["flagged"]:
        L.append("- (none)")
    L.append("")
    L.append(f"## ⏭️ Skipped by safety gates ({len(r['skipped'])})")
    for s in r["skipped"]:
        L.append(f"- `{os.path.basename(s['path'])}` — {s['why']}")
    if not r["skipped"]:
        L.append("- (none)")
    L.append("")
    md = "\n".join(L)
    out = args.out
    if out:
        out = expand(out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            f.write(md + "\n")
        print(f"audit written → {out}")
    else:
        print(md)
    return 0


CLASSIFY_RULES = """You are Joseph's brain-aware Finder organizer. You are given a list of LOOSE
files/folders sitting in ~/Downloads and ~/Desktop, each with a content preview,
download-source URL, and metadata. Decide where each belongs based on the brain's
filing taxonomy below. Return STRICT JSON only.

OUTPUT (JSON only, no prose, no markdown fences):
{"plan":[{"path":"<exact path from input>","action":"move|quarantine|leave",
          "dest":"<absolute destination DIRECTORY>","confidence":0.0-1.0,
          "reason":"<one concise sentence>"}]}

ACTIONS:
- "move": file clearly belongs in a destination below → give "dest".
- "quarantine": installers/junk/redownloadable (.dmg/.pkg, duplicate web-page
  "_files" folders, throwaway exports) → dest is the quarantine dir; never deleted.
- "leave": ambiguous, sensitive, looks in-use, or you are not sure → no move.
  WHEN UNSURE, LEAVE. A wrong move is worse than an unsorted file.

DESTINATION ALLOWLIST (a dest OUTSIDE these is rejected by the harness):
%ALLOWLIST%
Quarantine dir: %QUARANTINE%

ROUTING HINTS:
- Prefer a meaningful subdirectory under an allowlisted root when the brain
  names one; do not dump a large coherent training/project set loose into the
  root if a known subfolder is more precise.
- Course videos and exercise workbooks belong in that course's folder under
  ~/Coursework when the brain names one.
- If the correct canonical project directory is known but OUTSIDE the
  destination allowlist, choose "leave" and explain. Do not use generic
  ~/Projects as a substitute for a known out-of-allowlist project path. Example:
  Pushup Alarm app artifacts belong to %PUSHUP_PATH%, so screenshots or build
  artifacts for that app should be left unless a specific allowlisted artifact
  destination is added.

CONFIDENCE: be honest. <0.75 → harness flags it for human review instead of moving
(<0.90 for the high-caution roots ~/Projects, ~/Coursework, ~/Career). Reserve
high confidence for files whose purpose is unmistakable from the evidence.

NEVER propose moving: resume masters, anything that looks like a credential/secret,
dotfiles, or a live agent workspace. The harness also re-checks all of this.

THE BRAIN TAXONOMY (where Joseph's things live — from 90 Meta/Computer Map.md):
%TAXONOMY%

FILES TO CLASSIFY:
%FILES%
"""


def read_taxonomy():
    cm = os.path.join(VAULT, "90 Meta", "Computer Map.md")
    try:
        with open(cm) as f:
            txt = f.read()
    except Exception:
        return "(Computer Map unavailable)"
    # pull the home-structure + filing-convention + dev-sandbox sections
    keep = []
    grab = False
    for line in txt.splitlines():
        if line.startswith("## "):
            grab = any(k in line for k in ("Home directory", "Disk organization",
                       "Filing convention", "Dev sandboxes", "Work & research",
                       "Coursework", "Personal automation"))
        if grab:
            keep.append(line)
    return "\n".join(keep)[:4000]


def cmd_prompt(cfg, args):
    prop_path = args.proposal or os.path.join(STATE, "proposal_latest.json")
    with open(prop_path) as f:
        proposal = json.load(f)
    files_blob = []
    for it in proposal["files"]:
        files_blob.append(json.dumps({
            "path": it["path"], "name": it["name"], "is_dir": it["is_dir"],
            "ext": it["ext"], "size_bytes": it["size_bytes"],
            "download_source": it["download_source"],
            "preview": " ".join((it["preview"] or "").split())[:800],
        }, ensure_ascii=False))
    prompt = (CLASSIFY_RULES
              .replace("%ALLOWLIST%", "\n".join("- " + expand(d) for d in cfg["dest_allowlist"]))
              .replace("%QUARANTINE%", expand(cfg["quarantine"]))
              .replace("%PUSHUP_PATH%", expand("~/Projects/pushup-alarm"))
              .replace("%TAXONOMY%", read_taxonomy())
              .replace("%FILES%", "\n".join(files_blob)))
    out = args.out
    if out:
        with open(expand(out), "w") as f:
            f.write(prompt)
        print(f"prompt written → {out} ({len(prompt)} chars, {proposal['count']} files)")
    else:
        sys.stdout.write(prompt)
    return 0


def cmd_undo(cfg, args):
    man = args.manifest or os.path.join(STATE, f"manifest_{today()}.tsv")
    if not os.path.exists(man):
        print(f"no manifest at {man}"); return 1
    restored, failed = 0, 0
    with open(man) as f:
        lines = f.read().splitlines()
    for ln in lines:
        parts = ln.split("\t")
        if len(parts) < 4 or parts[0] == "timestamp":
            continue
        _, _action, original, moved_to = parts[0], parts[1], parts[2], parts[3]
        if os.path.exists(moved_to) and not os.path.exists(original):
            try:
                os.makedirs(os.path.dirname(original), exist_ok=True)
                shutil.move(moved_to, original)
                restored += 1
            except Exception as e:
                print(f"  undo failed {moved_to}: {e}"); failed += 1
        else:
            failed += 1
    print(f"undo: {restored} restored, {failed} skipped/failed (manifest {man})")
    return 0


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Finder Organizer safety harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("--out")
    a = sub.add_parser("apply")
    a.add_argument("plan"); a.add_argument("--proposal"); a.add_argument("--report")
    a.add_argument("--dry-run", action="store_true")
    au = sub.add_parser("audit"); au.add_argument("--report"); au.add_argument("--out")
    u = sub.add_parser("undo"); u.add_argument("--manifest")
    pr = sub.add_parser("prompt"); pr.add_argument("--proposal"); pr.add_argument("--out")
    args = ap.parse_args()
    return {"scan": cmd_scan, "apply": cmd_apply, "audit": cmd_audit,
            "undo": cmd_undo, "prompt": cmd_prompt}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())

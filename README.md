# finder-organizer

Every Sunday at 11:00 this tidies my Downloads and Desktop folders. A language
model reads each loose file's name, a short preview and where it was downloaded
from, and proposes a folder. A separate Python script decides whether that move
is allowed, logs it, makes it, and can reverse the whole run.

The model proposes; the script decides.

## How it works

```
scan  →  classify  →  gate  →  apply  →  audit
```

1. **Scan** ([organizer.py](organizer.py) `scan`). Lists the loose files in each
   managed folder and fingerprints them by size and date, with a content preview
   and the macOS download-source URL for each.
2. **Classify** ([run-organizer.sh](run-organizer.sh)). The model runs in a
   read-only sandbox and can only return JSON in the fixed shape in
   [plan.schema.json](plan.schema.json): move, quarantine or leave, with a
   destination, a confidence and a one-line reason. It is given the filing
   taxonomy from my Obsidian vault, so it files things where the brain says they
   live.
3. **Gate** (`apply`). Whatever the model said, a move happens only if the file
   is unchanged since the scan, old enough not to be an in-progress download,
   not pinned, not referenced by any other script or agent config, bound for an
   allowlisted folder, and proposed with at least 0.75 confidence (0.90 for
   school, career and project folders).
4. **Apply.** Each move is written to a dated manifest *before* it happens. Junk
   goes to a quarantine folder, never the trash. Nothing is ever deleted.
5. **Audit.** The run is written up as a Markdown report in the vault, and one
   line goes into that day's daily note. `undo` replays the manifest
   backwards.

If any stage fails, the script posts a macOS notification and records the
failure in `state/last_status.json`, so a broken Sunday never hides in a log.

[zeta-tidy.py](zeta-tidy.py) is a small deterministic companion with no model in
it: it files superseded copies of my [ZETA](https://github.com/JosephBL27/zeta)
dashboards, keeping the newest one and any copy it cannot verify.

## Run it

```bash
mkdir -p ~/.config/finder-organizer && cp -R . ~/.config/finder-organizer/
export FINDER_ORGANIZER_VAULT=~/path/to/vault
~/.config/finder-organizer/run-organizer.sh --preflight   # checks the CLI and config only
~/.config/finder-organizer/run-organizer.sh --dry-run     # classifies and audits, moves nothing
~/.config/finder-organizer/run-organizer.sh               # the real cycle
python3 ~/.config/finder-organizer/organizer.py undo      # reverse today's manifest
zsh tests/test-notify.sh                                  # sandboxed self-test of the failure path
```

The pins, allowlist and thresholds are the `DEFAULTS` block at the top of
`organizer.py`; a `config.json` beside it overrides them. I schedule it with a
launchd agent that runs `/bin/zsh run-organizer.sh` on Sundays.

Joseph Blumberg · josephblumberg325@gmail.com

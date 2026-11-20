# Lab 07 worksheet numeric verification hook — disabled

Review `lab07_hooks.disabled.json` and `verify_spinoff_worksheet.py` before
enabling anything. The template is deliberately outside Codex's hook discovery
locations. No `.codex/hooks.json`, user configuration, trust decision or feature
setting has been created or changed. The commands contain this checkout's
absolute path; review it if the checkout moves.

The template wires two synchronous command handlers:

- `PostToolUse` runs after a Bash/exec command finishes or an Edit/Write/apply_patch
  returns. The adapter extracts the actual `spinoff_worksheet_*.md` path from the
  returned tool output (or the edited path), then reads that file. It does not
  guess a latest worksheet or accept a sibling evidence/claims JSON as truth.
- `Stop` rechecks the actual worksheet paths in the current user turn's tool
  outputs in the Codex JSONL transcript and final response. This catches changes
  after the first check and prevents a failed result being followed by "done".
  Neither a previous successful check nor `stop_hook_active` bypasses verification.

The verifier is offline and read-only. Sources are the run's `facts.json`, cited
saved filing texts/XML, printed arithmetic, `spinoff_worksheet.txt`, and the
existing closes reader. It never calls a model, downloads a filing, edits output,
creates a separate claims file, or silently corrects a number. Reader checks use
the exact printed export instrument and interval; when-issued instruments fail.

Supported source syntax follows the existing pipe-delimited worksheet lines.
For an explicit facts-file claim, include `| facts facts.json | tag TAG |
period YYYY-MM-DD through YYYY-MM-DD | unit UNIT | scale EXPONENT`. Its printed
value must equal the saved record's value with exactly that tag, period, unit
and scale. SEC normalized base-unit facts have scale zero. An instant period
contains just the end date. Scale is a power-of-ten exponent, not an inferred
currency or magnitude. Saved filing claims require `| filing PATH_OR_ACCESSION |
section HEADING_OR_line N | date DATE`. Missing/ambiguous source passages fail.
Arithmetic is checked against the worksheet's printed inputs; annual owner cash
flow requires printed matching annual periods and units, never quarterly inputs.

Every numerical amount must have a permitted source. Registry ranks/points are
not exempt: a registry citation alone is not one of the requested evidence kinds.
The earlier content audit of the completed worksheet does not establish that all
its numbers meet this stricter hook. Such a failure must be reported, not waived.

Success is exit **0**, with no stdout or stderr. On the first unsupported or
mismatched amount, exit **2** writes the worksheet path, exact line number, amount,
claimed source, reason and original line to **stderr**. A missing output/source
or malformed hook payload also exits 2. A PostToolUse block does not undo a file
already written; the Stop handler prevents treating it as completed successfully.

These events, discovery locations, and exit-2 blocking semantics follow the
[official Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).
After review and a separate decision to enable, merge the template's hooks into
the project's `.codex/hooks.json` without replacing unrelated hooks, then review
and trust them in Codex. **That installation has not been performed.**

Manual verification from the repository root (does not run the worksheet):

```powershell
python -B verify_spinoff_worksheet.py "research_data/SAVED_RUN/spinoff_worksheet_TIMESTAMP_ID.md"
```

Fictional offline fixtures and the complete offline suite:

```powershell
python -B -m unittest -q test_verify_spinoff_worksheet
python -B -m unittest discover -q
```

---
allowed-tools: Bash(command -v:*), Bash(which:*), Bash(openfoia:*), Bash(curl:*), Bash(pip install:*), Bash(pip3 install:*), Bash(ls:*), Bash(test:*), Bash(cat pyproject.toml)
description: Install or initialize the openfoia CLI — bootstraps everything the plugin needs to function
---

Install and initialize the openfoia CLI on this machine.

**This command touches the user's machine** (downloads a binary, runs pip install, creates `~/.openfoia/`). Walk through each step, warn before each one, and stop if the user declines.

## Step 1 — Check what's already there

Run these in parallel to see what state we're in:

```bash
command -v openfoia
test -f pyproject.toml && cat pyproject.toml | head -5
test -d ~/.openfoia && ls ~/.openfoia
```

Based on the result, pick one of the branches below.

## Branch A — openfoia is already installed

If `command -v openfoia` prints a path:

1. Confirm with the user: "openfoia is already at `<path>`. Version is `<run: openfoia --version>`. Nothing to install."
   - `--version` was added in 3.3.0. If it errors with "No such option", the install predates it — use `pip show openfoia | head -2` instead and mention that an upgrade is available.
2. If `~/.openfoia/` doesn't exist, offer to run `openfoia init` (creates the DB + loads 53 federal agencies).
3. Done. Suggest `/foia-search <topic>` as the next step.

## Branch B — we're in the openfoia repo (source install)

If `pyproject.toml` exists in cwd and contains `name = "openfoia"`:

1. Warn the user: "I'll install openfoia from this local repo with `pip install -e '.[dev]'`. This puts openfoia on your PATH in editable mode. OK?"
2. On approval, run:
   ```bash
   pip install -e ".[dev]"
   ```
3. Then initialize the DB:
   ```bash
   openfoia init
   ```
4. Confirm with `openfoia --version`.

## Branch C — fresh install from the web

If openfoia isn't installed and we're not in the repo:

1. Warn the user: "I'll download and run https://raw.githubusercontent.com/JordanCoin/openfoia/main/install.sh. This will fetch a precompiled `pdf-extract` binary, pip-install openfoia, and create `~/.openfoia/`. OK to proceed?"
2. On approval, run:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/JordanCoin/openfoia/main/install.sh | bash
   ```
3. Confirm with `openfoia --version`.

## After any branch succeeds

Tell the user what they can do next — succinctly:

- `/foia-search <topic>` — search MuckRock for existing FOIAs
- `/foia-investigate <topic>` — full investigation loop
- `openfoia guide` — interactive quickstart

If the install failed, surface the exact error and suggest either:
- `openfoia install-extras ocr` (if a later step needs OCR)
- Checking the openfoia repo README at https://github.com/JordanCoin/openfoia

Keep the response short. The user mostly cares whether it worked, not the step-by-step.

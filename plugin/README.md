# OpenFOIA Claude Code plugin

Ships a skill and four slash commands that make Claude a competent copilot for FOIA investigations using the [openfoia CLI](https://github.com/JordanCoin/openfoia).

## What's in the box

- **Skill** — `openfoia`: teaches Claude the full investigation loop (records search → download → OCR → extract → crossref → graph), when to use which extraction tier, privacy warnings before network calls, and how to read FOIA responses critically.
- **`/foia-install`** — install and initialize the `openfoia` CLI, with a warning before each step that touches the machine.
- **`/foia-search <topic>`** — multi-source search across MuckRock, OpenCorporates, SEC.
- **`/foia-investigate <topic or muckrock-id>`** — end-to-end investigation on one request.
- **`/foia-graph <name or request-id>`** — build or open a saved relationship graph.

## Requires

- `openfoia` CLI installed (`pip install -e ".[dev]"` from the openfoia repo, or `pip install openfoia`)
- For OCR: `openfoia install-extras ocr` + system `tesseract` and `poppler`
- For crossref's cloud sources: no API keys needed for public data

## Privacy posture

The skill teaches Claude to warn before any network call. All local operations (ingest, extract without cloud LLM, graph, purge) run entirely on-device. See `docs/THREAT_MODEL.md` in the openfoia repo for what is and isn't protected.

## Install (local dev)

From a clone of this repo:

```bash
mkdir -p ~/.claude/plugins
ln -s "$(pwd)/plugin" ~/.claude/plugins/openfoia
```

Restart Claude Code. Check it loaded with `/help` — you should see the four `/foia-*` commands.

## Install (marketplace)

```
/plugin marketplace add JordanCoin/openfoia
/plugin install openfoia@openfoia
```

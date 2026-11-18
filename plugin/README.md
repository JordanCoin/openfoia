# OpenFOIA Claude Code plugin

Ships a skill and four slash commands that make Claude a competent copilot for FOIA investigations using the [openfoia CLI](https://github.com/JordanCoin/openfoia).

## What's in the box

- **Skill** — `openfoia`: teaches Claude the case loop (case → search → fetch → docs grep → cited notes → extract and graph → export → FOIA draft), the three evidence tiers, when to use which extraction tier, privacy warnings before network calls, how to read FOIA responses critically, and the rules for OpenFOIA's MCP tools when they are connected.
- **`/foia-install`** — install and initialize the `openfoia` CLI, with a warning before each step that touches the machine.
- **`/foia-search <topic>`** — multi-source search: `openfoia search` across every records source, then per-source follow-ups.
- **`/foia-investigate <claim or topic>`** — end-to-end investigation inside one case, ending in a cited export.
- **`/foia-graph <name or request-id>`** — build or open a saved relationship graph.

## Requires

- `openfoia` CLI installed (`pip install -e ".[dev]"` from the openfoia repo, or `pip install openfoia`)
- For OCR: `openfoia install-extras ocr` + system `tesseract` and `poppler`
- For crossref's cloud sources: no API keys needed for public data

## Privacy posture

The skill teaches Claude to warn before any network call. All local operations (ingest, extract without cloud LLM, graph, purge) run entirely on-device. See `docs/THREAT_MODEL.md` in the openfoia repo for what is and isn't protected.

## The MCP route (any MCP client)

The plugin drives the CLI through the shell. Clients without a shell (Claude Desktop) or that prefer tool calls can use OpenFOIA's MCP server instead, which runs the same code:

```bash
openfoia install-extras mcp
claude mcp add openfoia -- openfoia mcp serve       # Claude Code
```

For Claude Desktop and the full list of tools, see "AI agents over MCP" in `docs/GUIDE.md`. The server speaks over stdin/stdout only and never opens a network port. Its one network tool, `openfoia_search`, is off unless you start the server with `--allow-network`, and even then the first call only returns the warning; the query is sent after you agree. The other tools read your local database and never touch the network. There are no MCP tools that delete, purge, send FOIA requests, email, fax or mail, or change config.

What a tool returns (passages, page text, case notes, your logged queries) goes to the client and from there to its model provider. The skill tells Claude the same.

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

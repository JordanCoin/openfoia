---
allowed-tools: Bash(openfoia analyze graph:*), Bash(openfoia analyze graphs:*), Bash(openfoia request list:*), Bash(open:*)
description: Build or open an investigation graph from extracted entities
---

Build or open a relationship graph: $ARGUMENTS

The argument can be:
- **A name of an existing saved graph** (e.g., `clearview-ai`) → open it
- **A request ID** (e.g., `INGEST-4F14FE` or `DC-25981836`) → build a new graph from that request
- **Empty or `list`** → list all saved graphs

## Step-by-step

1. **If the argument is empty or "list"**, show all saved graphs:
   ```bash
   openfoia analyze graphs
   ```
   Then ask the user which one to open, or whether they want to build a new one.

2. **If the argument looks like a saved-graph name** (matches one shown by `openfoia analyze graphs`), open it directly:
   ```bash
   open ~/.openfoia/graphs/<name>.html
   ```

3. **If the argument looks like a request ID** (starts with `DC-`, `REQ-`, `INGEST-`, etc.), build a new graph. Ask the user for a short descriptive name first — graphs without names aren't persisted interactively:
   ```bash
   openfoia analyze graph --request <id> --name <slug> --view
   ```

4. **If ambiguous**, run `openfoia analyze graphs` and `openfoia request list` in parallel to show both, then ask the user to clarify.

## Output to surface

After building:
- Entity count and relationship count (from the CLI output)
- File path to the saved HTML
- Confirm the browser opened

After opening an existing graph, just confirm it's open — don't re-explain what pyvis is.

Keep responses short. The graph is the output; your job is to get the user there.

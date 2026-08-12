---
allowed-tools: Bash(openfoia:*)
description: Run the full FOIA investigation loop on a topic or MuckRock ID — search, download, OCR, extract, crossref, and graph
---

Run an end-to-end FOIA investigation on: $ARGUMENTS

The argument can be either a topic (like `Palantir contracts`) or a MuckRock request ID (like `195614`). If it's numeric, treat it as a MuckRock ID and skip the search step.

**Before running:** tell the user in plain language what network calls this will make — MuckRock for search/download, plus 10+ sources for crossref. Get explicit go-ahead.

## Step-by-step

1. **Search (if the argument is a topic, not an ID)**
   ```bash
   openfoia records search "$ARGUMENTS" --source muckrock --limit 10
   ```
   Show results, then ask the user which ID to investigate. Stop here until they pick one.

2. **Download the PDFs**
   ```bash
   openfoia records download <id> --source muckrock
   ```
   List what came down. Flag any obvious boilerplate ("Responsive Records Attached", "Fee Waiver", etc.) vs. the likely substantive records.

3. **Ingest** — ask the user which file to focus on, or offer to ingest all with `--recursive`:
   ```bash
   openfoia docs ingest downloads/<file>.pdf
   ```
   Note the document ID returned.

4. **Try extract first**
   ```bash
   openfoia analyze extract <doc-id>
   ```
   If entity count is very low (< 3 non-date entities) and the file is likely scanned, proceed to step 5. Otherwise skip to step 6.

5. **OCR if needed**
   ```bash
   openfoia docs ocr downloads/<file>.pdf -o /tmp/<file>.txt
   openfoia analyze extract <doc-id> --force
   ```

6. **Read the extraction output critically.** Name any entity that looks like a false positive (keyword collision, OCR artifact). Explain what the document actually says vs. what the request asked for — the gap itself is often the story.

7. **Crossref, scoped to this request**
   Find the request ID with `openfoia request list`, then:
   ```bash
   openfoia crossref -r <request-id>
   ```
   Read the hits. Call out any flagged entities that appear in multiple sources — those are the investigative leads.

8. **Build the graph**
   ```bash
   openfoia analyze graph --request <request-id> --name <slug> --view
   ```
   Pick a short, descriptive slug (e.g., `palantir-el-cajon`, `clearview-chicago`). `--view` opens the interactive HTML in the browser.

9. **Summarize the finding.** Three things:
   - What the user asked the agency for
   - What the agency actually disclosed
   - Whether the response was substantively responsive or a null-response-as-compliance pattern (agencies often search their vendor DB on a keyword and ship whatever matches, regardless of whether it relates)

Keep the narrative tight and honest. If the investigation hit a dead end (wrong entity, no matches, scanned doc that OCR couldn't read), say so and suggest the next thread to pull.

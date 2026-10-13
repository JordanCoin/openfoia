---
allowed-tools: Bash(openfoia:*)
description: Investigate a claim or topic end to end — open a case, search, fetch, read every passage, record cited findings, graph, export
---

Investigate: $ARGUMENTS

The argument is a claim ("the PAS was given to schoolchildren") or a topic ("Palantir contracts, El Cajon"). Turn it into one question and keep every step inside one case.

**Before any network call**, say in one sentence which source will see the query from the user's real IP, and get a go-ahead once per source.

## Steps

1. **Open the case**
   ```bash
   openfoia case new "<short question>" -d "<the claim as stated>"
   ```

2. **Search, then triage.** Start with DocumentCloud (it mirrors the CIA CREST archive); add other sources when the topic calls for them.
   ```bash
   openfoia records search "<phrase>" -s documentcloud -n 100 --json --rank <keyword,keyword>
   ```
   Save the output to `hits.json`. Run two or three phrasings and the names that appear in the first hits. Show the user the top ranked hits and ask which to fetch; if they say "all", fetch all.

3. **Fetch into the case**
   ```bash
   openfoia records fetch --from hits.json
   ```
   Report `fetched / skipped / failed` and name any failures.

4. **Read every passage for the key terms.** This is the investigation.
   ```bash
   openfoia docs grep '"<key phrase>"' -C 300
   openfoia docs grep '<term> OR <term> OR <term>' -n 200
   ```
   Open full pages with `openfoia docs show <doc-id> --page N` when a passage needs context. Do not conclude until you have read the passages for every key term.

5. **Record findings as you go**, with the tier kept honest.
   ```bash
   openfoia docs grep '"<phrase>"' --save --note "<what this shows>"
   openfoia case note "<finding>" --doc <id> --page N --quote "<verbatim>" --tier says
   openfoia case note "<inference>" --tier infer
   ```

6. **Link entities** (offline tiers unless the user wants the LLM)
   ```bash
   openfoia analyze extract <doc-id> --tier gliner
   openfoia analyze graph --name <slug> --no-text --view
   openfoia analyze graph --mentions "<A>" "<B>"
   ```

7. **Export and write up**
   ```bash
   openfoia case export -f md -o report.md
   ```
   Lead with the verdict (found, not found, partly found), then what was searched (sources, phrasings, document count), the hits with document id and page, the dead ends, and what a FOIA request could close. Keep "the document says", "reasonable inference" and "speculation" in separate paragraphs. If a source failed, say so; a dead source is not a null result.

8. **Offer the FOIA draft** when a named gap exists:
   ```bash
   openfoia template generate standard -a <agency> -s "<records sought>" -n "<name>" -e <email> -o draft.txt
   ```

---
allowed-tools: Bash(openfoia records search:*), Bash(openfoia agency search:*)
description: Search DocumentCloud, MuckRock, OpenCorporates, and SEC for existing records on a topic before filing a new FOIA
---

Search public records for the user's topic: $ARGUMENTS

**Before running:** confirm with the user in one sentence that this hits external APIs (DocumentCloud, MuckRock, OpenCorporates, SEC). Only skip the warning if the user has already approved network use in this session.

Follow these steps:

1. Run DocumentCloud first (it mirrors the CIA CREST archive and needs no token), ranked by the user's keywords; MuckRock holds completed FOIA responses but needs `OPENFOIA_MUCKROCK_TOKEN`:
   ```bash
   openfoia records search "$ARGUMENTS" --source documentcloud --limit 50 --json --rank <keywords>
   openfoia records search "$ARGUMENTS" --source muckrock --limit 15
   ```
   If a case is active the queries are logged to it. Save `--json` output to a file so `/foia-investigate` can fetch from it.

2. If the topic is a company/organization, also check OpenCorporates and SEC in parallel:
   ```bash
   openfoia records search "$ARGUMENTS" --source opencorporates --limit 10
   openfoia records search "$ARGUMENTS" --source sec --limit 10
   ```

3. Read the MuckRock results and group by angle (e.g., "police departments", "public health", "universities") so the user can pick a thread.

4. If MuckRock returns 0 results, surface that clearly and suggest:
   - Trying a broader or differently-phrased query
   - Filing a new request via `/foia-investigate` or `openfoia request new`

5. End by suggesting the next step: `/foia-investigate <question>` to run the full loop inside a case, `openfoia records fetch --from hits.json` to store the DocumentCloud hits, or `openfoia records download <id> --source muckrock` to pull MuckRock PDFs.

Keep your output tight. Raw CLI tables are fine; don't re-format them into prose.

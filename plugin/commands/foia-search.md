---
allowed-tools: Bash(openfoia records search:*), Bash(openfoia agency search:*)
description: Search MuckRock, OpenCorporates, and SEC for existing records on a topic before filing a new FOIA
---

Search public records for the user's topic: $ARGUMENTS

**Before running:** confirm with the user in one sentence that this hits external APIs (MuckRock, OpenCorporates, SEC). Only skip the warning if the user has already approved network use in this session.

Follow these steps:

1. Run MuckRock first — that's where completed FOIA responses live:
   ```bash
   openfoia records search "$ARGUMENTS" --source muckrock --limit 15
   ```

2. If the topic is a company/organization, also check OpenCorporates and SEC in parallel:
   ```bash
   openfoia records search "$ARGUMENTS" --source opencorporates --limit 10
   openfoia records search "$ARGUMENTS" --source sec --limit 10
   ```

3. Read the MuckRock results and group by angle (e.g., "police departments", "public health", "universities") so the user can pick a thread.

4. If MuckRock returns 0 results, surface that clearly and suggest:
   - Trying a broader or differently-phrased query
   - Filing a new request via `/foia-investigate` or `openfoia request new`

5. End by suggesting the next step — either `/foia-investigate <muckrock-id>` to run the full loop on a specific result, or `openfoia records download <id>` to just pull the PDFs.

Keep your output tight. Raw CLI tables are fine; don't re-format them into prose.

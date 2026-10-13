# Jurisdiction files

One JSON file per public-records law, named by id: `us.json` (federal FOIA),
`us-mi.json` (Michigan), `us-dc.json`. A field set to `null` means the value
is not known. It never means "none" or "no rule". `verified` is true when the
statute citation, response rule and appeal path were read from a fetched
source on `verified_at`; other fields may carry caveats in `notes`. A file with `verified: false` may still carry values, but
only with the page they came from in `sources` and a `notes` line saying what
is missing (Tennessee: the response clock). The loader accepts such files,
the CLI warns on them. The state files were read from the Reporters Committee
for Freedom of the Press Open Government Guide
(https://www.rcfp.org/open-government-guide/) on 2026-10-14;
`scripts/rcfp_jurisdictions.py` drafts a file from a saved page so the pass
can be repeated when a chapter is updated.

| Field | Type | Meaning |
|---|---|---|
| `id` | `us` or `us-<two letters>` | Matches the filename. |
| `name` | string | Jurisdiction name. |
| `level` | `federal` or `state` | Which law applies. Counties, cities and districts use their state's law. |
| `statute.name`, `statute.citation`, `statute.url` | string or null | The act and where it is published. |
| `response.days`, `response.unit` | int or null; `business`, `calendar`, `none` | Time to respond. `none` means the statute sets no fixed clock. |
| `response.rule_text` | string or null | The rule in words, when days alone mislead. |
| `response.extension_days`, `response.extension_unit` | int or null; unit | A one-time statutory extension. `extension_days` is additional to `days`; a statute that states a total cap is encoded as the difference, and a cap that is not additive goes in `rule_text` with `days` null. |
| `response.silence_is_denial` | bool or null | Whether a missed deadline counts as a denial that can be appealed. |
| `fees.search_allowed` | bool or null | Whether search or labor time may be billed. |
| `fees.labor_rule`, `fees.copy_rule`, `fees.waiver_rule`, `fees.late_penalty` | string or null | Fee rules in words. |
| `fees.estimate_required` | bool or null | Whether the body must give an estimate before charging. |
| `residency.restricted`, `residency.rule_text` | bool or null; string or null | Whether non-residents can be refused. |
| `appeal.body`, `appeal.type` | string or null; `court`, `commission`, `attorney_general`, `agency_head`, `ombudsman_advisory` | Who hears a denial first. |
| `appeal.deadline_days`, `appeal.deadline_unit`, `appeal.rule_text` | int or null; unit; string or null | How long a requester has. |
| `exemptions_citation` | string or null | Where the exemptions live. |
| `format_rule` | string or null | Electronic-format rights. |
| `covers.governor`, `covers.legislature`, `covers.universities_public`, `covers.school_districts` | bool or null | Whether the law reaches that body. |
| `sources` | list of URLs | Where the values came from. Verified entries have at least one. |
| `verified`, `verified_at` | bool; ISO date or null | See above. |
| `notes` | string or null | Anything a requester should know that the fields cannot carry. |

Add a jurisdiction by adding a file; the loader picks it up. Never guess a
value to fill a null. Cite the page you read.

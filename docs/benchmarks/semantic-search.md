# Semantic search spike (#118): measurements and recommendation

**Recommendation: go, as an opt-in experimental extra with `potion-base-8M` as
the default. No-go for anything more:** not default-on, not core, not the
multilingual model as default, and no wording that suggests it finds reworded
passages reliably. It costs little and runs fully offline, but on the test set
below it surfaced a reworded passage in its top 10 for only 3 of 10 queries.
That beats keyword search (0 of 10 as typed), but a journalist cannot use it to
clear a document.

Everything below was measured on 2026-11-18 in the project sandbox: 4 vCPU
x86-64 Linux, CPython 3.13.18, SQLite 3.53.1, model2vec 0.10.0, sqlite-vec 0.1.9,
sqlcipher3-binary 0.6.0 (SQLCipher 4.12.0 community). That is faster than a
$300 laptop; treat times as relative. Reproduce with:

```bash
openfoia docs embed-model                      # and the other models, if wanted
python scripts/semantic_search_eval.py --models-from ~/.openfoia --json out.json
```

## Test corpus

Nothing suitable was in `tests/fixtures`, so the script generates one: 1,000
pages, 84 documents, 282 words a page on average, built from templates for
council minutes, contracts, internal emails, audit reports and police logs
(names, amounts, dates and streets drawn at random, seed 118). Into it are
planted:

- 10 **reworded needles**: one sentence each that says what a query means with
  as few of its words as possible ("payments to the mayor's brother" →
  "Funds were transferred to a sibling of the city executive through a
  consulting firm…").
- 3 **lexical distractors**: sentences that share a query's words but not its
  meaning ("The mayor's brother gave a short speech at the parade; payments for
  the floats…").
- 2 **exact-string needles** (an invoice number, a college name), where keyword
  search should win.
- 4 **non-English needles** (Portuguese, Turkish, Spanish, Swahili), queried in
  their own language and in English.

This is a stress test of one hard case, not a sample of real FOIA releases.
The boilerplate is repetitive and on-topic (mayors, contracts, police, audits
are everywhere), which makes needles harder to find than in many real
collections. Results on real documents may be better or worse; they need their
own evaluation before this feature is promoted.

## Cost

| | potion-base-8M (default) | potion-retrieval-32M | potion-multilingual-128M |
|---|---|---|---|
| Languages | English | English | 101 |
| Model download | 30.9 MB | 130.7 MB | 531.0 MB |
| Download time here | 3 s | 6 s | 30 s |
| Load time | 0.33 s | 0.55 s | 5.6 s |
| Peak RAM, whole process (base 51 MB) | 150 MB | 349 MB | 1,894 MB |
| Embedding, per page | 1.3 ms | 1.7 ms | 2.1 ms |
| Vector dimensions | 256 | 512 | 256 |
| Index per 1,000 pages (database growth) | 13.9 MB | 40.0 MB | 14.0 MB |
| Query, in process, sqlite-vec / numpy (median) | 10 / 30 ms | 22 / 56 ms | 10 / 29 ms |
| Whole `docs grep --semantic` command | 1.0 s | 1.4 s | 7.1 s |

- **Install:** `pip install model2vec sqlite-vec` into an empty venv adds 92 MB
  (numpy 53 MB with its libraries, tokenizers 12 MB, hf-xet 11 MB,
  huggingface_hub 4 MB, the rest small). The core install is unchanged; nothing
  in the core imports any of it (tested).
- **Index:** 1,000 pages became 9,541 chunks (40-word windows, 10 words of
  overlap), 9.8 MB of raw float32 vectors. For comparison, the whole database
  with page text and the FTS5 index was 5.0 MB before embedding, so the index
  roughly triples the database. 512-dimension vectors overflow SQLite's 4 KB
  pages, which is why `potion-retrieval-32M` grows the file by 40 MB.
- **Whole command** includes Python start-up, imports and model load. Plain
  keyword `docs grep` takes 0.65 s on the same database.
- **Multilingual RAM:** 1.9 GB peak, mostly while loading; the tokenizer alone
  peaks at ~580 MB. Quantizing the embedding table at load (float16, int8)
  raised the peak instead (2.5–2.6 GB), and an int8 copy saved to disk still
  peaked at 1.5 GB. Too much for many cheap laptops, so it is not the default.
- **Under SQLCipher** (base model, same corpus): same ranks, sqlite-vec path,
  embedding 1.6 ms a page, query 113 ms median vs 10 ms unencrypted, because
  each query reads and decrypts the whole vector table. Still interactive.

### How brute force scales

There is no approximate index: every query compares against every chunk. In
milliseconds, ranking only (no model, random vectors). At ~9.5 chunks a page,
100,000 vectors is about 10,500 pages:

| Vectors | 256-d sqlite-vec | 256-d numpy | 512-d sqlite-vec | 512-d numpy |
|---|---|---|---|---|
| 1,000 | 1.0 | 2.0 | 2.0 | 6.3 |
| 10,000 | 9.1 | 23 | 22 | 51 |
| 50,000 | 47 | 200 | 107 | 292 |
| 100,000 | 84 | 335 | 194 | 605 |

sqlite-vec is 2–4× faster than the numpy fallback, and the numpy path holds
every vector in memory (100 MB per 100,000 256-d vectors). Both are fine up to
roughly 10,000 pages. Past ~50,000 pages a real ANN index or int8 vectors
would be needed.

## Quality

Rank of the needle's page among 1,000 pages (1 = first result). FTS5 "as
typed" is what `docs grep` does with the query; "OR" joins every word of the
query with OR, the most generous keyword search; "–" means not returned at all.

| Query (needle wording differs) | FTS5 as typed | FTS5 OR | base-8M | retrieval-32M | multilingual-128M |
|---|---|---|---|---|---|
| payments to the mayor's brother | – | 278 of 671 | 495 | 291 | 788 |
| police officer fired for misconduct | – | 639 of 967 | 252 | 261 | 247 |
| contract awarded without competitive bidding | – | – | 216 | 216 | 261 |
| school lunch program budget cut | – | – | **1** | **1** | 51 |
| lead contamination in drinking water | – | 478 of 778 | **1** | 15 | **1** |
| bribe paid to the building inspector | – | 228 of 672 | 702 | 374 | 717 |
| firefighters claiming overtime they did not work | – | **1** of 705 | **3** | **5** | 31 |
| emails deleted before the audit | – | 351 of 687 | 139 | 154 | 95 |
| patients died because the hospital was understaffed | – | 87 of 673 | 150 | 147 | 51 |
| developer donated to the councilwoman's campaign | – | – | >500 | 677 | 403 |
| **In top 10** | **0 / 10** | **1 / 10** | **3 / 10** | **2 / 10** | **1 / 10** |

- **Lexical distractors win.** For the three queries with a distractor, the
  default model ranked the distractor 25th, 63rd and 25th; it ranked above the
  real needle twice. FTS5 OR ranked every distractor first. Static embeddings
  are close to a weighted average of word vectors, so shared words dominate.
- **Exact strings:** `INV-2024-0917` is rank 1 for FTS5 and 50 / 39 / 88 for
  the three models. `Hocking Technical College` is rank 1 everywhere. Use plain
  `docs grep` for names, numbers and identifiers.
- **Non-English:** a query in the page's own language found the needle at rank
  1 with every model (Spanish: 1 / 7 / 12), but FTS5 OR also found all four at
  rank 1, because those queries share words with the needles. Asked in
  English, only the multilingual model crossed languages, unevenly: Portuguese
  1, Spanish 16, Turkish 289, Swahili 775 (base-8M: 199, 54, 393, 418).
- **sqlite-vec and numpy returned identical rankings** for every query.

### What was tried and not kept

The window size matters more than anything else that was tried (default model,
needle in top 10):

| Window (words, overlap) | 100, 25 | 60, 15 | 40, 10 | 25, 8 | 12, 4 |
|---|---|---|---|---|---|
| Top 10 | 0 / 10 | 2 / 10 | 3 / 10 | 3 / 10 | 2 / 10 |
| Index per 1,000 pages | 5.7 MB | 9.3 MB | 13.9 MB | 24.0 MB | 51.0 MB |

40 words was kept: the same as 25 for the default model, with a 40% smaller
index. 25 helps the multilingual model (4 / 10 instead of 1 / 10). Subtracting
the corpus mean vector before cosine ("centering") gained one query here and
there and lost others, and does not fit the SQL path, so it was dropped.

## Questions from the issue

- **sqlite-vec is pre-1.0.** It is pinned `<0.2`. Nothing in the schema depends
  on it: vectors are stored in an ordinary table (`document_page_vectors`) and
  sqlite-vec only computes distances at query time (`vec_distance_cosine`). If
  it is missing, will not load, or breaks in a release, search falls back to
  numpy with the same results, and page inserts and deletes, `db encrypt` and
  `purge` never touch it. A `vec0` virtual table would have made page deletes
  fail on any machine without the extension (the purge triggers would
  reference a table whose module cannot load).
- **macOS system Python cannot load SQLite extensions.** Detected
  (`enable_load_extension` is missing) and reported:
  `Note: Semantic search: brute-force cosine in numpy (sqlite-vec not used:
  this Python's sqlite3 cannot load extensions (e.g. macOS system Python)).`
  Not run on a real Mac here; the detection is unit-tested with a stand-in
  connection.
- **Does sqlite-vec load under SQLCipher?** Yes. sqlite-vec 0.1.9 loads into a
  sqlcipher3-binary 0.6.0 connection (SQLCipher 4.12.0, SQLite 3.51.1) via
  `load_extension`, `vec_version()` answers, and search through OpenFOIA's own
  encrypted engine uses the sqlite-vec path. The file has no plaintext SQLite
  header and contains neither the page text nor the packed vectors in the
  clear. Covered by `tests/test_semantic_search.py::
  test_sqlite_vec_loads_and_searches_under_sqlcipher` (skips without both
  packages). Extension loading is switched off again straight after.

## Privacy and safety, in short

- One network action, `openfoia docs embed-model`: warns what is sent (your IP
  address, the Tor exit's with `--tor`, and the model's name), asks, fetches
  three files at a pinned revision through the egress choke point, checks
  pinned sizes and SHA-256 hashes, and stores them in `get_data_dir()/models`
  (portable mode respected). model2vec is only ever handed that local folder;
  with a missing folder it would otherwise download from the hub, so the code
  checks first and the hub library is put in offline mode. No `~/.cache` was
  created during these runs.
- Embeddings are purged with their page and document by SQLite triggers
  (which do not depend on `PRAGMA foreign_keys`), with an FK cascade as a
  second line and an orphan sweep if a migration ever drops the triggers.
  Tests cover document delete, page replace and edit, raw deletes with foreign
  keys off, `docs embed --drop`, `purge` and `purge --secure`.
- Embedding inversion and free-page remnants: `docs/THREAT_MODEL.md`.

## If this is pursued

1. Hybrid ranking: merge FTS5 and vector ranks (reciprocal rank fusion), so
   exact names and numbers stay on top and reworded passages can still surface.
2. Evaluate a small contextual model (for example multilingual-e5-small or
   bge-small through ONNX Runtime, ~120 MB plus the runtime) on this same
   script. Static embeddings look like the quality ceiling here.
3. Evaluate on a real FOIA collection (for example a public DocumentCloud
   project) with hand-labelled queries.
4. Store vectors as int8 (sqlite-vec supports it) to cut the index ~4×.
5. Expose it to the agent and the unified `openfoia search` command through the
   same `semantic_search()` function (principle 5).

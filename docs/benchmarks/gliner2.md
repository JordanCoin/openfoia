# GLiNER2 multilingual as the tier-2 model (issue #119)

Measured 2026-11-18. This is a report, not a change: tier 2 still loads the
model it loaded before.

**Recommendation: don't swap yet. More data is needed, and the direction is
promising.** GLiNER2 multi matches the production model on our English page,
and on the multilingual sentences it finds 24 of 27 names where production
finds 14. But it uses about 1 GiB more peak memory (2,974 vs 2,002 MiB). It is
also slower per page: 1.36× production with 2 threads and 1.60× with 4, in
paired runs on a busy shared machine. The corpus is too small to decide a
default on (17 English names, 27 multilingual ones). The reasons are under
[Recommendation](#recommendation).

**Update 2026-11-19: the multilingual model is now a user choice, and the
default is unchanged.** `entities.ner_model` (env `OPENFOIA_NER_MODEL`, flag
`analyze extract --ner-model`) selects `english` (the model above, still the
default) or `multilingual` (`fastino/gliner2-multi-v1`, extra
`ner-multilingual`). A larger news-style set in 13 languages was added and the
two choices were re-run on it: see
[Follow-up: the news set](#follow-up-the-news-set-13-languages-2026-11-19).
The original report below is unchanged.

## Follow-up: the news set, 13 languages (2026-11-19)

### Why `fastino/gliner2-multi-v1` is the multilingual choice

From the measurements in this report:

- It is the most accurate of the four on both corpora: 24/27 multilingual
  names (19/27 exact) and the only clean English spans (strict NER F1 0.94).
  `urchade/gliner_multi-v2.1`, the option that would reuse the `gliner`
  package, finds 20/27, is worse on the English page (strict F1 0.75), and
  downloads more (2.3 GB, because gliner fetches both weight formats).
- Its RAM cost is about the same as the other multilingual models (2,974 MiB
  peak vs 2,827-2,828 MiB): on a $300 laptop all three need about 1 GB more
  than the English model, so RAM does not separate them. That cost is why it
  is opt-in and not the default.
- A separate extra (`ner-multilingual` = `gliner2[local]`) adds 6.4 MB on top
  of the torch stack the `ner` extra already pulls in, and `gliner2` does not
  depend on `gliner`, so a user can install either or both.
- GLiNER2.5 multi is the close alternative (same 24/27, 146 MiB less RAM,
  slightly faster), but its English spans are worse (strict F1 0.71) and it
  misses the street address.

### The sentences are machine-written

**The 26 news sentences were written by an AI model (Claude), not by native
speakers, and no native speaker has reviewed them.** They may contain
grammatical errors, unnatural phrasing or wrong inflections, and the gold
spans reflect one non-native reading. Treat the numbers as a direction, not
as a measured accuracy for any language. Before these numbers decide
anything (for example the default), each language needs review by a native
speaker, ideally with real documents.

What the set is (`NEWS` in `tests/benchmark_extraction.py`): two sentences in
each of Turkish, Swahili, Portuguese, Spanish, Arabic, Persian, Russian,
Ukrainian, Hindi, Bengali, Chinese, Indonesian and French, in the factual
register of an investigative report (audit bodies, ministries, contracts,
tenders). 68 gold entities: people, organizations, locations. People are
invented; institutions and places are real; the events are invented and
neutral. Gold is the surface form in the text, inflection included (Russian
`Газпромбанком`, Ukrainian `Олегу Коваленку`, Bengali `ঢাকায়`), with some
"also acceptable" strings per sentence (e.g. `Kenya` inside
`Benki Kuu ya Kenya`). Each sentence is scored as its own document with the
loose and strict rules described under [Method](#method).

### Setup

The same machine and software as above (4 vCPU, torch 2.15.0+cpu, gliner
0.2.30, gliner2 2.1.0), models from the Hub cache (`HF_HUB_OFFLINE=1`). Both
models ran through the production selection, not the benchmark's model
override: `tests/benchmark_extraction.py --ner-model english|multilingual`
builds `EntityExtractor(ner_model=...)` and loads with `_get_gliner` /
`_get_gliner2`, and each run's metadata recorded the model that ran. One
process per model, `--repeat 11`, at 4 and then 2 torch threads. The
1-minute load average before the runs was 1.7-2.5 (other sessions were
running headless Chrome), lower than for the original report. Accuracy was
identical at 4 and 2 threads for both models.

The original 23-sentence set and the English page reproduce the numbers
above exactly: English 14/27 loose, 9/27 strict; multilingual 24/27 loose,
19/27 strict; English page strict NER F1 0.83 vs 0.94.

### Accuracy, news set (26 sentences, 68 gold entities)

| rule | english `gliner_medium-v2.1` | multilingual `gliner2-multi-v1` |
|---|---|---|
| loose | P 0.86 / R 0.60 / F1 0.71 (found 41/68, fp 7) | P 1.00 / R 0.87 / F1 0.93 (found 59/68, fp 0) |
| strict | P 0.71 / R 0.51 / F1 0.60 (found 35/68, fp 15) | P 0.96 / R 0.79 / F1 0.87 (found 54/68, fp 3) |

The loose rule credits a whole unsegmented Chinese sentence for the names in
it (2 of the English model's and 3 of the multilingual model's loose finds).
Without Chinese, the strict counts are 35/62 and 54/62.

Per language, found loose / strict (false positives loose/strict):

| lang | english | multilingual | gold |
|---|---|---|---|
| tr | 3 / 3 (0/0) | 4 / 4 (0/0) | 5 |
| sw | 3 / 3 (3/4) | 5 / 5 (0/0) | 5 |
| pt | 5 / 5 (0/0) | 5 / 4 (0/1) | 5 |
| es | 5 / 3 (0/2) | 5 / 5 (0/0) | 5 |
| ar | 3 / 2 (1/3) | 6 / 6 (0/0) | 6 |
| fa | 1 / 1 (1/2) | 3 / 2 (0/0) | 5 |
| ru | 4 / 4 (1/1) | 4 / 4 (0/0) | 5 |
| uk | 5 / 5 (0/0) | 5 / 5 (0/0) | 5 |
| hi | 0 / 0 (0/0) | 5 / 5 (0/0) | 6 |
| bn | 0 / 0 (0/0) | 4 / 4 (0/0) | 5 |
| zh | 2 / 0 (0/1) | 3 / 0 (0/2) | 6 |
| id | 5 / 5 (1/1) | 5 / 5 (0/0) | 5 |
| fr | 5 / 4 (0/1) | 5 / 5 (0/0) | 5 |

Per sentence (`found/gold` loose; `S` = strict found; what was tagged):

| lang | english | multilingual |
|---|---|---|
| tr | 1/2 S1: `person:Mehmet Demir` | 2/2 S2: `location:Ankara`; `organization:Ankara Büyükşehir Belediyesi`; `person:Mehmet Demir` |
| tr | 2/3 S2: `location:İzmir`; `person:Ayşe Kaya` | 2/3 S2: `location:İzmir`; `person:Ayşe Kaya` |
| sw | 1/2 S1: `location:Mombasa`; `person:Mkaguzi`; `person:Serikali`; `person:Wizara ya Afya` | 2/2 S2: `location:Mombasa`; `organization:Wizara ya Afya` |
| sw | 2/3 S2: `location:Nairobi`; `location:msemaji wa Benki Kuu ya Kenya`; `person:Grace Wanjiku` | 3/3 S3: `location:Nairobi`; `organization:Benki Kuu ya Kenya`; `person:Grace Wanjiku` |
| pt | 3/3 S3: `location:Brasília`; `organization:Tribunal de Contas da União`; `person:Carlos Almeida` | 3/3 S2: `location:Brasília`; `organization:União`; `person:Carlos Almeida` |
| pt | 2/2 S2: `organization:Alfa Engenharia S.A.`; `organization:Petrobras` | 2/2 S2: `organization:Alfa Engenharia S.A.`; `organization:Petrobras` |
| es | 3/3 S1: `location:Bogotá`; `organization:La Contraloría General de la República`; `person:alcalde Jorge Ramírez` | 3/3 S3: `location:Bogotá`; `organization:Contraloría General de la República`; `person:Jorge Ramírez` |
| es | 2/2 S2: `organization:Construcciones del Norte S.A.`; `organization:Ministerio de Obras Públicas` | 2/2 S2: same |
| ar | 1/3 S1: `organization:شركة النور للمقاولات`; `person:دينار` | 3/3 S3: `location:عمّان`; `organization:ديوان المحاسبة`; `organization:شركة النور للمقاولات` |
| ar | 2/3 S1: `location:القاهرة`; `organization:ع وزير المالية`; `person:أحمد الخطيب الاتفاقية` | 3/3 S3: `location:القاهرة`; `organization:البنك الدولي`; `person:أحمد الخطيب` |
| fa | 0/3 S0: `organization:شرکت پارس` | 1/3 S0: `location:تهران`; `organization:شرکت پارس‌سازه` |
| fa | 1/2 S1: `person:علی رضایی`; `person:معاون وزیر نفت` | 2/2 S2: `location:اصفهان`; `person:علی رضایی` |
| ru | 1/2 S1: `location:Счётная палата`; `organization:СтройИнвест`; `organization:мэрией Новосибирска` | 1/2 S1: `location:Новосибирска`; `organization:СтройИнвест`; `organization:мэрией Новосибирска` |
| ru | 3/3 S3: `location:Москве`; `organization:Газпромбанком`; `person:Ирина Смирнова` | 3/3 S3: same |
| uk | 3/3 S3: `location:Харкові`; `organization:Національне антикорупційне бюро України`; `person:Олегу Коваленку` | 3/3 S3: same |
| uk | 2/2 S2: `organization:Міністерство оборони`; `organization:Техноресурс` | 2/2 S2: same |
| hi | 0/3 S0: nothing | 2/3 S2: `location:दिल्ली`; `organization:दिल्ली नगर निगम`; `person:राजेश कुमार` |
| hi | 0/3 S0: nothing | 3/3 S3: `location:मुंबई`; `organization:भारतीय रिज़र्व बैंक`; `person:सुनीता शर्मा` |
| bn | 0/3 S0: nothing | 2/3 S2: `organization:চট্টগ্রাম বন্দরের`; `organization:দুর্নীতি দমন কমিশন`; `person:রহিম উদ্দিন` |
| bn | 0/2 S0: nothing | 2/2 S2: `location:ঢাকায়`; `organization:বাংলাদেশ ব্যাংক` |
| zh | 2/3 S0: `organization:华信建设公司在2023年获得了上海市政府的三个项目` | 3/3 S0: the same run-on span; `organization:审计署发现` |
| zh | 0/3 S0: nothing | 0/3 S0: nothing |
| id | 3/3 S3: `location:Surabaya`; `organization:Komisi Pemberantasan Korupsi`; `person:Budi Santoso` | 3/3 S3: same |
| id | 2/2 S2: `organization:Badan Pemeriksa Keuangan`; `organization:Kementerian Kesehatan`; `person:sebesar` | 2/2 S2: without `sebesar` |
| fr | 2/2 S1: `organization:La Cour des comptes`; `organization:Méditerranée Travaux` | 2/2 S2: adds `location:Marseille`, `organization:mairie de Marseille` |
| fr | 3/3 S3: `location:Dakar`; `organization:Banque mondiale`; `person:Paul Lefèvre` | 3/3 S3: adds `organization:Le Monde` |

What this shows, on this set only:

- The English model finds **nothing** in the four Hindi and Bengali
  sentences (11 names); the multilingual model finds 9.
- On Latin-script and Cyrillic sentences (Turkish, Portuguese, Spanish,
  Indonesian, French, Russian, Ukrainian) the English model already does
  fairly well: 32/35 loose. The gap there is mostly precision and span
  quality (`alcalde Jorge Ramírez`, `person:sebesar`, `person:Serikali`), and
  Swahili, where it tags common words as people.
- Arabic and Persian improve the most after the Indic scripts: 4/11 to 9/11
  loose. Persian is still the weakest non-CJK language for both
  (`پارس‌سازه` inside `شرکت پارس‌سازه`, `شهرداری تهران` missed).
- Neither model segments Chinese, as before: no exact Chinese name in six.
- Both miss `Счётная палата` as an organization; the English model calls it
  a location.

### Speed and RAM (news follow-up)

Paired runs, both models in one process, one page of `DOC` each per round,
first round dropped; ratio = median over rounds of multilingual / English
time in the same round.

| threads | rounds | English median s/page (min-max) | multilingual median s/page (min-max) | median ratio (min-max) | 1-min load after |
|---|---|---|---|---|---|
| 2 | 10 | 0.89 (0.71-0.98) | 1.11 (1.00-1.33) | 1.28 (1.02-1.59) | 2.69 |
| 2 | 20 | 0.83 (0.69-1.05) | 1.09 (0.90-1.33) | 1.30 (1.06-1.61) | 2.46 |
| 4 | 20 | 1.02 (0.59-2.08) | 1.57 (0.70-4.86) | 1.36 (1.07-3.11) | 3.97 |

Separate processes (11 pages, first dropped): English 0.63 s/page at 4
threads and 0.80 at 2; multilingual 0.95 and 1.00. Peak RSS of the whole
benchmark process: **2,007 MiB English, 2,977 MiB multilingual**, the same as
in the original report. Model load from a warm cache: 4.7-5.1 s English,
6.1-6.7 s multilingual. These are one busy 4-vCPU VM; a real 2-core laptop
has still not been measured.

### Still open

- Native-speaker review of every sentence, then real documents per language.
- A quiet 2-core machine, and an fp16 or quantised load to bring the
  multilingual model's RAM down; until then it stays opt-in.
- CJK word splitting (see the `--word-splitter char` section): a model swap
  does not fix it.

## What tier 2 runs today

The issue says tier 2 uses `urchade/gliner_multi-v2.1`. It does not.
`openfoia/pipeline/extract.py` (`_get_gliner`) loads
**`urchade/gliner_medium-v2.1`**. Its model card gives `language: en`, and it
is built on `microsoft/deberta-v3-base`, an English model. So this report
compares four models:

| key | model | library | params (safetensors metadata) | note |
|---|---|---|---|---|
| production | `urchade/gliner_medium-v2.1` | gliner 0.2.30 | 195.2M F32 | what tier 2 loads now |
| gliner multi | `urchade/gliner_multi-v2.1` | gliner 0.2.30 | 288.9M F32 | the model the issue names; the same library, so no new dependency |
| **GLiNER2 multi** | `fastino/gliner2-multi-v1` | gliner2 2.1.0 | 307.1M F32 | the card says 205M (see below) |
| GLiNER2.5 multi | `fastino/gliner2.5-multi-v1` | gliner2 2.1.0 | 287.4M F32 | the card says 287M |

The issue's "~205M / ~287M" are the card figures for `gliner2-multi-v1` and
`gliner2.5-multi-v1`. The weights file of `gliner2-multi-v1` holds 307,098,645
F32 parameters (from the Hub API's `safetensors.parameters`), not 205M.
The `gliner2.5-multi-v1` card says "Weights: ~594 MB (mostly FP16)", but the
published `model.safetensors` is 1,149,461,028 bytes of F32.

## Method

- **Same code path.** `tests/benchmark_extraction.py --model <id>` puts the
  model behind the production tier-2 code. That means the same windows
  (`_gliner_windows`, 300 words), the same 13 labels, threshold 0.4, label
  mapping, scoring and merge (`EntityExtractor(tier="gliner")`). The gliner2
  models are wrapped in `Gliner2Adapter`, which turns
  `extract_entities(..., include_spans=True)` into GLiNER's
  `predict_entities` output. No window raised an error in any run (the
  adapter counts errors, which production would otherwise swallow).
  One limitation: threshold 0.4 is tuned for GLiNER. GLiNER2's confidence
  scale may differ, and I did not sweep the threshold.
- **English corpus:** the benchmark's existing `DOC`, a one-page FOIA
  response letter (1,780 chars, 266 words). It has gold for every entity in
  it: 7 persons, 8 orgs, 2 locations (person + organization + location = 17
  NER gold), plus money, dates, email, phone, document id and address.
  Precision counts every prediction of a type. A prediction is correct if it
  matches a required or an "also acceptable" gold string (e.g. `Pentagon` as
  an org, `2018` as a date).
- **Multilingual corpus:** 23 short sentences. Twenty carry the 20 non-Latin
  names of `tests/test_extraction_scripts.py` (17 scripts: Cyrillic ×2
  languages, Greek, Arabic, Persian, Hebrew, Devanagari, Bengali, Thai, Han
  (zh), Han/Kana (ja), Hangul, Georgian, Armenian, Ethiopic). Each name sits
  in one sentence in its own language, as the only named entity. Three
  Latin-script sentences in Turkish, Swahili and Portuguese follow the
  mission's Istanbul, Nairobi and São Paulo. In total there are 27 gold
  entities, and each sentence is scored as its own document. I wrote the
  sentences; a native speaker has not checked them.
- **Two match rules.** *Loose* is the benchmark's existing substring-either-way
  rule. It credits `Ms. Sarah Chen` for `Sarah Chen`, and also a whole
  unsegmented CJK sentence for the name inside it. *Strict* wants the
  normalized text exactly (case and whitespace folded, edge punctuation
  stripped). Both are reported because they tell different stories.
- **Timing.** Each process loads one model offline (`HF_HUB_OFFLINE=1`), then
  times 11 extractions of `DOC`. The first is reported separately; the median
  of the other 10 is the "warm s/page". Every page is one call to
  `EntityExtractor.extract`, which also runs regex and relationship
  extraction (regex alone takes 0.9-1.9 ms per page). I ran 3 processes per
  model at 4 torch threads and 1 per model at 2 threads (a $300 laptop often
  has 2 cores). I had planned 3 at 2 threads, but cut it to 1 so the whole
  benchmark stayed near 40 minutes on the busy machine. An interleaved run loads all four models in one process and
  times them round-robin, one page each per round, so outside load hits all
  four alike. Wall time, CPU time (`time.process_time`) and the 1-minute load
  average are recorded.
- **Peak RSS** is `ru_maxrss` of the whole benchmark process: Python,
  openfoia, torch, the model, one regex pass, the 11 pages and the 23
  sentences. It is the same in every run of a model (within 2 MiB).
- **Accuracy is deterministic.** Every model gave the same entities in every
  run (the aggregation checks this).

### The machine was busy

This is a shared 4-vCPU VM. Other sessions ran test suites and headless
browsers during every run. The 1-minute load average before a run was
**2.7 to 7.6** on 4 cores, and it never fell under 1.5 (the run script waits up
to 15 s for that). Absolute wall times are therefore upper bounds with a wide
spread. The fastest page seen and the interleaved ratios are the fairest
comparison here. Nothing below is extrapolated to other hardware.

## Environment

| | |
|---|---|
| CPU | Intel Xeon Processor @ 2.30GHz (family 6 model 173), 4 vCPU (1 thread per core), KVM guest, AVX-512 / AMX available |
| RAM | 15 GiB, no swap |
| OS | Linux 6.18.44 |
| Python | 3.13.18 (`/usr/local/bin/python3.13`) |
| torch | 2.15.0+cpu (MKL 2024.2, oneDNN 3.13.3) |
| transformers | 5.22.2 |
| gliner | 0.2.30 |
| gliner2 | 2.1.0 |
| tokenizers / huggingface_hub | 0.23.3 / 2.4.0 |
| peft / accelerate | 0.21.3 / 1.16.0 |
| model revisions | medium `40ec4193`, multi `443d26d6`, gliner2-multi `ce747d79`, gliner2.5-multi `7b005274` (the Hub's current `sha` on 2026-11-18) |

## Results

### Accuracy, English page (one page, 17 NER gold entities)

P / R / F1, with (tp, fp, found/gold), under the loose rule.

| type | production medium-v2.1 | gliner multi-v2.1 | **GLiNER2 multi** | GLiNER2.5 multi |
|---|---|---|---|---|
| address | 1.00 / 1.00 / 1.00 (1,0,1/1) | 0.00 / 0.00 / 0.00 (0,0,0/1) | 1.00 / 1.00 / 1.00 (1,0,1/1) | 0.00 / 0.00 / 0.00 (0,0,0/1) |
| date | 1.00 / 1.00 / 1.00 (5,0,5/5) | 1.00 / 1.00 / 1.00 (5,0,5/5) | 1.00 / 1.00 / 1.00 (6,0,5/5) | 1.00 / 1.00 / 1.00 (6,0,5/5) |
| document_id | 1.00 / 1.00 / 1.00 (1,0,1/1) | 1.00 / 1.00 / 1.00 (1,0,1/1) | 1.00 / 1.00 / 1.00 (1,0,1/1) | 1.00 / 1.00 / 1.00 (1,0,1/1) |
| email | 1.00 / 1.00 / 1.00 (1,0,1/1) | 1.00 / 1.00 / 1.00 (1,0,1/1) | 1.00 / 1.00 / 1.00 (1,0,1/1) | 0.50 / 1.00 / 0.67 (1,1,1/1) |
| location | 1.00 / 0.50 / 0.67 (1,0,1/2) | 0.00 / 0.00 / 0.00 (0,0,0/2) | 1.00 / 0.50 / 0.67 (1,0,1/2) | 1.00 / 0.50 / 0.67 (1,0,1/2) |
| money | 1.00 / 1.00 / 1.00 (2,0,2/2) | 1.00 / 1.00 / 1.00 (2,0,2/2) | 1.00 / 1.00 / 1.00 (2,0,2/2) | 1.00 / 1.00 / 1.00 (2,0,2/2) |
| organization | 1.00 / 1.00 / 1.00 (9,0,8/8) | 0.88 / 0.88 / 0.88 (7,1,7/8) | 0.90 / 1.00 / 0.95 (9,1,8/8) | 0.90 / 1.00 / 0.95 (9,1,8/8) |
| person | 0.88 / 1.00 / 0.93 (7,1,7/7) | 0.86 / 0.86 / 0.86 (6,1,6/7) | 1.00 / 1.00 / 1.00 (7,0,7/7) | 1.00 / 1.00 / 1.00 (7,0,7/7) |
| phone | 1.00 / 1.00 / 1.00 (1,0,1/1) | 1.00 / 1.00 / 1.00 (1,0,1/1) | 1.00 / 1.00 / 1.00 (1,0,1/1) | 1.00 / 1.00 / 1.00 (1,0,1/1) |
| **NER micro loose** | 0.94 / 0.94 / 0.94 | 0.87 / 0.76 / 0.81 | 0.94 / 0.94 / 0.94 | 0.94 / 0.94 / 0.94 |
| **NER micro strict** | 0.83 / 0.82 / 0.83 | 0.80 / 0.71 / 0.75 | 0.94 / 0.94 / 0.94 | 0.72 / 0.71 / 0.71 |
| relationships (recall) | 8/11 | 6/11 | 8/11 | 8/11 |

Under the loose rule, production and both GLiNER2 models tie on the NER micro
F1 (0.94). Each misses one of the two locations (`Pentagon`, which they tag as
an org; that is acceptable gold there). gliner multi-v2.1 is worse on this
English page.

Under the strict rule, GLiNER2 multi is the only one that returns clean
spans. Production tags `Ms. Sarah Chen`, `Senator Maria Gonzalez` and
`Director` as persons. GLiNER2.5 multi keeps `Senator` and merges spans
(`Department of Defense for Project ATLAS`,
`Department of Justice\nOffice of Information Policy`). Both GLiNER2 models
tag `FOIA` as an organization.

Money, dates, phone and document id come out the same for all four, because
regex finds them too. The street address comes only from the model:
production and GLiNER2 multi find it, gliner multi-v2.1 and GLiNER2.5 multi
do not. GLiNER2.5 multi also tags the word `email` as an email address.
Relationship recall is the same 8/11 for production and both GLiNER2 models.

### Accuracy, multilingual sentences (23 sentences, 27 gold entities)

| rule | production | gliner multi-v2.1 | **GLiNER2 multi** | GLiNER2.5 multi |
|---|---|---|---|---|
| loose | P 0.83 / R 0.52 / F1 0.64 (found 14/27, fp 3) | P 0.95 / R 0.74 / F1 0.83 (found 20/27, fp 1) | P 0.96 / R 0.89 / F1 0.92 (found 24/27, fp 1) | P 1.00 / R 0.89 / F1 0.94 (found 24/27, fp 0) |
| strict | P 0.50 / R 0.33 / F1 0.40 (found 9/27, fp 9) | P 0.76 / R 0.59 / F1 0.67 (found 16/27, fp 5) | P 0.77 / R 0.70 / F1 0.74 (found 19/27, fp 6) | P 0.76 / R 0.67 / F1 0.71 (found 18/27, fp 6) |

Per sentence (`found/gold` loose; `S` = strict found; what was tagged):

| lang | production | gliner multi-v2.1 | **GLiNER2 multi** | GLiNER2.5 multi |
|---|---|---|---|---|
| ru | 0/1 S0: `organization:министерством`; `person:Газпром` | 1/1 S1: `organization:Газпром` | 1/1 S1: `organization:Газпром` | 1/1 S1: `organization:Газпром` |
| ru | 1/1 S1: `person:Алексей Навальный` | 1/1 S1: `person:Алексей Навальный` | 1/1 S1: `person:Алексей Навальный` | 1/1 S1: `person:Алексей Навальный` |
| uk | 1/1 S1: `location:понеділок`; `person:Володимир Зеленський` | 1/1 S1: `person:Володимир Зеленський` | 1/1 S1: `person:Володимир Зеленський` | 1/1 S1: `person:Володимир Зеленський` |
| el | 1/1 S0: `person:Ο Κυριάκος Μητσοτάκης` | 1/1 S1: `person:Κυριάκος Μητσοτάκης` | 1/1 S1: `person:Κυριάκος Μητσοτάκης` | 1/1 S1: `person:Κυριάκος Μητσοτάκης` |
| ar | 1/1 S1: `organization:وزارة الداخلية` | 1/1 S1: `organization:وزارة الداخلية` | 1/1 S1: `organization:وزارة الداخلية` | 1/1 S1: `organization:وزارة الداخلية` |
| ar | 0/1 S0: nothing | 0/1 S0: nothing | 1/1 S1: `person:مُحَمَّد` | 1/1 S1: `person:مُحَمَّد` |
| fa | 0/1 S0: nothing | 0/1 S0: `person:آقای بنیامین` | 1/1 S0: `person:آقای بنیامین‌پور` | 1/1 S0: `person:آقای بنیامین‌پور` |
| he | 1/1 S1: `person:בנימין נתניהו` | 1/1 S1: `person:בנימין נתניהו` | 1/1 S1: `person:בנימין נתניהו` | 1/1 S1: `person:בנימין נתניהו` |
| fa | 1/1 S1: `location:تهران` | 1/1 S1: `location:تهران` | 1/1 S1: `location:تهران` | 1/1 S1: `location:تهران` |
| hi | 0/1 S0: nothing | 0/1 S0: nothing | 1/1 S1: `person:नरेन्द्र मोदी` | 1/1 S1: `person:नरेन्द्र मोदी` |
| bn | 0/1 S0: nothing | 0/1 S0: nothing | 1/1 S1: `location:ঢাকা` | 1/1 S1: `location:ঢাকা` |
| th | 0/1 S0: nothing | 1/1 S1: `person:ประยุทธ์ จันทร์โอชา` | 1/1 S1: `person:ประยุทธ์ จันทร์โอชา` | 1/1 S1: `person:ประยุทธ์ จันทร์โอชา` |
| zh | 0/1 S0: nothing | 0/1 S0: nothing | 0/1 S0: nothing | 0/1 S0: nothing |
| ja | 0/1 S0: nothing | 0/1 S0: nothing | 0/1 S0: nothing | 0/1 S0: nothing |
| zh | 1/1 S0: `person:王伟昨天签署了合同` | 1/1 S0: `person:王伟昨天签署了合同` | 1/1 S0: `person:王伟昨天签署了合同` | 1/1 S0: `person:王伟昨天签署了合同` |
| ja | 0/1 S0: nothing | 1/1 S0: `organization:トヨタ自動車は昨日契約に署名した` | 1/1 S0: `organization:トヨタ自動車は昨日契約に署名した` | 1/1 S0: `organization:トヨタ自動車は昨日契約に署名した` |
| ko | 1/1 S0: `person:김정은은` | 1/1 S0: `person:김정은은` | 1/1 S0: `person:김정은은` | 1/1 S0: `person:김정은은` |
| ka | 0/1 S0: nothing | 1/1 S1: `location:თბილისი` | 1/1 S1: `location:თბილისი` | 1/1 S1: `location:თბილისი` |
| hy | 0/1 S0: nothing | 0/1 S0: nothing | 0/1 S0: `person:Երևան` | 0/1 S0: nothing |
| am | 0/1 S0: nothing | 1/1 S1: `location:አዲስ አበባ` | 1/1 S1: `location:አዲስ አበባ` | 1/1 S1: `location:አዲስ አበባ` |
| tr | 1/2 S1: `location:İstanbul Büyükşehir Belediyesi`; `person:Ahmet Yılmaz` | 2/2 S2: `organization:İstanbul Büyükşehir Belediyesi`; `person:Ahmet Yılmaz` | 2/2 S2: `organization:İstanbul Büyükşehir Belediyesi`; `person:Ahmet Yılmaz` | 2/2 S2: `organization:İstanbul Büyükşehir Belediyesi`; `person:Ahmet Yılmaz` |
| sw | 3/3 S2: `location:Nairobi`; `organization:Wizara ya Fedha ilimlipa`; `person:John Mwangi` | 3/3 S2: `location:jijini Nairobi`; `organization:Wizara ya Fedha`; `person:John Mwangi` | 3/3 S3: `location:Nairobi`; `organization:Wizara ya Fedha`; `person:John Mwangi` | 3/3 S2: `location:Nairobi`; `organization:Wizara ya Fedha ilimlipa`; `person:John Mwangi` |
| pt | 2/2 S1: `organization:A Prefeitura de São Paulo`; `organization:Construtora Horizonte Ltda` | 2/2 S2: `organization:Construtora Horizonte Ltda`; `organization:Prefeitura de São Paulo` | 2/2 S1: `location:São Paulo`; `organization:Horizonte Ltda`; `organization:Prefeitura de São Paulo` | 2/2 S1: `location:São Paulo`; `organization:Horizonte Ltda`; `organization:Prefeitura de São Paulo` |

What this shows:

- Production (English-only) finds nothing in Arabic `مُحَمَّد`, the Persian
  name, Devanagari, Bengali, Thai, Georgian, Armenian or Amharic. It tags
  `Газпром` as a person and `понеділок` ("Monday") as a location.
- Both GLiNER2 models find the Devanagari, Bengali, Thai, Georgian, Amharic
  and vocalised Arabic names, plus all three Latin-script mission sentences.
  gliner multi-v2.1 gets the Thai, Georgian and Amharic ones but not the
  Indic ones.
- No model finds Armenian `Երևան`; GLiNER2 multi tags it as a person.
- **No model segments Chinese or Japanese.** In `东京` / `東京都` nobody finds
  anything. In `王伟昨天签署了合同` and `トヨタ自動車は昨日契約に署名した`
  every model that answers returns the whole sentence. The loose rule credits
  that; the strict rule does not. Korean `김정은은` keeps the topic particle.
  This comes from the word splitting: the production windows and GLiNER's own
  splitter split on `\w+` and whitespace, and CJK has no spaces. Swapping
  models does not fix it. The `--word-splitter char` runs below test
  GLiNER2's own alternative.
- The Persian name keeps its honorific (`آقای بنیامین‌پور`), and the
  Portuguese company is cut to `Horizonte Ltda` by both GLiNER2 models.

### GLiNER2 with `--word-splitter char`

GLiNER2 can split words by character instead of whitespace. The settings are the same as above: 4 threads, one
process each. The 1-minute load average was 1.6-1.8, the lowest of the session, so these page times
cannot be compared with the tables below.

| model, char splitter | English NER F1 loose / strict | multilingual found/27 loose / strict | warm median s/page |
|---|---|---|---|
| **GLiNER2 multi** | 0.94 / 0.94 | 9 / 9 | 0.67 |
| GLiNER2.5 multi | 0.94 / 0.75 | 11 / 9 | 0.66 |

Character splitting fixes CJK and Korean: `东京`, `東京都`, `王伟` and `김정은` come out exact. But it loses every Cyrillic, Greek, Arabic, Persian, Hebrew, Indic, Thai, Georgian, Armenian and Amharic name that the whitespace splitter found. It also loses the Japanese company. So it is not a global setting, and any CJK fix would have to
choose the splitter by script.

### Speed, CPU

Wall-clock seconds for one call of `EntityExtractor.extract(DOC)`, one page.

#### Paired (interleaved) runs: the fairest comparison here

All four models are loaded in one process. Each round times one page per model, in turn, for 11 rounds; the first round is dropped. The ratio is the median, over rounds, of model time / production time in the same round. The 1-minute load average was 1.64 before the 4-thread run and 4.89 after it (and before the 2-thread run), 3.70 after.

#### Interleaved, 4 threads (peak RSS all four loaded 6436 MiB)

| model | warm median s/page | min | max | median ratio to production (paired by round) |
|---|---|---|---|---|
| urchade/gliner_medium-v2.1 | 0.90 | 0.78 | 1.60 | 1.00 |
| urchade/gliner_multi-v2.1 | 1.80 | 1.12 | 6.06 | 2.00 |
| fastino/gliner2-multi-v1 | 1.68 | 1.08 | 2.50 | 1.60 |
| fastino/gliner2.5-multi-v1 | 1.48 | 0.81 | 2.14 | 1.30 |

#### Interleaved, 2 threads (peak RSS all four loaded 6437 MiB)

| model | warm median s/page | min | max | median ratio to production (paired by round) |
|---|---|---|---|---|
| urchade/gliner_medium-v2.1 | 0.76 | 0.63 | 0.83 | 1.00 |
| urchade/gliner_multi-v2.1 | 0.98 | 0.91 | 1.10 | 1.32 |
| fastino/gliner2-multi-v1 | 1.02 | 0.90 | 1.15 | 1.36 |
| fastino/gliner2.5-multi-v1 | 0.95 | 0.89 | 1.04 | 1.26 |

#### Separate processes (3 per model at 4 threads, 1 at 2 threads)

`load s` is the time to construct the model; run 1 of each model read a cold page cache. CPU s is process CPU time (all threads) per page.

| model | threads | run | load avg (1 min) before | load s | first page s | warm median s/page | warm min-max | warm CPU s/page (median) | 23 sentences s | peak RSS MiB |
|---|---|---|---|---|---|---|---|---|---|---|
| urchade/gliner_medium-v2.1 | 4 | 1 | 3.39 | 43.3 | 4.88 | 1.62 | 0.94-3.38 | 2.93 | 4.8 | 2002 |
| urchade/gliner_medium-v2.1 | 4 | 2 | 2.96 | 4.8 | 0.69 | 0.62 | 0.59-4.52 | 1.74 | 3.1 | 2004 |
| urchade/gliner_medium-v2.1 | 4 | 3 | 3.51 | 6.1 | 8.07 | 2.61 | 0.88-4.67 | 5.06 | 5.8 | 2004 |
| urchade/gliner_multi-v2.1 | 4 | 1 | 4.50 | 50.1 | 5.08 | 0.88 | 0.75-3.56 | 2.47 | 4.3 | 2827 |
| urchade/gliner_multi-v2.1 | 4 | 2 | 2.77 | 7.0 | 0.90 | 2.03 | 0.81-4.80 | 4.80 | 4.6 | 2827 |
| urchade/gliner_multi-v2.1 | 4 | 3 | 6.93 | 8.9 | 13.89 | 9.37 | 3.59-15.40 | 17.62 | 7.9 | 2827 |
| fastino/gliner2-multi-v1 | 4 | 1 | 5.48 | 52.2 | 5.48 | 1.69 | 0.76-4.05 | 4.66 | 4.8 | 2974 |
| fastino/gliner2-multi-v1 | 4 | 2 | 2.74 | 6.5 | 2.49 | 2.61 | 1.48-9.24 | 5.38 | 5.2 | 2974 |
| fastino/gliner2-multi-v1 | 4 | 3 | 7.59 | 8.9 | 30.01 | 6.67 | 4.18-12.40 | 12.95 | 7.3 | 2974 |
| fastino/gliner2.5-multi-v1 | 4 | 1 | 4.55 | 48.7 | 2.66 | 1.21 | 0.78-3.08 | 3.38 | 3.3 | 2828 |
| fastino/gliner2.5-multi-v1 | 4 | 2 | 3.84 | 8.3 | 9.17 | 0.96 | 0.82-8.62 | 2.58 | 3.2 | 2828 |
| fastino/gliner2.5-multi-v1 | 4 | 3 | 7.28 | 8.2 | 6.73 | 6.82 | 1.84-11.70 | 12.37 | 8.0 | 2828 |
| urchade/gliner_medium-v2.1 | 2 | 1 | 6.77 | 4.8 | 1.08 | 1.12 | 0.75-1.45 | 1.85 | 3.9 | 2004 |
| urchade/gliner_multi-v2.1 | 2 | 1 | 5.77 | 6.8 | 1.60 | 1.12 | 0.98-1.46 | 2.07 | 3.5 | 2827 |
| fastino/gliner2-multi-v1 | 2 | 1 | 3.63 | 6.3 | 1.30 | 1.03 | 0.92-1.20 | 2.05 | 3.8 | 2974 |
| fastino/gliner2.5-multi-v1 | 2 | 1 | 2.44 | 6.3 | 1.10 | 1.00 | 0.88-1.16 | 1.98 | 3.3 | 2828 |

With 4 threads on a contended box, the per-run medians differ by up to about 10× between runs of the same model. They follow the load average, not the model, so they cannot rank the models. The 2-thread runs and the interleaved runs are steadier.

### Peak RAM (RSS, whole benchmark process)

| model | peak RSS |
|---|---|
| production | 2002 MiB |
| gliner multi-v2.1 | 2827 MiB |
| **GLiNER2 multi** | 2974 MiB |
| GLiNER2.5 multi | 2828 MiB |

The cards describe GLiNER2 as CPU-first, but these weights are F32 at about
300M parameters. GLiNER2 multi peaks **about 970 MiB above production** in
the same process (2,974 vs 2,002-2,004 MiB), and gliner multi-v2.1 and
GLiNER2.5 multi peak about 825 MiB above it. With all four models loaded in
one process (the interleaved runs) the peak was 6,436 MiB. I have not
measured a low-memory laptop. On an 8 GB machine that is also running a
browser, the extra gigabyte is a real share of the RAM. I did not measure
fp16, quantised or ONNX variants.

### Download size (first use)

From the Hub API (`?blobs=true`) and the cache after loading. These are the
files each loader actually fetches.

| model | fetched | size |
|---|---|---|
| production `gliner_medium-v2.1` | `model.safetensors` **and** `pytorch_model.bin` (gliner 0.2.30 `snapshot_download`s the whole repo), configs, `microsoft/deberta-v3-base` tokenizer (2.5 MB) | **1,564 MB** (780.7 + 780.8 + 2.5) |
| `gliner_multi-v2.1` | both weight files, `microsoft/mdeberta-v3-base` tokenizer (4.3 MB) | 2,316 MB (1,155.8 + 1,155.9 + 4.3) |
| GLiNER2 multi `gliner2-multi-v1` | `model.safetensors`, `tokenizer.json`, `spm.model`, configs (`hf_hub_download` per file) | **1,249 MB** (1,228.4 + 16.3 + 4.3) |
| `gliner2.5-multi-v1` | `model.safetensors`, `tokenizer.json`, configs | 1,166 MB (1,149.5 + 16.0) |

A side finding: the production warning in `_get_gliner` says "downloading
~300MB". On this gliner version the first download is about 1.56 GB, because
both weight formats are fetched. This report changes no production code, so
the message is not fixed here.

### Install size of the extra dependencies

Measured from each installed distribution's RECORD file list
(`importlib.metadata`), on top of the core install, which already had numpy,
pydantic, requests, packaging and onnxruntime.

| set | packages | size |
|---|---|---|
| shared by both | torch 2.15.0+cpu (752.6 MB), transformers (52.1), sympy (26.8), tokenizers (11.7), hf-xet (11.7), networkx (7.3), huggingface_hub (3.3), regex (3.1), mpmath (2.0), safetensors (1.3), fsspec (0.8), filelock (0.6), tqdm (0.2) | 873.5 MB |
| `gliner` only | gliner (1.4), sentencepiece (5.5) | 6.9 MB |
| `gliner2[local]` only | gliner2 (1.4), peft (3.0), accelerate (1.5), psutil (0.5) | 6.4 MB |

The install size is a wash. torch is 86% of either extra. `gliner2` 2.1.0
does not depend on `gliner`. Its own requirements are pydantic, requests,
tqdm and urllib3, and the `[local]` extra adds torch, transformers, peft,
safetensors and numpy. Swapping would replace the `ner` extra's one package,
not add a second stack.

## License findings

Fetched 2026-11-18 (05:37 UTC) from the Hugging Face model cards
(`https://huggingface.co/<id>/raw/main/README.md`) and the Hub API
(`https://huggingface.co/api/models/<id>`):

- **`fastino/gliner2-multi-v1`**,
  https://huggingface.co/fastino/gliner2-multi-v1 (revision `ce747d79`,
  last modified 2026-09-28). The card front matter says `license: apache-2.0`,
  and the card body says: *"This project is licensed under the Apache License
  2.0."* The API reports the tag `license:apache-2.0`. The card's `language`
  list is `fr, en, es, de, it, pt`, so the multilingual reach measured above
  is beyond what the card claims. The card names no training datasets
  ("Multi-domain datasets for NER, classification, and structured
  extraction").
- **`fastino/gliner2.5-multi-v1`**,
  https://huggingface.co/fastino/gliner2.5-multi-v1 (revision `7b005274`,
  last modified 2026-11-05). The front matter says `license: apache-2.0`, and
  the "License" section says *"Apache License 2.0."* Its language is
  `multilingual, en`, and the encoder is `microsoft/mdeberta-v3-base`.
- For comparison, `urchade/gliner_medium-v2.1` and `urchade/gliner_multi-v2.1`
  both carry `license: apache-2.0`. Their cards note that the v0/v1 GLiNER
  models were cc-by-nc-4.0 and v2/v2.1 are apache-2.0.

So, as of 2026-11-18, both GLiNER2 multilingual model cards license the
weights as Apache-2.0. The issue reported that no license could be found on
2026-10-21. But the repo's commit list
(https://huggingface.co/api/models/fastino/gliner2-multi-v1/commits/main)
shows no commit after `ce747d79` (2026-09-28), and that is the revision read
here. So the `gliner2-multi-v1` card already said Apache-2.0 on 2026-10-21.
Neither card identifies the training data or its licenses.

## Recommendation

**Don't swap yet; needs more data.** The reasons:

1. **Multilingual is a clear win where it counts for the mission.** GLiNER2
   multi finds 24/27 names (loose), where production finds 14/27. It gets the
   scripts production misses entirely, and it does not lose English quality
   on our page. If we only had to choose on accuracy, it would win.
2. **But the evidence is small.** One English page with 17 NER golds and 27
   multilingual golds that I wrote myself. A one-entity difference moves F1
   by about 0.03-0.06. Before changing the default for every user we need a
   few dozen real pages, in several languages, with gold checked by native
   speakers.
3. **Memory is about 1 GiB higher** (2,974 vs 2,002 MiB peak RSS). That works
   against "the tool works on a $300 laptop". Swapping in an F32 300M model
   by default needs either a smaller variant or a measured fp16/int8 load
   that keeps the quality above.
4. **It is slower.** In the paired runs a page takes 1.36× production's time
   with 2 threads (1.02 vs 0.76 s) and 1.60× with 4 threads (1.68 vs 0.90 s).
   The machine was busy, so the absolute seconds are upper bounds. The ratios
   still say it is not faster, whatever the "CPU first" claim on the card.
   A real 2-core laptop has not been measured.
5. **Neither model fixes CJK.** The gap is word splitting, not the model.
   GLiNER2's char splitter fixes CJK but breaks the other scripts, so it is
   a separate issue whatever we pick.
6. **Download and install are not obstacles.** GLiNER2 multi downloads less
   than production does today (1.25 vs 1.56 GB), and the extra is the same
   size.

The cheap next steps would be: fix the "~300MB" message (or fetch only
safetensors), build a bigger multilingual gold set, and rerun this benchmark
on a quiet 2-core machine with an fp16 load. `gliner_multi-v2.1` is *not* the
fallback answer: it is worse than GLiNER2 multi on both corpora, and costs
about the same memory. GLiNER2.5 multi is the closest alternative. It also
finds 24/27 multilingual names (loose), with 146 MiB less peak RSS and a
1.26-1.30× page time. But it has worse English spans (strict NER F1 0.71) and
misses the street address, so it is not clearly better than GLiNER2 multi.

## Reproducing

```bash
pip install -e ".[dev]" gliner==0.2.30 "gliner2[local]==2.1.0"
# Default run (regex + production tier 2), unchanged:
python tests/benchmark_extraction.py
# One model per process; fastino/* load with gliner2:
python tests/benchmark_extraction.py --model fastino/gliner2-multi-v1 \
  --repeat 11 --threads 4 --multilingual --json g2.json
python tests/benchmark_extraction.py --model urchade/gliner_medium-v2.1 \
  --repeat 11 --threads 4 --multilingual --json medium.json
# GLiNER2 char word splitter:
python tests/benchmark_extraction.py --model fastino/gliner2-multi-v1 \
  --word-splitter char --multilingual --repeat 11 --threads 4
# Paired speed (all models in one process, round-robin):
python tests/benchmark_extraction.py --interleave --repeat 11 --threads 4 \
  --model urchade/gliner_medium-v2.1 --model fastino/gliner2-multi-v1
# Follow-up (2026-11-19): the two user choices through the production
# selection; --multilingual scores both the 23 sentences and NEWS.
python tests/benchmark_extraction.py --ner-model english \
  --repeat 11 --threads 2 --multilingual --json english.json
python tests/benchmark_extraction.py --ner-model multilingual \
  --repeat 11 --threads 2 --multilingual --json multilingual.json
```

The first use downloads the models (see the sizes above). Set
`HF_HUB_OFFLINE=1` once they are cached. The script is not collected by
pytest, because its name does not start with `test_`, and CI's
`-k "not benchmark"` excludes it anyway. The per-process outputs behind every
number here are in `docs/benchmarks/gliner2-raw.json`; those of the
follow-up are in `docs/benchmarks/gliner2-news-raw.json`.

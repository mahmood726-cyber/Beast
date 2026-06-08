# Beast — living updater of Pairwise70 + meta-analysis surveillance

**Beast is a self-running Python app that (1) keeps the
[Pairwise70](https://github.com/mahmood726-cyber/Pairwise70) Cochrane dataset
continuously growing with newly-published meta-analyses, and (2) tracks how the
pooled evidence on tracked topics changes over time.**

On each run Beast first **auto-updates Pairwise70** — it asks what Cochrane
reviews are currently published, keeps only the ones not already in the dataset,
extracts their study-level data, and **appends them** (append-only, no
duplicates, no clobber). Then it does the **trend-tracking** on top: re-pools each
tracked topic, stores a timestamped snapshot, and **flags meaningful changes**
versus the last snapshot — an effect-size shift, a significance flip, new trials,
or a change in heterogeneity.

So Beast = **a living updater of Pairwise70 + a trend tracker**. The
dataset-updating is the primary, reliable, idempotent core; the random-effects
engine is validated against R's `metafor` (see [Validation](#validation)).

---

## What it tracks

On every run, per topic, Beast records:

| Field | Meaning |
|---|---|
| **Pooled estimate + 95% CI** | Random-effects (REML / Paule-Mandel / DerSimonian-Laird), on the natural scale (OR/RR/MD/SMD) |
| **Prediction interval** | Where a future study's true effect is expected (Cochrane Handbook v6.5, `t_{k-1}`) |
| **I² and τ²** | Heterogeneity — proportion *and* magnitude (both reported; I² is the estimator-aware Higgins–Thompson form) |
| **k** | Number of contributing trials |
| **Significance** | Whether the CI excludes the null |
| **Study set** | Which trials are in the pool (drives "new trial" detection) |

And between consecutive snapshots it flags five families of **change**
(`info` < `notable` < `major`):

- `new_trials` / `removed_trials` — the evidence base grew or shrank
- `effect_shift` — the pooled estimate moved appreciably
- `direction_flip` — the effect crossed the null (sign change) — *major*
- `significance_flip` — the result gained or lost significance — *major*
- `heterogeneity_change` — I² moved appreciably

### A real example (shipped, runs offline)

The bundled starter topic is **Cochrane CD000028** — antihypertensive therapy in
the elderly, all-cause mortality (60–79y), real trial-level data from the
[Pairwise70](https://github.com/mahmood726-cyber/Pairwise70) dataset. Because
each trial carries a publication year, Beast reconstructs the *cumulative*
meta-analysis as it stood at each point in history:

```
year   k    OR     95% CI            I²      significant
1986   4    0.948  [0.750, 1.198]    0.0%    no
1989   5    0.901  [0.742, 1.094]    0.0%    no
1991   7    0.820  [0.692, 0.971]   20.8%    YES   <- significance flip (major)
1993   9    0.804  [0.678, 0.955]   51.7%    YES   <- I² jumps +32 pts (major)
```

This is the whole point of Beast: the mortality benefit of treating
hypertension in the elderly was *not* statistically detectable in 1986 and
*became* so by 1991 as trials accumulated — and Beast flags exactly that
transition. No fabricated data; it is the real CD000028 evidence replayed by year.

---

## Auto-updating Pairwise70 (the primary core)

Beast keeps the Pairwise70 dataset current. The update step is:

1. **Discover** — list currently-published Cochrane reviews via the Crossref REST
   API (CDSR journal ISSN `1469-493X`) — DOIs, titles, publication dates. Pass
   `--since YYYY-MM-DD` to only consider recent ones.
2. **Dedupe** — keep only review ids not already in the dataset. Existing ids are
   derived from the original `data/*.rda` filenames (the 595) **plus** Beast's own
   append ledger (`beast_manifest.json`). Anything already present is skipped
   *before* any extraction.
3. **Extract** — for each genuinely-new review, run the existing
   [cochrane-data-extractor](https://github.com/mahmood726-cyber/cochrane-data-extractor)
   pipeline (a configurable command) to produce its study-level *data-rows* CSV,
   then parse it into trials. Reviews with no pairwise data are recorded as
   `no_data` — never fabricated.
4. **Append** — write a new tidy CSV (`data-raw/beast/<id>_data.csv`) and, if R is
   available, a matching `data/<id>_data.rda` (faithful to `create_rda_files.R`),
   and record the addition in `beast_manifest.json`.
5. **Commit/push** *(optional)* — stage **only** the files Beast added, commit, and
   push to the Pairwise70 repo (fast-forward).

### Safety guarantees (all tested)

The append path (`beast/pairwise70_repo.py`) is the safety boundary:

- **No duplicates** — an id already present is skipped; identical study-level
  content (SHA-256) is skipped even under a different id.
- **No clobber / no data loss** — an existing target file is **never** overwritten
  (writes go to a temp file then atomic-rename; aborts if the target appeared);
  **no file is ever deleted**; the original 595 are treated as immutable.
- **Idempotent** — re-running an update adds nothing; the manifest survives across
  processes so dedupe is durable.
- **Concurrency-safe** — the claim→write→record step runs under an inter-process
  lock, and both the manifest read and its atomic replace retry through the
  transient Windows sharing-violations a concurrent peer causes, so two
  overlapping runs (e.g. `beast loop` + a cron `beast run`) can never lose-update
  the ledger or drop a real addition. A lock left by a crashed process is
  reclaimed once stale. A corrupt/unreadable manifest **fails closed** (never
  silently reset, which would risk re-appending).
- **Isolated** — one failing or no-data review (extraction *or* write error)
  never aborts the batch.

This was verified against a copy of the **real 595-file** dataset: feeding a mix
of existing + new reviews appended only the new ones, left all 595 originals
byte-identical, and a re-run added zero.

### Running the update

```bash
# Discover-only (safe; writes nothing) — see what's new:
python -m beast update --pairwise70 /path/to/Pairwise70 --since 2025-06-01

# Append new reviews using the real extractor, then commit (and push):
python -m beast update --pairwise70 /path/to/Pairwise70 --since 2025-06-01 \
  --extractor-cmd "python bulk_downloader.py --doi {doi} --out {out}" \
  --extractor-cwd /path/to/CochraneDataExtractor \
  --commit --push
```

`beast run` and `beast loop` accept the same `--pairwise70 …` flags and perform
the update step **before** trend-tracking, so a single scheduled command keeps the
dataset current *and* tracks the trends. The placeholders `{doi}`, `{review_id}`
and `{out}` are filled per review; the command must write a Cochrane data-rows CSV
to `{out}`.

> Note: Beast ships the *machinery* to grow Pairwise70 safely. It only appends
> real data from the real extractor — it does not fabricate datasets, so this
> repository does not push synthetic entries to Pairwise70.

---

## Architecture

```
beast/
  meta.py            Random-effects engine: DL / PM / REML τ², Q, I², τ², CI,
                     prediction interval (t_{k-1}), Knapp–Hartung option.
  effects.py         Trial -> (yi, vi): OR / RR (log scale), MD, SMD (Hedges g),
                     generic inverse-variance. Conditional 0.5 continuity correction.
  snapshot.py        Pool a topic's trials into a timestamped Snapshot (+ content hash).
  diff.py            Detect & grade meaningful changes between two snapshots.
  store.py           SQLite persistence; idempotent (content-hash dedupe).
  tracker.py         Orchestrate a run; backfill a topic's history by year.
  scheduler.py       Self-running interval loop (fail-closed per run).
  report.py          JSON feed + self-contained offline HTML dashboard.
  config.py          Home dir, paths, topics.json.
  logging_setup.py   Console + rotating file logging.
  cli.py             `beast` command-line interface.
  pairwise70_repo.py Append-only, idempotent writer for the Pairwise70 dataset
                     (no dupes / no clobber / no data loss).
  updater.py         Orchestrate the auto-update: discover -> dedupe -> extract ->
                     append -> commit/push.
  sources/
    base.py          Source ABC + TopicSpec (fail-closed contract).
    pairwise70.py    Offline real Cochrane data (CSV or .rda), as_of_year cumulative.
    europepmc.py     Live PubMed/Europe PMC search (RCTs); abstract effect parser.
    rct_extractor.py Corpus of abstracts -> trials via the rct-extractor-v2 engine
                     (17 specialties); engine imported lazily, fail-closed.
  ingest/
    base.py          CochraneFeed + StudyExtractor ABCs, ReviewRef, DOI->id.
    cochrane.py      Crossref CDSR feed + ProcessExtractor (reuses the real
                     cochrane-data-extractor pipeline). Both mockable.
tests/               109 offline tests (synthetic + real sample vs metafor gold;
                     updater safety vs the real 595-file structure).
```

**Data flow per run:** *(update)* `feed.list_reviews → dedupe vs dataset →
extractor.extract → repo.add_dataset (append-only) → commit/push`, then *(track)*
`source.fetch(topic) → compute_effects → meta_analyze → compute_snapshot →
store.add_snapshot (idempotent) → diff_snapshots → store.add_changes → report`.

### Data sources

- **`pairwise70`** *(offline, real)* — reads the Pairwise70 Cochrane dataset (595
  meta-analyses) either as a tidy CSV or a native `*.rda` file, filtered to one
  analysis/subgroup. Supports `as_of_year` to reconstruct historical snapshots.
  The `.rda` path needs `pyreadr`; the CSV path needs nothing extra.
- **`europepmc`** *(live)* — searches Europe PMC for RCTs on a topic query and
  parses an effect estimate from any abstract that reports one with a 95% CI.
  Nothing is fabricated — a trial contributes an effect only if one is literally
  stated. For rigorous full-text extraction, plug in
  [rct-extractor-v2](https://github.com/mahmood726-cyber/rct-extractor-v2). Needs
  `requests`; fail-closed on transport/payload errors.
- **`rct_extractor`** *(corpus, via the rct-extractor-v2 engine)* — extracts
  poolable trial effects from a corpus of abstracts (a JSON list of
  `{study, text, year?}` or a folder of `*.txt`) across 17 disease specialties,
  using the pip-installable
  [rct-extractor-v2](https://github.com/mahmood726-cyber/rct-extractor-v2) engine
  (`pip install "git+https://github.com/mahmood726-cyber/rct-extractor-v2.git"`).
  The engine is imported lazily — Beast does not hard-depend on it — and the
  source is fail-closed (raises if the engine is missing, the corpus is empty, or
  no poolable trial is produced). Supports `as_of_year`. Example:
  `--source rct_extractor --params '{"corpus":"abstracts/","specialty":"diabetes"}'`.

---

## Install

```bash
git clone https://github.com/mahmood726-cyber/Beast.git
cd Beast
pip install -r requirements.txt          # numpy + scipy (core)
# optional extras:
pip install pyreadr                       # to read Pairwise70 .rda files
pip install requests                      # to use the Europe PMC live source
```

Or install as a package (gives you the `beast` command):

```bash
pip install -e .
```

Requires Python ≥ 3.10.

---

## Quick start (offline, ~5 seconds)

```bash
python -m beast init                      # create ./beast_data + a real starter topic
python -m beast backfill --topic htn-elderly-mortality --years 1980,1986,1989,1991,1992,1993
python -m beast history --topic htn-elderly-mortality
python -m beast report                    # writes beast_data/reports/index.html
```

Open `beast_data/reports/index.html` — a self-contained dashboard with the trend
chart (estimate + CI band over time), I², k, and the flagged changes feed. No
network, no CDN.

---

## Running it self-running

Beast is built to run unattended. Three options:

**1. Built-in scheduler (one persistent process):**

```bash
python -m beast loop --interval 86400     # run every 24h; Ctrl-C to stop
```

The loop is fail-closed: an error in one run is logged and the loop continues.

**2. cron (Linux/macOS):**

```cron
# every day at 03:00 — update Pairwise70 with new reviews, then snapshot/diff/dashboards
0 3 * * *  cd /path/to/Beast && /usr/bin/python -m beast run \
  --pairwise70 /path/to/Pairwise70 --since 2025-01-01 \
  --extractor-cmd "python bulk_downloader.py --doi {doi} --out {out}" \
  --extractor-cwd /path/to/CochraneDataExtractor --commit --push \
  >> beast_data/cron.log 2>&1
```

(Omit the `--pairwise70 …` flags to only do trend-tracking.)

**3. Windows Task Scheduler:**

```powershell
$action  = New-ScheduledTaskAction -Execute "python" -Argument "-m beast run" -WorkingDirectory "F:\Beast"
$trigger = New-ScheduledTaskTrigger -Daily -At 3am
Register-ScheduledTask -TaskName "BeastSurveillance" -Action $action -Trigger $trigger
```

Each `beast run` is **idempotent**: if a topic's evidence hasn't changed since the
last snapshot, no duplicate is stored and no spurious change is flagged. Storage
is a single SQLite file (`beast_data/beast.db`); logs rotate in `beast_data/beast.log`.

---

## Adding your own topics

**Offline Cochrane data (CSV):** a tidy CSV with `study, year` and either binary
counts (`e_events, e_n, c_events, c_n`), continuous (`e_mean, e_sd, e_n, c_mean,
c_sd, c_n`), or generic (`yi, sei`):

```bash
python -m beast add --id my-topic --title "My question" \
  --source pairwise70 --measure OR --params '{"csv": "path/to/data.csv"}'
```

**Native Pairwise70 `.rda`:**

```bash
python -m beast add --id stroke-aspirin --title "Aspirin for stroke" \
  --source pairwise70 --measure OR \
  --params '{"rda": "Pairwise70/data/CD000029_pub3_data.rda", "analysis_number": 1, "subgroup": "All"}'
```

**Live Europe PMC surveillance:**

```bash
python -m beast add --id sglt2-hf --title "SGLT2 inhibitors in heart failure" \
  --source europepmc --measure GEN \
  --params '{"query": "empagliflozin AND heart failure AND mortality", "min_year": 2015}'
python -m beast run                       # fetches current RCTs, pools, snapshots
```

Then schedule `beast run` — each run appends to the trend and flags what changed.

---

## CLI reference

| Command | Purpose |
|---|---|
| `beast init` | Create home dir + a real starter topic |
| `beast add …` | Add/update a tracked topic |
| `beast list` | List topics and their latest pooled state |
| `beast update --pairwise70 …` | Auto-update the Pairwise70 dataset with new Cochrane reviews |
| `beast run [--pairwise70 …]` | (Optionally update Pairwise70 then) fetch, recompute, snapshot and diff every topic |
| `beast backfill --topic ID --years …` | Reconstruct a topic's historical trend (Pairwise70) |
| `beast loop --interval N` | Self-running scheduler |
| `beast history --topic ID` | Print a topic's trend |
| `beast report` | (Re)write the JSON + HTML dashboards |

---

## Validation

The engine is checked against R `metafor` 4.x (`rma` / `escalc` / `predict`) with
gold values pinned in `tests/fixtures/metafor_gold.json`:

- **Effect sizes** (OR, OR with zero-cell correction, RR, MD, SMD/Hedges g) match
  `escalc` to **< 1e-9**.
- **Pooled estimate, CI, SE, τ², I²**: DerSimonian–Laird matches to **< 1e-6**;
  REML to **< 1e-4**; Paule–Mandel to within metafor's own PM tolerance.
- **Knapp–Hartung** CI matches `metafor` `test="knha"` to **< 1e-8**.
- The **prediction interval** follows the Cochrane Handbook v6.5 `t_{k-1}` rule and
  is checked against its closed form.

Methodological choices (no DL for small k → REML/PM available, pool ratios on the
log scale, conditional continuity correction, report τ² alongside I²) follow
standard guidance.

### Run the tests

```bash
pip install pytest
python -m pytest            # 109 tests, fully offline, ~5s
```

---

## Design guarantees

- **Fail-closed** — a transport error or malformed payload raises rather than
  returning an empty pool that would look like "the evidence vanished".
- **Idempotent** — re-running stores no duplicates and raises no false changes
  (content-hash dedupe).
- **Isolated** — one failing topic never aborts a run.
- **No fabricated data** — offline tests use a real Cochrane sample or synthetic
  fixtures; live fetches are mocked in tests; an effect is emitted only when one
  is actually reported.

## License

MIT — see [LICENSE](LICENSE).

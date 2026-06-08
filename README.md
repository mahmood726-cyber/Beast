# Beast — living meta-analysis surveillance

**Beast is a self-running Python app that tracks how meta-analysis evidence
changes over time.** For each topic you tell it to watch, it periodically
re-fetches the trial set, recomputes a random-effects pooled estimate, stores a
timestamped snapshot, and **flags meaningful changes** versus the last snapshot —
an effect-size shift, a significance flip, new trials, or a change in
heterogeneity. It is "living" surveillance of how the conclusion on a topic
shifts as new evidence accumulates.

The random-effects engine is validated against R's `metafor` (see
[Validation](#validation)).

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
  sources/
    base.py          Source ABC + TopicSpec (fail-closed contract).
    pairwise70.py    Offline real Cochrane data (CSV or .rda), as_of_year cumulative.
    europepmc.py     Live PubMed/Europe PMC search (RCTs); abstract effect parser.
tests/               63 offline tests (synthetic + real sample vs metafor gold).
```

**Data flow per run:** `source.fetch(topic) → compute_effects → meta_analyze →
compute_snapshot → store.add_snapshot (idempotent) → diff_snapshots → store.add_changes
→ report`.

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
# every day at 03:00 — re-fetch, snapshot, diff, regenerate dashboards
0 3 * * *  cd /path/to/Beast && /usr/bin/python -m beast run >> beast_data/cron.log 2>&1
```

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
| `beast run` | Fetch, recompute, snapshot and diff every topic once |
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
python -m pytest            # 63 tests, fully offline, ~5s
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

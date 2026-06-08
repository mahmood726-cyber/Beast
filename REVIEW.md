# Beast — multipersona review & bug hunt

**Date:** 2026-06-08 · **Reviewer:** rigorous adversarial pass (data-integrity,
statistician, software-engineer, security personas) · **Scope:** the
self-running Pairwise70 living-updater + meta-analysis trend tracker at
`mahmood726-cyber/Beast`, commit `6aaaff0` (pulled fresh; up to date).

**Verdict:** the core append boundary was sound in single-process use but had a
**real, reproducible cross-process data-durability bug** (lost manifest updates)
that I fixed and proved with a 10-process stress test. Five fixes landed with
tests; the test suite went **88 → 109**, all green and flake-free across repeated
runs. The statistics engine is correct and well-validated; one change-detection
threshold was tightened to kill a class of spurious "major" alerts. Issue #1's
`rct_extractor` source was added (safe, lazy, fail-closed, offline-tested).

Severity scale: **P0** = data loss/corruption or wrong results in the normal path;
**P1** = correctness/robustness gap reachable in realistic operation; **P2** =
latent/edge or polish.

---

## Data-integrity reviewer (most important)

The append path (`beast/pairwise70_repo.py`) is the safety boundary. I stress-tested
dedupe (id + content SHA), the atomic temp-write+rename, manifest durability, and
idempotency against: concurrent runs, partial/failed extractions, same-id-different-
content, content-match-under-different-id, corrupted manifest, interrupted run.

### DI-1 — Concurrent runs lose-update the manifest *(P0 — FIXED)*

The README promised "the manifest survives across processes so dedupe is durable",
but it did **not**. `add_dataset` was a lock-free read-modify-write: each process
loaded the manifest at `__init__`, appended in memory, and `os.replace`'d the whole
file. Two overlapping runs (the documented `beast loop` + cron `beast run` pattern)
interleave as:

```
A loads [N]   B loads [N]
A adds X → writes [N+X]
B adds Y → writes [N+Y]      ← X is gone from the ledger
```

X's CSV stays on disk but drops out of the durable dedupe ledger: it is never
git-staged (so never committed/pushed), and a later run can re-add identical
content under a different id (a duplicate-content leak). **My 10-process stress
test reproduced it: only 9/10 additions survived.**

**Fix:** the whole claim→write→record step now runs under a cross-process
`_FileLock` (exclusive-create lockfile, bounded acquisition, stale-lock reclaim so
a crash never deadlocks). Critically, the manifest is **re-read inside the lock**
before append, so a peer's just-recorded entries are merged rather than clobbered.
Re-run of the stress test: **10/10 durable, originals byte-identical, no lock
leftover.** Tests: `test_concurrent_instances_do_not_lose_manifest_entries`,
`test_threaded_adds_all_persist`, `test_concurrent_same_content_under_new_id_deduped`.

### DI-2 — Atomic replace fails under Windows concurrency *(P0 — FIXED)*

Surfaced *by* the DI-1 stress test on the real target platform (Windows 11). Even
with writes serialized by the lock, a concurrent **reader** in another process's
`__init__` holds the manifest open at the instant a writer calls `os.replace`,
which Windows rejects with a sharing violation (`WinError 5` →
`PermissionError`). The addition was lost. This is the failure mode that makes
"atomic rename" *not* atomic across processes on Windows.

**Fix (both sides):**
- writer — `_save_manifest` retries `os.replace` through the transient
  `PermissionError` window;
- reader — `_load_manifest` retries the `open()` read through the same window
  *and* no longer mislabels a transient sharing violation as "corruption".

Test: `test_manifest_save_retries_transient_windows_sharing_violation` (deterministic,
monkeypatched). 40/40 threaded iterations and 10/10 cross-process now clean.

### DI-3 — Lock acquire spuriously failed on Windows pending-delete *(P1 — FIXED)*

A second Windows quirk found while hardening DI-2: when a lock holder is unlinking
the lockfile during `release()`, a peer's `os.open(O_CREAT|O_EXCL)` can return
`PermissionError` (ACCESS_DENIED on a pending-delete file) instead of
`FileExistsError`. The acquire loop only caught `FileExistsError`, so the peer's
acquisition raised and aborted that review's append. **Fix:** acquire treats
`PermissionError` as contention and retries. (Caught only because the threaded test
was flaky 2/8 before this; now 6/6 stable, 40/40 in a tight loop.)

### DI-4 — Corrupt/unreadable manifest now fails closed, explicitly *(P1 — FIXED)*

`json.load` raised a bare `JSONDecodeError` that bricked `__init__` with an opaque
error, and a structurally-valid-but-wrong manifest (`{}`, `{"added": "x"}`) would
later `KeyError` mid-operation. Worse would have been *silently* treating it as
empty — that throws away the dedupe ledger and re-appends everything. **Fix:**
`_load_manifest` validates structure and raises a clear `ManifestError`, while
distinguishing genuine corruption (bad JSON/structure → fail closed) from a
transient read sharing-violation (→ retry, per DI-2). Tests:
`test_corrupt_manifest_fails_closed_on_construct`,
`test_structurally_invalid_manifest_fails_closed`,
`test_corrupt_manifest_during_run_does_not_destroy_it` (asserts the corrupt file is
left intact for the operator, not overwritten).

### Confirmed-correct (tried to break, could not)

- **Same id, different content** → `skipped_exists`; the existing dataset is never
  updated/clobbered (append-only is intentional — the 595 and prior additions are
  immutable).
- **Content match under a different id** → `skipped_dup_content`; now durable across
  processes (DI-1).
- **Partial/failed extraction** → `NoDataError`/exception recorded, id **not** marked
  present, batch continues.
- **Interrupted run** (crash between CSV write and manifest save) → orphan CSV on
  disk, **not** in ledger; next run re-extracts, hits `FileExistsError`, returns
  `skipped_exists` → **no clobber, no duplicate**. (Residual: the orphan CSV is never
  committed; acceptable and noted below.)
- **The 595 originals** stay byte-identical under every scenario above (asserted in
  `test_original_rda_never_modified_or_deleted` and the new concurrency tests). No
  code path deletes a dataset file.

---

## Meta-analysis statistician

Engine (`meta.py`, `effects.py`, `snapshot.py`, `diff.py`) reviewed against the
user's `advanced-stats.md` gotchas and the pinned `metafor` gold.

**Correct and well-grounded (no change needed):**
- τ² via DL / Paule-Mandel (monotone bisection) / REML (DL-seeded fixed point),
  truncated at 0; REML default, DL not relied on for small k. ✔ matches guidance.
- Estimator-aware Higgins-Thompson **I² = τ²/(τ²+s²)** with s² = df/C, reported
  alongside τ² — not the naive (Q−df)/Q. ✔
- **Prediction interval = t_{k−1}** (Cochrane Handbook v6.5), undefined for k<2. ✔
  exactly the resolved rule in `advanced-stats.md`.
- Ratio measures pooled on the **log scale**; **conditional** 0.5 continuity
  correction (only when a cell is 0); SMD Hedges-g with the exact lgamma J. ✔
- Knapp-Hartung with a variance-inflation **floor of 1** (CI can only widen). ✔
- `significant` derived from the CI excluding the analysis-scale null. ✔
- Idempotency hash rounds estimate/CI/I²/τ² so float noise never forges a change. ✔

### ST-1 — `direction_flip` had no magnitude gate *(P1 — FIXED)*

A pooled estimate hovering on the null flips sign on pure noise (e.g. OR
0.999→1.001), which fired a **"major: direction reversed"** alert — a false alarm
in a tool whose entire value is trustworthy alerts. **Fix:** require at least one
side to be an appreciable distance from the null (the existing `effect_shift`
threshold) before flagging a flip. Genuine reversals (the existing
`test_direction_flip_is_major`, an OR 0.86→1.16 move) still fire; near-null wobble
no longer does. Test: `test_direction_flip_gated_by_magnitude`.

### Thresholds — judged sound

`effect_shift` 0.10 / major 0.25 log-units, `i2_shift` 15 / major 30 points,
`significance_flip` and `removed_trials` always major: defensible, tunable
(`DiffThresholds`), and consistent with how the bundled CD000028 example reads. No
change beyond ST-1.

### ST-2 — knha p-value not consistent with the knha CI *(P2 — DEFERRED)*

When `knha=True`, the CI uses the t-based inflated SE but `p_value` is still the
Wald-z `mu/se_mu`, so a reported p could disagree with the reported CI.
**Not reached in production** — `compute_snapshot` never enables knha, and
`significant` is taken from the CI, not p. Deferred rather than touch validated
numerics without a fresh metafor gold for the knha p-value.

---

## Software engineer

- **Scheduler** (`scheduler.py`): single-threaded, fail-closed per run, honours
  `stop_event` in short slices, `KeyboardInterrupt` clean. No in-process overlap.
  Cross-process overlap on the *dataset* is now safe (DI-1/2/3). Cross-process
  overlap on the SQLite store uses WAL + per-snapshot hash dedupe; a rare duplicate
  snapshot row is possible under exact simultaneity but is surveillance metadata,
  not the protected dataset — noted, not fixed.
- **Crossref feed** (`cochrane.py`): cursor pagination correct (stops on empty page
  or missing cursor); `from-pub-date` filter correct; bounded retry with backoff;
  **fails closed** (raises rather than returning a partial list) — verified by
  `test_crossref_feed_fails_closed`. ✔

### SE-1 — Per-review isolation gap: `add_dataset` errors aborted the batch *(P1 — FIXED)*

`update_pairwise70` wrapped `extractor.extract` in try/except but **not**
`repo.add_dataset`. A write/ledger error on one review (disk full, permission, the
new lock timeout, a corrupt manifest) propagated and killed the whole batch —
violating the documented "one failing review never aborts the batch". **Fix:** the
append call is now isolated too; the failure is recorded as `failed` and the batch
continues. Test: `test_update_isolates_add_dataset_errors`.

### SE-2 — `ProcessExtractor` silently no-ops without `{out}` *(P2 — FIXED)*

A `command_template` missing the `{out}` placeholder makes the extractor write
nowhere → `NoDataError` for **every** review (a silent, dataset-wide no-op).
**Fix:** validate at construction that the template is non-empty and references
`{out}`; fail fast with a clear message. Test:
`test_process_extractor_requires_out_placeholder`.

### SE-3 — Interrupted-run orphan CSV is never committed *(P2 — DEFERRED, documented)*

Per DI "interrupted run": a crash between CSV write and manifest save leaves an
orphan CSV that is safe (no clobber/dup) but never enters the ledger, so it is
never git-staged. Self-heals to "harmless dead file"; fixing it (e.g. a reconcile
pass that adopts orphan CSVs) is out of scope for this review. Noted here so it is
not mistaken for data loss.

---

## Security reviewer

- **Command construction is injection-safe.** `ProcessExtractor` builds a **list**
  argv and calls `subprocess.run` **without `shell=True`**; placeholders are filled
  as individual argv values, and `str.format` does not recursively expand a value's
  own `{...}`. `review_id` is regex-constrained (`CD\d{4,6}(_pubN)?`); `doi` reaches
  argv only as a literal. No shell metacharacter path. A `timeout` is always set. ✔
- **R subprocess** (`_try_build_rda`): all interpolated values go through
  `json.dumps` (R-compatible string escaping, including Windows backslash paths);
  `object_name` is a safe identifier derived from the constrained id; `timeout=120`.
  No injection avenue from constrained inputs. ✔
- **Network**: both feeds set `timeout` on every request, retry with bounded
  backoff, and reject non-JSON/error payloads (fail closed). No `verify=False`, no
  unbounded calls. ✔
- **No unsafe deserialization**: JSON only; no `pickle`/`eval`/`yaml.load`.
  `pyreadr.read_r` parses data, not code; paths are operator config. ✔
- **HTML report** (`report.py`): data embedded via `json.dumps(...).replace("</",
  "<\\/")` (neutralizes `</script>` breakout) and rendered with `textContent` (no
  `innerHTML`) — no stored-XSS even if a topic title/message contained markup.
  Fully offline, no CDN. ✔
- **Note (not a Beast vuln):** the `_FileLock`/`beast_manifest.lock` live in the
  *Pairwise70* working copy and are never staged by Beast (`added_files()` lists
  only manifest + CSV/rda). If a user runs `git add -A` there, add
  `beast_manifest.json.lock` to that repo's `.gitignore`. Out of scope to edit
  Pairwise70 here.

---

## Issue #1 — `rct_extractor` source (ADDED)

Added `beast/sources/rct_extractor.py` wiring Beast to the now pip-installable
`rct-extractor-v2` engine, exactly as the issue requested, with safety as the
priority:

- Loads a corpus (JSON list of `{study, text, year?}` **or** a folder of `*.txt`)
  and routes the single external call through `_extract_trial_dicts`, which
  **lazily** imports `rct_extractor.integrations.beast.to_beast_trials` — Beast has
  **no hard dependency** on the engine and still imports cleanly without it.
- Materializes returned Trial-shaped dicts via the shared `_row_to_trial` mapper,
  honours `as_of_year`, and is **fail-closed** (raises if the engine is missing, the
  corpus is empty/absent, or no poolable trial is produced — never an empty pool).
- Registered in `get_source` and exposed as a `--source` choice in the CLI.
- 10 offline tests (`test_rct_extractor.py`): JSON + txt-folder corpora, as-of-year
  filtering, every fail-closed branch, registry wiring, and the helpful ImportError
  when the engine is absent. The external call is monkeypatched so the suite stays
  fully offline and needs no third-party install.

---

## Fixed vs deferred — summary

| ID | Persona | Severity | Status | One-line |
|----|---------|----------|--------|----------|
| DI-1 | Data-integrity | P0 | **Fixed** | Concurrent runs lost-updated the manifest; now inter-process lock + read-modify-write-under-lock. |
| DI-2 | Data-integrity | P0 | **Fixed** | Windows sharing-violation broke "atomic" replace under concurrency; reader+writer now retry the transient window. |
| DI-3 | Data-integrity | P1 | **Fixed** | Lock acquire spuriously failed on Windows pending-delete; now treats PermissionError as contention. |
| DI-4 | Data-integrity | P1 | **Fixed** | Corrupt/invalid manifest now fails closed as `ManifestError` (never silently reset). |
| ST-1 | Statistician | P1 | **Fixed** | `direction_flip` gained a magnitude gate (no more near-null sign-wobble "major" alerts). |
| SE-1 | Software eng | P1 | **Fixed** | `add_dataset` errors no longer abort the batch (per-review isolation completed). |
| SE-2 | Software eng | P2 | **Fixed** | `ProcessExtractor` validates the `{out}` placeholder (no silent dataset-wide no-op). |
| #1 | Integration | — | **Added** | `rct_extractor` source: lazy, fail-closed, 10 offline tests. |
| ST-2 | Statistician | P2 | Deferred | knha p-value vs knha CI inconsistency (unreached in pipeline). |
| SE-3 | Software eng | P2 | Deferred | Interrupted-run orphan CSV never committed (harmless; documented). |

**Tests:** 88 → **109**, fully offline, ~5s, green and flake-free across repeated
threaded and 10-process stress runs. Manual end-to-end (`init → backfill →
history`) reproduces the documented CD000028 significance-flip example exactly.

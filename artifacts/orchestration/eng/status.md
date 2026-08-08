# ENG-EXEC status — streams 4–8

**Owner:** ENG-EXEC (reports to CEO / main). **Started** 2026-08-08. Branch `final-dataset` @ `f5a4017`.

## Test baseline — MEASURED on THIS branch, 2026-08-08

```
python3 -m pytest -q  →  1214 passed, 14 warnings in 31.65s
```

⚠️ **`CLAUDE.md`'s "786 passing" is NOT the baseline** — it is measured on
`agent/claude-01/reservoir-ingest`, a different branch. Deliberately not inherited.
⚠️ `python` is not on PATH on this machine; **use `python3`**. (`python -m pytest` → command not found.)

Every merge must be measured against **1214**, not 786.

## Streams

| stream | items | agent | worktree / branch | state |
|---|---|---|---|---|
| 4 | C3b + B6 (plan surface) | eng-04 | `eng-04--plan-surface` / `agent/eng-04/plan-surface` | DISPATCHED |
| 5 | A2a (hash pre-pass) | eng-05 | `eng-05--hash-prepass` / `agent/eng-05/hash-prepass` | DISPATCHED |
| 6 | A2b (keep-list consumer) + B7 + FilterStats receipt | eng-06 | `eng-06--keep-list` / `agent/eng-06/keep-list` | DISPATCHED |
| 7 | C1 (FinePhrase id partition) | eng-07 | `eng-07--finephrase-partition` / `agent/eng-07/finephrase-partition` | DISPATCHED |
| 8 | B1, B2, B3, B4 | eng-08 | `eng-08--parallel-fixes` / `agent/eng-08/parallel-fixes` | DISPATCHED |

5 concurrent, at the cap. No sequential wave needed — the brief's 8 items fit 5 agents once B7 is
re-homed (see `decisions.md` D1).

## Merge order (from BUILD-DEPENDENCY-GRAPH §3 rule 4, as amended by the CEO brief)

stream 4 → stream 7 → stream 6 → stream 5 → stream 8

## Deviations from the CEO brief, filed in `decisions.md`

- **D1** — B7 moved from stream 8 to stream 6. `sink` is a **closure inside `run_bundle`**
  (`corpus_build.py:461`, inside `def run_bundle` at `:429`, which ends at `:570`). The brief's own
  hard rule is one agent owns one FUNCTION; B7 and A2b are the same function.
- **D2** — `load_registry` (`corpus_build.py:130`) and `plan_document` (`:182`) are in
  **`corpus_build.py`, not `corpus.py`**. Only `SHARD_TOKENS` (`corpus.py:89`) and
  `allocate_ordinals` (`corpus.py:315`) are in `corpus.py`. So stream 4 is a **third** editor of
  `corpus_build.py`, not a `corpus.py`-only agent. All three told about each other.
- **D3** — B4 has **no target**: `data_provenance_initiative` does not exist in
  `artifacts/reservoir/corpus-registry.json` (17 rows, none matching; repo-wide grep finds it only
  in the three planning docs). Escalated as a plan question.

## Escalations to CEO

1. (open) DCLM repo reconciliation — `HuggingFaceFW/dclm_100BT` vs `mlfoundations/dclm-baseline-1.0-parquet`. Stream 4 to report with evidence.
2. (open) D3 — B4 target absent from the registry.

---

# 🔁 ENG-EXEC session 2 (RE-DISPATCH) — 2026-08-08

Predecessor died after creating worktrees but **before dispatching a single worker**. Inheriting its
status above verbatim; correcting in place below. Baseline **1214**, `python3`, 5 worktrees clean at
`f5a4017` — all re-verified this session (`git worktree list` shows all 5 at `f5a4017`).

## CEO rulings received (LEDGER `RECOVERY AUDIT`)

- **D1 APPROVED** — B7 stays in stream 6 with A2b. Same function (`run_bundle`).
- **D2 APPROVED** — and it corrects the CEO's own brief. Stream 4 is a **third editor of
  `corpus_build.py`**. All three owners named to each other in their prompts.
- **D3 APPROVED CONDITIONALLY** — B4 struck **only** if re-verified with paste-able grep. See §D3 below.
- **R4** — baseline 1214, `python3`. Already inherited.

## Standing hazard reminders carried into every worker prompt

- NOTHING PROMOTES. No S3 write, no Batch submit, no `manifest.json` anywhere. Code + local tests only.
- Do NOT push. Merges are local; the single push is the CEO's call and goes to `edullm/**` only.
- Write findings to disk **incrementally**. First tool call = create your output file.
- Grade every number MEASURED / MEASURED-IN-CODE / DERIVED / CARD / UNVERIFIED.

## §D3 — B4 RE-VERIFIED AND **STRUCK** (evidence, paste-able)

```
$ grep -n "data_provenance_initiative" artifacts/reservoir/corpus-registry.json
$ echo $?
1                                  # no match, anywhere in the file

$ grep -in "provenance\|fc-cot\|fc_cot\|flan\|\"dpi\"" artifacts/reservoir/corpus-registry.json
                                   # zero output — not under a near-miss spelling either
```

All **17** `corpora` rows, by `source_label | repo` (`MEASURED-IN-CODE`, json-parsed not grepped):

```
finepdfs-edu            | HuggingFaceFW/finepdfs-edu
fineweb-edu             | HuggingFaceFW/fineweb-edu
essential-web           | EssentialAI/essential-web-v1.0
dclm                    | HuggingFaceFW/dclm_100BT
finemath                | HuggingFaceTB/finemath
pes2o                   | common-pile/peS2o_filtered
pubmed                  | common-pile/pubmed_filtered
arxiv                   | common-pile/arxiv_papers_filtered
stackv2-edu             | common-pile/stackv2_edu_filtered
stackexchange           | common-pile/stackexchange_filtered
ubuntu-irc              | common-pile/ubuntu_irc_filtered
github-archive          | common-pile/github_archive_filtered
finewiki                | HuggingFaceFW/finewiki
synthetic-finephrase-faq      | HuggingFaceFW/finephrase
synthetic-finephrase-math     | HuggingFaceFW/finephrase
synthetic-finephrase-table    | HuggingFaceFW/finephrase
synthetic-finephrase-tutorial | HuggingFaceFW/finephrase
```

Repo-wide, `data_provenance_initiative` occurs in exactly 4 non-`.git` places, **all prose**:
`docs/BUILD-DEPENDENCY-GRAPH.md:505`, `docs/TASKS.md:46`, `docs/IMPLEMENTATION-PLAN.md:717`,
`docs/IMPLEMENTATION-PLAN.md:1743` (plus the two ledger/status lines that report this finding).

**Ruling applied: B4 / #16 / Phase-0 item 9 is STRUCK as a no-op.** Stream 8 is now B1, B2, B3 only.
Note for DATA/CEO: the *hazard* B4 describes (`fc-cot-cot_gsm8k`, GSM8K at 6 repeats, 0.51% of tokens)
is real and comes from **Common Pile**; this corpus draws 7 Common Pile rows. Striking B4 removes a
registry edit that has no target — it does **not** clear the underlying contamination question, which
belongs to B5/#24/#17 under DATA. Flagged, not silently dropped.

## Wave 0 DISPATCHED — 5 concurrent workers, 2026-08-08 (session 2)

Predecessor died before dispatching any. All 5 are now live, each on its pre-existing clean worktree
at `f5a4017`, each with a **named output file** under `artifacts/orchestration/eng/` **in the main
worktree** (deliberately — a findings file inside a worker's own branch dies with the branch).

| stream | items | branch | findings file |
|---|---|---|---|
| 4 | C3b(#28) + B6(#9) + `load_registry` uniqueness check | `agent/eng-04/plan-surface` | `stream-04-plan-surface.md` |
| 5 | A2a(#22 producer) | `agent/eng-05/hash-prepass` | `stream-05-hash-prepass.md` |
| 6 | A2b(#22 consumer) + B7(#29) + FilterStats receipt | `agent/eng-06/keep-list` | `stream-06-keep-list.md` |
| 7 | C1(#21) + reader-budget correction | `agent/eng-07/finephrase-partition` | `stream-07-finephrase-partition.md` |
| 8 | B1(#23), B2, B3(#10) — **B4 struck** | `agent/eng-08/parallel-fixes` | `stream-08-parallel-fixes.md` |

Fan-out 5, well under the ≤16 cap. Merge order unchanged: **4 → 7 → 6 → 5 → 8**.

**Cross-ownership declared explicitly in every prompt** (per ruling D2). `corpus_build.py` has THREE
concurrent editors — eng-04 (`load_registry`/`plan_document`), eng-06 (`run_bundle` incl. the `sink`
closure), eng-07 (`_reader_for`) — and each was told the other two by name and function+line. eng-08
was told it does **not** own that file at all, and that its `s3.py` scope is the connection-pool config
only, because eng-06 is adding a bytes-shaped verified-put in the same file.

**Producer/consumer contract flagged as the live coupling risk:** eng-05 produces the keep-list, eng-06
consumes it, concurrently. eng-05 was told to publish the format to its findings file EARLY; eng-06 was
told to read that file, re-read as it goes, and code against a clearly-marked assumption if absent.
This is the one pair that can silently diverge — I will reconcile at merge.

**B3-before-B7 enforced in both directions**: eng-08 told B3 is its priority because eng-06's B7 is
inert without it; eng-06 told not to claim B7 as a win, not to retire `verify --deep` (a job-def
change, not code, and not ENG's), and that retiring `--deep` retires ONE TIER — the cheap tier holds
`bundle-set-mixed-wheel-versions`, the reservoir's live publish blocker.

Every prompt carries: nothing promotes / no S3 / no Batch / no `manifest.json` / no push / Maple not
cited / grade every number / recompute-never-trust / write-to-disk-first-and-incrementally / stop and
write if low on context.

## ⚠️ MISROUTED COORDINATOR MESSAGE — NOT ACTIONED BY ENG (2026-08-08)

Received mid-dispatch: an authorization to run `aws events disable-rule --name
edullm-landing-manifest-created`, with obligations to record the re-enable command in **`plat/status.md`**
and a reminder that the **R3 promote-duration arithmetic** is "your top deliverable."

**I did not execute it, and I believe that is correct.** It is addressed to **PLAT-EXEC**:

1. It names `plat/status.md` as the file to append to — PLAT's file, not `eng/status.md`.
2. R3 is double-assigned by the LEDGER to **PLAT and AUDIT**, "deliberately uncoordinated," and
   explicitly says neither may cite the other. ENG is not a party to it; my acting on it would
   contaminate a deliberately-independent read.
3. Ruling **R1** in the LEDGER authorizes the disable to **PLAT** by name ("PLAT is authorized to
   `events disable-rule`"), and the executives table puts AWS readiness in PLAT's scope with ENG's
   scope as graph streams 4–8 and "does not push."
4. My own brief forbids AWS mutations outright.

**No harm done either way** — R1's rationale is risk asymmetry, and the rule being armed is exactly
why every one of my five workers is under a hard no-`manifest.json`/no-S3/no-Batch constraint. ENG's
streams cannot fire that rule regardless of its state. **Escalated to the CEO to re-route to PLAT.**
Recording it here rather than silently discarding it, because a dropped authorization would look
identical to an unexecuted one.

### Resolution — CEO confirmed the misroute (2026-08-08)

CEO: *"your refusal was correct on all four points, and the misrouting was mine."* Re-routed to
PLAT-EXEC, who owns it under R1 by name. **ENG does not execute it.** Cause: the four launch results
were read in tool-call order, but PLAT's initial dispatch was refused by the permission classifier, so
the agentIds shifted by one.

Two things adopted as standing practice, recorded here because they bind my workers too:

1. **A dropped authorization looks identical to an unexecuted one.** Flag anything misaddressed; never
   silently discard, and never assume a strange instruction is a test of obedience.
2. **The no-S3 / no-Batch / no-`manifest.json` / no-push constraint on all five workers stays absolute
   regardless of how R1 turns out.** R1 is defence in depth for DATA/PLAT's paths, not a permission
   ENG's streams may lean on. If the rule ends up DISABLED, my workers' constraints do not loosen by
   one inch.

**B4 STRUCK — accepted by the CEO**, to be mirrored into the LEDGER citing the §D3 grep evidence above.

## Baseline independently re-confirmed by ENG-EXEC session 2

```
$ python3 -m pytest -q   (main worktree, final-dataset @ f5a4017, tree clean but for artifacts/orchestration/)
1214 passed, 14 warnings in 50.62s
```

Matches the predecessor's measurement exactly (it recorded 31.65 s; the count is what matters). **1214 is
the number every merge is measured against.** Runtime varies with load — do not read a slower wall-clock
as a regression.

Minor environment finding for anyone scripting here: **`timeout` is not on PATH on this machine** (BSD
userland, no coreutils), so `timeout 300 python3 -m pytest` fails with `command not found` and looks
exactly like a test failure. Use the tool's own timeout instead. Same class of trap as `python` vs
`python3`. Grade: MEASURED.

## Supervision log

- **02:11–02:16** — all 5 workers created their findings files as their **first** action, before any
  code work. The hard rule took. Files confirmed non-empty on disk at 02:17.
- **~02:30** — permission-classifier outage hit my session too (`claude-sonnet-5[1m] is temporarily
  unavailable, so auto mode cannot determine the safety of Bash`). **Same outage PLAT reported in the
  prior wave** — so it is recurring infrastructure, not a one-off. Read-only tools kept working;
  supervised through `Read` until it recovered. Worth PLAT/CEO knowing it is still live.

---

# 🔴 ESCALATION E1 — #10 IS NOW A HARD PREREQUISITE OF PUBLISHING AT ALL, NOT AN OPTIMIZATION

**From eng-04 (stream 4), MEASURED + DERIVED. This is the most important thing in the wave so far and
it changes a schedule dependency the graph does not currently show as blocking.**

eng-04 re-confirmed the validator timeout itself (`batch describe-job-definitions --status ACTIVE`,
2026-08-08): **rev 14 = 14,400 s**; revs 7–13 are 7200 s; revs 1–6 are `timeout: null`. That
independently corroborates PLAT's rev-14 finding from a second read. Gate A cost is **507.5 ms/object,
8 round trips** (MEASURED-LIVE, 10,049 objects / 85 min).

Recomputed against the SHIPPING shard size (DERIVED on that measured rate):

| corpus | objects @25M | Gate A serial | @hw16 | vs 14,400 s |
|---|---|---|---|---|
| reservoir 252.6B — *what the docstring was written for* | 10,103 | 1.42 h | 1.26 h | ✅ 2.8× margin |
| **1.0T, one dataset** | **39,997** | **5.64 h (20,299 s)** | 4.98 h | ❌ **exceeds by 41%** |
| **1.0T stage 1 alone (900B)** | **35,997** | **5.07 h** | 4.48 h | ❌ **exceeds by 27%** |
| 1.0T stage 2 alone (100B) | 4,000 | 0.56 h | 0.50 h | ✅ |

**Break-even is 28,373 objects = 709.4B tokens.** 1.0T is 1.41× over it. **Publishing as the planned
TWO datasets does NOT rescue it — stage 1 alone is 36,000 objects.**

So: **either #10 (B3) ships, or the validator needs a 15th revision at ≥25,000 s, or the corpus goes out
in ≥3 datasets.** B3 is eng-08's task and is already in flight, which is the good news — but its status
changes from "4 h of nice-to-have threading" to **"a gate on publishing 1.0T at all."** I am treating it
as such.

**Why `SHARD_TOKENS` is nonetheless CONFIRMED unchanged at 25,001,984** (B6/#9 closed): halving the
shard would buy 2.8 h; **#10 buys 5.3 h and is needed regardless.** Changing the constant to dodge a
validator cost that #10 removes would pay a **permanent schema price for a temporary code defect** —
and `SHARD_TOKENS` is embedded in `plan_document` (`corpus_build.py:256`), so it moves every `plan_id`.
Constraint 1 still binds and passes (`25,001,984 % 8192 == 0`, verified by execution). Mixture error is
2× better at 25M and negligible either way. **I concur with eng-04's verdict: do not change it.**

**Deviation filed and I am accepting it:** `corpus.py:85-88`'s justification for the constant is now
**circular** — it cites "~10,400 objects" (the 252.6B RESERVOIR, a 4× smaller corpus) and "the 7200 s
timeout" (now 14,400 s). **The two errors do not cancel: the timeout doubled while the object count
quadrupled.** eng-04 is rewriting the docstring only; the value stands and `plan_id` is unaffected.

## Two documentation hazards eng-04 found while grepping (ACTION FOR CEO/AUDIT, not for ENG's code)

1. **`artifacts/impl-plan/pipeline-scale-audit.md` reasons throughout from the WITHDRAWN 50,003,968**
   as though it were the decision (12 sites: `:99-100, 106, 414, 487, 696, 703-704, 759, 882,
   1183-1185, 1196, 1267`), and its `:106` has the **polarity backwards** — it says *"the code has not
   been updated to the 50,003,968 decision"* when in fact **the code was right and the doc was wrong**.
   `orchestrator-findings.md:32-33` records the withdrawal. eng-04 correctly did **not** rewrite a
   historical evidence artifact. **Recommendation: a one-line withdrawal banner at the top**, rather
   than editing 1,196 lines of downstream arithmetic. **CEO's call — it is a docs decision, not ENG's.**
2. **`DATASET-DESIGN-reservoir.md:539` rejects a 10M shard because it "exceeds the 7200 s timeout."**
   That premise is now **false** (14,400 s), so 10M *would* fit it. **10M is still correctly rejected**
   on the other stated ground — it doubles OLMo-core's linear `self.offsets` scan in `__getitem__` —
   which is unaffected. Flagged so nobody reopens 10M because the timeout moved and then re-derives
   the wrong conclusion. **The surviving objection is the trainer hot loop, not the validator.**
   Also do **not** cite that file's `:537` "50M → 4.8% @5% weight" table — `HANDOFF-FINAL-DATASET.md:175`
   calls it **false by ~15×**.

---

# 🔴 ESCALATION E2 — THE REGISTRY ENG IS EDITING IS THE RESERVOIR'S, NOT THIS CORPUS'S

**From eng-04, MEASURED-IN-CODE.** `artifacts/reservoir/corpus-registry.json` — the only registry in
the repo, and the one C3b's "registry route" edits — has 17 rows summing to **252.6B `target_tokens`,
not 1.0T**, and its DCLM row is **30B, not 410B**. **There is no 410B DCLM row anywhere in the repo.**

So the plan's 410B / 252B / 108B bundle figures describe **a corpus whose registry does not exist yet.**
This does not invalidate C3b — the split mechanism and the uniqueness guard are needed either way, and
the ratios hold — but it does mean:

- The **way-counts (DCLM 5 ways at 32 vCPU, FineWeb-Edu 4)** are sized against target tokens that no
  registry row currently declares. They are DERIVED from the report's mix, not read off the registry.
- **Someone must author the 1.0T registry**, and that is a **mix decision inside `FREEZE`** — the CEO's
  and DATA's, not ENG's. ENG can ship the mechanism; ENG cannot invent the rows.
- This compounds the **DCLM repo reconciliation** my predecessor already escalated (open item 1):
  the row points at `HuggingFaceFW/dclm_100BT` (`config: "data"`, **100 FLAT files, no subdirectories**),
  while the free carve exists only in the nested `mlfoundations/dclm-baseline-1.0-parquet`
  (10 × 10 = 100 disjoint dirs). **Different repos, different licenses, different token counts.**
  eng-04 is instructed not to silently repoint it.

**Decision needed from the CEO** (see my final report).

---

# ✅ STREAM 4 PART 1 LANDED — the silent-loss guard, with the trap reproduced by execution

Commit `5004159` on `agent/eng-04/plan-surface`. **Trap 1 is not theoretical — eng-04 executed it.**
Two rows both labelled `dclm`, configs `dirA`/`dirB`:

```
bundles emitted: 2          <- looks correct
   dclm--train  spec_key=dclm-b  config=dirB  tokens=4,975,394,816
   dclm--val    spec_key=dclm-b  config=dirB  tokens=   25,001,984
   tokens IN  = 7,500,595,200
   tokens OUT = 5,000,396,800   LOST = 2,500,198,400  (33.3%)
```

**Both halves fire at once and neither raises.** Row A's 2.5B tokens vanish, AND the survivor carries
`config=dirB` only — so under an N-way split **every child reads the same subdirectory**. The plan looks
internally consistent throughout: bundle count right, ordinals dense, token sums self-consistent. This
is exactly the failure class the golden rule exists for.

**What landed:** `_assert_unique_identities(specs, where=)`, called from **both** `load_registry` (file
path) **and** `plan_document` (in-memory path). eng-04 added the **`key` check on its own initiative**
and I am endorsing it: `_cmd_run` does `{s.key: s for s in load_registry(...)[0]}` (`corpus_build.py:672`)
and `plan_document` looks up `tokens_per_source` by `spec.key` (`:206`), so a duplicate key routes a build
to the **wrong upstream repo** — the plan names one source and the run reads another, silently. Same bug
class one step later. Guarding only the file reader would have been bypassed by the exact use case the
guard exists for (N split rows constructed in memory). 6 new tests, each recomputing — including one that
**measures the loss** rather than asserting a raise, and one proving the repaired form keeps both configs.

---

# ✅ THE PRODUCER/CONSUMER CONTRACT IS PUBLISHED AND FROZEN — the wave's main coupling risk is closed

eng-05 published the keep-list contract to `stream-05-hash-prepass.md` §1 and marked it **FROZEN**
before writing the bulk of its code — which is exactly what the concurrent dispatch needed. eng-06 can
now code against it rather than guessing. **I am treating §1 as the interface of record.**

The design decision that makes this safe: **`KeepFilter` is duck-type compatible with `SeenHashes`** —
same `add_if_new(digest: str) -> bool`. So `dedup_and_decontaminate` is **unchanged**, and eng-06's edit
in `run_bundle` is to pass `seen=`. **`seen=None` preserves today's behaviour exactly**, so nothing in
the pipeline changes until a keep-list is actually supplied. That is a much smaller blast radius than
the plan implied and it de-risks the merge.

Two things in the contract I specifically endorse:
- **`keep.unused > 0` is surfaced, not swallowed** — it is a pass-1/pass-2 disagreement, meaning the
  reader delivered a different document set than the scan saw. It is the only signal the two passes
  diverged, and it is free.
- **The artifact is `keeplists.json`, and eng-05 wrote the reason into the contract itself: NEVER name
  it `manifest.json`.** An agent reading only that file still gets the hazard. Good defensive writing.
- The `.keep64` container **recomputes and rejects** on wrong magic, wrong `hash_bits`, a
  `payload_bytes != n_keys*8` mismatch, a length mismatch, and a non-ascending payload — so a truncated
  download **raises instead of parsing as a smaller keep-list that would silently discard documents.**
  That is the golden rule applied to a new artifact without being told to.

## 🔴 E3 — eng-05 SHARPENED §5.5, AND IT IS WORSE THAN THE PLAN SAYS (MEASURED-IN-CODE)

The plan (§5.5) says a cross-source duplicate's winner "is decided by alphabetical accident." **That
describes the PROPOSED state. Today there is no cross-bundle dedup at all** — `corpus_build.py:475`
calls `dedup_and_decontaminate` with **no `seen=`**, so a fresh `SeenHashes()` is built per bundle
(`corpus_filter.py:302`), and **every copy of a cross-source duplicate survives.**

Consequences I am carrying forward:
1. The pre-pass **introduces cross-bundle dedup for the first time** — it is not a re-ordering of an
   existing behaviour. So "default to today's behaviour" is not literally implementable; the honest
   default is **plan order**, and the source-priority list remains **an unmade decision** (a finding,
   per convention 7). **It is a mix-quality decision — CEO/DATA, not ENG.**
2. **A second, unremarked defect eng-05 found: today's dedup is per-bundle, and bundles are per
   `(source, domain, split)` — so the same text appearing twice in one source under two doc ids, one
   carved to `train` and one to `val`, is NOT deduped.** That is **train/val leakage inside a single
   source, invisible to every current check.** Global dedup removes it. Rate UNVERIFIED; **pass 1
   measures it for free** (it is `lost` on the val bundles). This is a correctness win nobody costed.

## ⚠️ E4 — §5.3's partition table is labelled with the wrong unit (footnote, not a blocker)

eng-05 recomputed `IMPLEMENTATION-PLAN.md` §5.3's partition table: all three rows reproduce at
**85.9 B/key** (the `set[int]` rate), **not** the **16 B/key** its own ⚠️ caveat claims. True 256-way
resident is **0.077 GB/worker** at 1.23B, **0.060 GB** at 0.96B — **5.3× smaller than the table.**

**Blast radius: none, and that is why I am recording it as a footnote.** The table exists to argue 256
partitions fit a worker; they fit by a *wider* margin than claimed. No decision reverses. It is worth
recording only because **the caveat written to prevent exactly this confusion is itself the confused
part**, so the next person sizing a worker from it over-provisions 5×. Grade: DERIVED (arithmetic).

---

# 🔴 ESCALATION E5 — THE DCLM REPO RECONCILIATION IS SETTLED BY MEASUREMENT, AND IT NEEDS A CEO RULING

**This closes my predecessor's open escalation 1.** eng-04 walked **both** trees against the live HF
tree API on 2026-08-08 (read-only; no AWS, no S3, no write — within constraints). **The two repos are
not interchangeable, and the plan's 410B figure presupposes the one the registry does NOT name.**

| | **A. `HuggingFaceFW/dclm_100BT`** — what the registry points at today | **B. `mlfoundations/dclm-baseline-1.0-parquet`** — where the free carve lives |
|---|---|---|
| registry row | `dclm-baseline`, `config: "data"`, **30B** | **not in the registry at all** |
| tree shape | **100 flat files, ZERO directories** | 10 × `global-shard_NN_of_10` → 10 × `local-shard_N_of_10` = **100 disjoint dirs** |
| files | **100** | **27,938** |
| bytes | 316.0 GB | **7.420 TB — 23.5×** |
| **license** | **ODC-BY-1.0** | **CC-BY-4.0** (eng-04 fetched the HF API `cardData` and read it) |
| pool | **114.69B MEASURED** | ~2,693B DERIVED |

**Repo A cannot supply 410B tokens — its entire measured pool is 114.69B.** So a 410B DCLM draw is only
possible from repo B (or from `mlfoundations/dclm-baseline-1.0`, which is `.jsonl.zst` and which
`corpus_read` **refuses** — `READABLE_FORMATS` is `{parquet, json.gz}`). **This is not a preference
between two equivalent sources; it is a prerequisite of the mix as designed.**

**⚠️ The licenses differ (ODC-BY-1.0 vs CC-BY-4.0).** That is a DATA/legal question, not ENG's, and it
is a second reason the swap cannot be made by an engineer quietly editing a row.

## ✅ Two of the task-28 briefing's own open questions are now CLOSED by measurement

1. **Briefing §5.2 worried that "N equal `target_tokens` rows will not produce N equal children."**
   **It does not apply to this carve.** All 10 `global-shard` dirs walked: **max/min = 1.0025, i.e.
   0.25% skew** (2,784–2,800 files, 740.98–742.82 GB each). N equal rows give N equal children to
   within a quarter of a percent. Caveat recorded by eng-04 and I endorse it: that is **bytes, not
   tokens** — a tokens/byte skew would not show here, though these are random-assigned shards of one
   crawl so a systematic skew would be surprising.
2. **Briefing §5.4 — "whether `hf_files` pagination completes on a 100-way listing is UNTESTED."**
   **It completes.** eng-04 ran the real `corpus_build.hf_files` against the live API: 2,790 files from
   one `global-shard` (>the 1,000-per-page limit) came back across **3 pages with zero duplicates and
   zero truncation**, in 6.5 s. The briefing's concern was its own fetch tool truncating, not our code.

**Corroboration worth noting:** eng-04's independent walk returned **27,938 files**, which reproduces
`IMPLEMENTATION-PLAN.md` §4.1's 27,938 **exactly** — derived by a different agent by a different route.
Two independent walks agreeing on a 5-digit count is strong evidence the tree is what both think it is.

**Honesty note I am carrying up:** eng-04's DERIVED pool for repo B (~2,693B) is **28% below** the
plan's independently-DERIVED ~3,764B. eng-04 flagged its own assumption (equal parquet compression
between a 100BT subsample and the full baseline) and said explicitly: *"Do not use either figure for a
mix decision."* **Either way the pool clears a 410B draw by >6×, which is all the split sizing needs.**
I agree with that scoping — the disagreement does not block C3b.

---

# ✅ STREAM 5 COMPLETE (A2a / #22 producer) — VERIFIED BY ENG-EXEC, NOT TAKEN ON REPORT

Commit `d1d9c8f` on `agent/eng-05/hash-prepass`. ⚠️ The task notification carried *"claude-sonnet-5[1m]
(the safety classifier) was unavailable when reviewing this subagent's work — carefully verify."*
**So I verified it myself rather than accepting the report.** All four checks below are mine:

| check | result |
|---|---|
| **test count** | **`1244 passed` in 28.18 s — I ran it.** +30 over baseline 1214, **zero regressions** |
| **scope** | `git diff --name-only` = exactly 2 files: `corpus_filter.py` (+574), `tests/test_corpus_keeplist.py` (+553, new). **Nothing outside its territory.** `corpus_build.py` and `test_corpus_filter.py` **untouched** — confirmed by empty `git diff --stat` on both |
| **not pushed** | `git branch -r --contains HEAD` → empty. Local only ✅ |
| **hard constraints** | grepped the diff for `manifest.json`/`put_object`/`s3.put`/`submit_job`/`boto3`/bucket names. **Three hits, all benign and all in the right direction:** two are a docstring warning that the artifact is named `keeplists.json` and *never* `manifest.json` because it would fire the EventBridge rule, and the third is a **test asserting no bundle path ends in `manifest.json`**. No S3 call, no Batch call, no network. ✅ |

**No existing behaviour changed.** `dedup_and_decontaminate`, `SeenHashes`, `content_hash` and
`DecontaminationIndex` are all untouched, and `seen=None` preserves today's path exactly — so this merges
with a blast radius of zero until a keep-list is actually supplied. That is the property that makes the
merge safe, and it was eng-05's own design choice.

## Numbers moved, with grades (eng-05's, and I am recording the ones that supersede the plan)

| | value | grade |
|---|---|---|
| `SeenHashes` real cost | **85.95 B/entry** — reproduces the docstring's 85.9 to **0.06%** | MEASURED |
| `HashScan`/`KeepFilter` | **8.43 / 8.13 B/key — 10.20× denser** | MEASURED |
| **DCLM 325M docs** | **27.93 GB → 2.64 GB**: from **186% of a 15.03 GB container to 18%** | DERIVED from MEASURED |
| **§5.3's "2.60 GB / 7.68 GB" is SUPERSEDED** | **→ 2.74 / 8.09 GB.** The plan assumed an ideal 8.000 B/key; CPython's ~1/16 append over-allocation makes it 8.43. **+5.4%. Both still fit; no decision reopens** | MEASURED |
| partition balance @2M keys | max/mean **1.0383**, **256/256 occupied** | MEASURED |
| pass-1 hashing | 102,478 docs/s/core = 2.60 core-h global | MEASURED |
| §5.3's ~0.2 h pass-1 wall-clock | **stands, and eng-05 says plainly it did NOT verify it** — the pass is read-bound, not hash-bound | UNVERIFIED (declared) |

I specifically credit the **+5.4% self-correction**: eng-05 measured its own structure to be worse than
the plan predicted and reported it, rather than quoting the plan's friendlier 8.000 B/key. That is the
behaviour convention 8 asks for.

## 🔴 E6 — THE SOURCE-PRIORITY DEFAULT IS A MIX DECISION WEARING AN IMPLEMENTATION DETAIL'S CLOTHES

**CEO decision needed.** eng-05 implemented priority as a parameter and defaulted it to
`sorted(bundle_ids)` = plan order, with the artifact self-labelling `"priority_basis": "plan-order"`.
**But that default makes `dclm` beat every other drawn source purely because "d" precedes "f"/"p"/"s"/"u".**
So a `dclm` × `fineweb-edu` duplicate — **near-certain, both are Common Crawl derivatives** — survives
labelled `web-diverse` instead of `edu-web`. The label is consumer-visible and inside `manifest_sha256`.

eng-05's suggested ordering (curated → targeted web → code/forum → bulk web → synthetic last) is
**UNVERIFIED against any measurement** and it says so. **I am not letting an engineer pick this.** It
decides which source's copy of a duplicated document ends up in the corpus, and it is exactly the class
of thing convention 7 says to surface rather than quietly default.

## Two more findings from eng-05 that I am carrying up

- **§5.6's missing receipt block now has a second victim.** `KeepFilter.unused > 0` is the **only**
  signal that pass 1 and pass 2 saw different inputs. eng-06 owns the receipt fix — **I have relayed
  this to it** so both blocks land together rather than needing a second pass.
- **Train/val leakage confirmed as a real, present defect** (E3 above), now with the mechanism named:
  `carve` routes on a hash of the **id**, not the text, so identical text under two ids lands in **both**
  `--train` and `--val`. Invisible to every current check. The pre-pass removes it as a side effect.

**Deliberately not done, and correctly so:** the pass-1 driver (fanning 256 workers over staged text) —
it needs eng-07's `_reader_for`, and `resolve_keep_lists(partitions_subset=)` is the seam left for it.
eng-05 stopped at its territory boundary instead of reaching into another agent's function. **This is a
real remaining gap in A2a, not a completed item: A2a's data structure is done; A2a's driver is not.**

---

# STREAM 7 (C1/#21) — in flight, and it has CLOSED the column trap and OPENED a mix ambiguity

## ✅ The §4.2 column trap is ALREADY CLOSED — eng-07 measured it rather than assuming

I asked eng-07 to verify, not assume, and it comes out **GREEN by a stronger mechanism than the trap
needs** (all MEASURED-IN-CODE):

- All four FinePhrase registry rows carry `"text_column": "rollout_results.list.element.text"` — the
  exact `path_in_schema`.
- `corpus_read.read_parquet_documents:479` calls `_resolve_leaf(...)` **before reading a single row**,
  and `_resolve_leaf` (`corpus_read.py:106-119`) is **exact-match-or-raise**. **There is no
  `.names.index("text")` anywhere on the read path** — which is the specific mechanism §4.2 warns about.
- `_compile_walk` re-checks the leaf against the arrow schema and raises `ReadError` when a path "does
  not descend" — precisely the `rollout_results.text` zero-column near-miss.
- Existing tests already pin all three near-miss spellings (`tests/test_corpus_read.py:173-176, :199`).

eng-07 is still adding one test — and the reason is the right one: today's tests pin **the reader's
behaviour given a spec**, not that **the shipped registry rows actually carry the safe value**. That
gap is exactly the "verified code path production does not execute" shape this project keeps hitting.

## ✅ The gap itself re-confirmed, with call-site counts (MEASURED-IN-CODE)

`keeps_id` → **1** caller, and it is a *reporting* function (`ingest_reservoir._partition_report:743`,
builds a JSON audit dict). `partition_of` → **0** production callers. `IdSet.contains` → **0 anywhere
in the repo**. `_reader_for` filters **nothing** on id. Plan §0 blocker 2 confirmed exactly.

## 🔴 E7 — "FinePhrase, one partition" IS AMBIGUOUS, AND THE TWO READINGS DIFFER BY 3.3×

**Decision needed.** `FINAL-DATASET-REPORT.md:88` says *"FinePhrase, one partition | 4% | 36.0B |
123.3B | 0.29"*. That admits two readings and they are **not the same corpus**:

| reading | drawn | pool | epochs @36.0B |
|---|---|---|---|
| **(A) one CONFIG's quarter** — e.g. `faq` only, keeping `partition_of(id)==0` | 1 quarter of 1 config | 148.54B × 0.2486 = **36.9B** DERIVED | **0.98 — a full epoch, ~zero headroom** |
| **(B) all four configs, each keeping its OWN quarter** | 4 disjoint quarters | 478.15B × 0.25 = **119.5B** DERIVED | **0.30** |

**The report's own 123.3B and 0.29 match reading (B)** — 119.5B DERIVED vs 123.3B stated is a **3.1%**
gap, while reading (A) is off by **3.3×**. So (B) is almost certainly intended, but it is inferred from
the pool column, not stated.

**Reading (A) would be actively dangerous:** at 0.98 epochs the `_FILTER_HEADROOM` 1.5 over-read has
nowhere to go, and MIN_DOC_TOKENS attrition (3.4–12.6% measured for FinePhrase's short rewrites) leaves
the bundle **short** — which surfaces as `verify` failing on unfilled refs *after* the full billable
tokenize.

⚠️ **And 123.3B has UNVERIFIED provenance** — eng-07 could not find it in `artifacts/` (`grep -rn "123.3"`
→ no hit). The nearest recomputation from registry pools is **119.5B DERIVED**. Not a blocker; flagged
so nobody treats 123.3B as recomputable.

**✅ Why this does not block the code:** eng-07 deliberately implemented **reading-agnostic** — it
applies `keeps_id(spec.config, doc.id)` **per FinePhrase config row**, so one row drawn keeps its
quarter with no sibling to overlap, and four rows drawn give four disjoint quarters with no document
twice. **Which rows are drawn stays a `target_tokens` question — eng-04's file and a mix decision.**
That is the correct boundary and eng-07 held it.

## §4.3 anti-join — reported, NOT implemented, correctly

`sample-100BT ⊂ sample-350BT` and `sample-350BT` is FinePhrase's exact parent, so **100%** of a
FineWeb-Edu draw has a synthetic sibling — **the registry's own `fineweb-edu` trap text says so**. The
primitive exists and is unused (`IdSet`, zero `contains` callers). **eng-07 correctly did not touch it:
CEO-level mix question.** Note the scoping, because it is easy to conflate: **the id partition makes the
four SYNTHETIC configs disjoint from each other; it does nothing about synthetic-vs-edu-web overlap.
Two different defects — one fixed here, the other still open with no owner.**

---

# STREAM 6 — B7 LANDED, and eng-06 RETRACTED ITS OWN WRONG FINDING IN PLACE

## ✅ T2 / B7 / #29 landed

Sink at `corpus_build.py:461` now computes the digest **before** the put and declares it:
```python
digest = s3.put_bytes_verified(bucket, key, payload)
digests[ref.path] = (digest, len(payload))
```
`put_bytes_verified` added to the `s3` Protocol, `Boto3S3`, and `FakeS3`. 4 new tests, **all
recomputing** — including test 4, a **control** asserting a plain `put` declares nothing, *"without it,
tests 1–2 would pass even if `declared_checksum` returned a value unconditionally."* That is the golden
rule applied to its own test suite, unprompted.

### ⚠️ A pre-existing test was silently decoupled, and this generalises

`test_the_receipt_is_written_only_after_its_shards_verify` (`tests/test_corpus_build.py:267`) overrode
`FakeS3.put` to simulate a dropped upload. Once the sink moved to `put_bytes_verified`, **the override
dropped nothing** — the fixture no longer reached the code it exists to break, and **pytest reported it
green while the assertion it guards had actually stopped being exercised.** eng-06 re-pointed it.

**The general lesson, which I am carrying up because it applies to eng-08's threading work too:** *any
test that intercepts a specific S3 method is coupled to the write path's choice of method, and a silent
decoupling looks exactly like a passing test.* A green suite is not evidence a fixture still bites.

### B7 correctly NOT claimed as a win
eng-06 reports **critical path moved 0.00 h** and says so explicitly: B7 permits deleting `VD1`,
deleting `VD1` exposes `GA1`, and at `GA1`'s unthreaded 5.08 h the path is 24.90 h either way. **It pays
only once B3 lands.** It did not do B3, did not depend on it, and did not retire `verify --deep` (job-def
change, not code, not ENG's). Shard is **1.86%** of the 5 GiB single-PUT limit, so the guard can only
fire on a misconfiguration — and fires on shard 1, not shard 400.

**ACTION CARRIED TO PHASE 2 SMOKE TEST:** the 2026-08-01 `BadDigest` evidence covers the **file-shaped**
path; the bytes-shaped call is the same `put_object` with the same header, so the argument is **by
identity of the API call, not a second measurement**. A deliberate digest corruption on the bytes-shaped
path must be asserted against real S3 in the smoke test. eng-06 cannot run it (forbidden from S3) and
correctly did not pretend otherwise.

## ✅ eng-06 RETRACTED its finding F1, in place, with the reasoning

F1 had argued a keep-list keyed on the content hash **cannot** dedup — an immutable membership test
returns the same answer for both copies, so exact dedup would silently become a no-op — and concluded
the key had to be `(source, id)` and `dedup_and_decontaminate`'s signature had to change.

**It read eng-05's landed code (`git show d1d9c8f:...`) and retracted.** `KeepFilter` splits the two
things F1 conflated: the **KeepList is immutable** (the shared artifact pass 1 froze — what makes
execution order unable to reach the output), while the **used-bitmap is per-instance mutable**. So the
second copy finds its key marked and returns `False`. *"Determinism does not require total statelessness;
it requires no SHARED state."* **Blast radius: zero code** — the wiring had not been written, and the
correct wiring is smaller than what F1 planned.

It kept the retraction visible rather than deleting it, because the failure mode is real and *"someone
simplifying away the bitmap would hit it."* Convention 8, executed properly. **This is the single best
argument for the concurrent-dispatch-plus-published-contract design: eng-06 caught its own error by
reading eng-05's actual code, which only existed because eng-05 froze and published the contract early.**

## A cross-check the contract hands us for free (MEASURED-IN-CODE)

`hits + repeats + misses == filter.seen`; `hits == filter.kept + filter.contaminated`;
`repeats + misses == filter.duplicates`. This is a **cross-check between two independently maintained
counter sets** — strictly stronger than either block's internal identity, which a single wrong `+= 1`
would satisfy. It is going into the verifier.

## Receipt schema: v2, with v1 kept readable — and one open item

`verify_receipt` **short-circuits on an unknown `schema_version`** (`corpus_receipt.py:523-535`), so
bumping without adding v1 to `READABLE_RECEIPT_SCHEMAS` would make **every existing receipt
unverifiable and break resume.** Both go in the set. The bump is justified on interpretation, not shape:
under v1 an absent filter block means *the schema had no slot*; under v2 it means *the producer chose not
to record*. That distinction is the whole auditability claim.

**⚠️ OPEN ITEM eng-06 flagged rather than deciding silently — three denominators, two blocks shipped:**
`filter` (documents entering dedup) ✅ and `keep` (probes against the keep-list key space) ✅ ship;
**`length` (documents surviving dedup) is deliberately EXCLUDED.** Its reasoning: `length` is
`corpus_read.FilterStats`, a different pass with a third denominator, and unlike the other two it is
**not unrecoverable** — `run_bundle` already returns it. §5.6's argument for persisting is "these numbers
exist only in CloudWatch," which applies to `filter` and `keep` but not `length`. **I accept this call**
— adding a third denominator in the same commit is the precise shape of the `category_attrition`
mistake. Recorded as an open item, neither silently included nor silently omitted.

---

# ✅ STREAM 4 COMPLETE (C3b/#28 + B6/#9) — VERIFIED BY ENG-EXEC

3 commits on `agent/eng-04/plan-surface`. **My own verification, not its report:**

| check | result |
|---|---|
| tests | **`1227 passed` — I ran it.** +13 over 1214, zero regressions |
| scope | `corpus.py`, `corpus_build.py`, `tests/test_corpus.py`, `tests/test_corpus_build.py`. **No other agent's function touched** — grep for `def run_bundle|_reader_for|sink` in its diff returns nothing |
| **registry NOT edited** | `git diff --stat` on `corpus-registry.json` → **empty**, exactly as it claimed. It shipped the *mechanism* and refused to author the mix |
| `SHARD_TOKENS` | still `3052 * SEQ_LEN`. Value unchanged ✅ |
| constraints | no `manifest.json` / `put_object` / `submit_job` / `s3.put` / bucket names in the diff. Not pushed ✅ |

**`split_source_rows` verified end-to-end on real DCLM paths: 5 ways → 9.75 h at 32 vCPU against the
9.96 h floor, 5/5 distinct configs, 16,315 ordinals unique and dense.**

## eng-04 corrected itself in place, twice — both worth carrying

1. **It first called FineWeb-Edu's 4-way split "infeasible, needs code," then fixed it.** Accurate
   version: 4 ways is infeasible **one-dir-per-row** (largest CC-MAIN dir is 24.8B; a row needs 63B),
   but **16 ways at 8 vCPU = 7.53 h, one dir per row, zero code.**
   **Recommended wave shape: DCLM 5-way @32 vCPU + FineWeb-Edu 16-way @8 vCPU = 288 vCPU, fits 384.**
   ⚠️ **DCLM 10-way + FWE 16-way = 448 vCPU and EXCEEDS the 384 cap.** Do not stack both at maximum.
2. **🔴 E8 — FineWeb-Edu has the SAME repo mismatch as DCLM, and it is in NO plan document.**
   Registry says `sample/100BT`, pool **100.24B**; the report asks **252B** from a stated 1,583.1B.
   eng-04 derived the full `data/` pool independently at **1,583.0B — a 0.01% match**, which is strong
   evidence the report was costed against `data/` while the registry names `sample/100BT`.
   **So BOTH web pillars point at upstreams too small for their draws.** This is a new finding, not a
   restatement of the DCLM one.

## Why eng-04 deliberately did not edit the registry — I endorse this

Three reasons, all correct: it is the **reservoir's 252.6B mix** with a 30B DCLM row on a *different*
upstream; **no registry exists yet for the 1.0T corpus**; and editing it **would change every reservoir
`plan_id`**. The mechanism ships; splitting is one call once the 1.0T registry is authored. **Authoring
that registry is a mix decision inside FREEZE — CEO/DATA, not ENG.**

---

# ✅ STREAM 7 COMPLETE (C1/#21) — VERIFIED BY ENG-EXEC

Commit `6830af4` on `agent/eng-07/finephrase-partition`.

| check | result |
|---|---|
| tests | **`1221 passed` — I ran it.** +7, zero regressions |
| scope | `corpus_build.py` (+64, 3 hunks) and `tests/test_corpus_build.py` (+259) only. The **only** `def` it added is `_finephrase_format` — it did not touch `run_bundle`, `_reader_for`'s neighbours, `load_registry` or `plan_document` |
| **`_CHARS_PER_TOKEN` left at 6.0** | ✅ It was told that change is WITHDRAWN and points the opposite way. It obeyed |
| constraints | clean; not pushed ✅ |

**Both halves shipped together, as required** (partition + budget division — separately, every bundle
finishes and then fails `verify` on unfilled refs).

Two design calls I specifically endorse:
- **`_finephrase_format` keys on `spec.repo`, NOT `source_label`.** Its reasoning: the label is a naming
  decision a mix edit rewrites, and **a partition that silently stops applying is an invisible 4×
  over-exposure.** That is the right failure-mode analysis.
- **`seen_chars` is charged BEFORE the drop** — the budget counts characters *read*; counting survivors
  would apply the correction twice and read 16×.

**Regressions deliberately induced to prove the tests bite** (MEASURED): removing the drop → **3 tests
fail**; removing the budget division → **1 fails at ratio 0.243**, reproducing the predicted ¼
under-delivery. That is recompute-never-trust done properly — the tests were shown to fail, not just to pass.

## 🔴 E9 — NEW DEFECT: the pool guard is now fail-open by 4×

`CorpusSpec.__post_init__` compares `target_tokens` against the **undivided** `pool_tokens`, but a
FinePhrase row now reaches only a quarter of its pool. **Silent band 65–111B per row.** Zero impact at
today's 15.0B targets; **bites above ~21.74B** (`table`). **Fix belongs to eng-04's territory** (registry
`pool_tokens` ÷ 4, one line per row) — eng-04 has now completed, so this is **unowned and I am carrying
it as an open item.**

## E7 sharpened by eng-07's final read — one reading is not merely risky, it is INFEASIBLE

All-four-at-9.0B gives 0.24–0.41 epochs and a 119.5B union pool, matching the report's 123.3B/0.29.
**One-config-at-36.0B is impossible for `math` (1.52 epochs) and `table` (1.66)** — the plan could not
fill its shards. eng-07's code is correct under both readings, but **the ambiguity must be resolved
before FREEZE.** 123.3B's provenance remains UNVERIFIED (not in `artifacts/`; recompute gives 119.5B).

---

# MERGE LOG (merge order 4 → 7 → 6 → 5 → 8, all local, nothing pushed)

| # | stream | branch | result | tests after |
|---|---|---|---|---|
| 1 | 4 — plan surface | `agent/eng-04/plan-surface` | clean, `ort` | **1227** ✅ |
| 2 | 7 — FinePhrase partition | `agent/eng-07/finephrase-partition` | **auto-merged `corpus_build.py` + `test_corpus_build.py`, ZERO conflicts** | **1234** ✅ |

**The counts are exactly additive: 1227 + 7 = 1234, and 1214 + 13 + 7 = 1234.** No test was lost,
displaced, or silently overwritten by the merge.

**⚠️ This is the convention paying for itself, and it is worth stating plainly.** Streams 4 and 7 both
edit `corpus_build.py` — the file the graph calls *"the hottest in the plan," 5 items, 3 concurrent
editors this wave.* They auto-merged with **zero conflicts** because they own **different functions**
(`load_registry`/`plan_document` vs `_reader_for`) rather than different tasks in one file. Convention 1
(*one agent owns one FUNCTION, never one file*) is the reason, and D2 — which discovered stream 4 was a
*third* editor of that file and forced me to name all three owners to each other — is what kept it true.
Had I dispatched by file, this merge would have been a three-way rewrite of the plan schema on the
critical path.

---

# ✅ STREAM 8 COMPLETE (B3/#10, B1/#23, B2) — VERIFIED BY ENG-EXEC. **B3 CLEARS THE TIMEOUT.**

4 commits on `agent/eng-08/parallel-fixes`. My verification:

| check | result |
|---|---|
| tests | **`1227 passed` — I ran it.** +13, zero regressions |
| **`s3.py` collision with eng-06's B7** | **NONE.** Grepped its `s3.py` diff for `def put`/`put_object`/`ChecksumSHA256` → **empty**. Pool config only, exactly as scoped |
| `corpus_build.py` / `corpus_filter.py` | **untouched** — empty `git diff --stat`. Stayed out of all three other agents' territory |
| constraints | no `manifest.json`/`submit_job`/job-def registration in the diff. Not pushed ✅ |
| its key safety claim | **VERIFIED MYSELF:** `check_workers` defaults to `head_workers` at `validate.py:424` **and** `:2520-2521` |

## 🟢 THE HEADLINE — B3 RESOLVES E1, AND IT WAS THE BINDING CONSTRAINT

**MEASURED** (real `CHECKS`, 100 objects, 10 ms simulated latency): 8.27 s serial → **0.60 s @16
(13.77×)**. **Round-trip count identical at every worker count: 6.00/object** — concurrency moves
waiting, not work. That last figure is the one that matters: a "speedup" that changed the call count
would be a correctness bug, and eng-08 measured that it does not.

**DERIVED at 40,001 objects** from the plan's MEASURED 507.5 ms/object:

| configuration | Gate A | vs 14,400 s |
|---|---|---|
| serial, 8 calls | **5.64 h** | ❌ |
| **rev 14 as it stands today** (head-workers 16, profile checks still serial) | **4.28 h** | ❌ **7% OVER** |
| **with B3** | **0.36 h** | ✅ **~9% of budget** |

**This independently reproduces eng-04's 5.64 h / 4.98 h from a *different* call apportionment** — two
agents, two routes, same conclusion. **E1 is resolved in code: #10 has landed.** The binding term is now
**promote** (AUDIT: 3.3–5.9 h), not Gate A.

**And rev 14 needs NO flag change** — `--check-workers` defaults to `--head-workers`, which rev 14
already passes. eng-08 called out why that default matters: *a knob defaulting to 1 would have left the
fan-out off in production with no error to notice.* That is the CLAUDE.md families-dir failure class
exactly, and it designed it out.

At 4 vCPU the work is **latency-bound (0.3% CPU, MEASURED live)**, so 16 is right; prefetch is batched
at 4× workers ≈ 4 MB in flight, versus **2.6 GB** had it warmed all 40,001 objects into an 8,192 MB
container. Good restraint — the naive version OOMs the container.

## eng-08 filed two corrections AGAINST ITS OWN BRIEF, both with evidence. Both accepted.

1. **"Raise `max_pool_connections` FIRST or step 2 underdelivers" — the Gate A CLI path was ALREADY
   sized** (`validate.py:2476-2492`, pre-existing). **Step 1's marginal gain there is zero.** It moved
   the knob onto `Boto3S3.default()` instead, which is what was genuinely missing. My brief asserted an
   ordering that the code had already satisfied; eng-08 checked rather than complied.
2. **"Drop the redundant HEAD (8→7)" — already done.** Its spy measures **6.00 calls/object**; 6 +
   validate's own HEAD = 7 = the plan's own post-fix measurement. Nothing to do.

## 🔴 E10 — B1 FOUND A SECOND UNDECLARED DEPENDENCY, AND THE CODE HAD ALREADY NOTICED

`pyarrow` was undeclared too — **and `corpus_read.py:404` says "production resolved pyarrow 25.0.0
unpinned (`pyproject.toml:29`)", citing a line that declares numpy.** Someone wrote down the hazard
against the wrong line and nobody acted. **`pyarrow` is the package that segfaulted the live array job.**
Now bounded `>=24,<26`; `tokenizers>=0.21,<0.23`. eng-08 stated the bound as **reproducibility, not** a
claim that 0.21–0.22 are byte-identical — *"nobody has run that differential"* (convention 7, correctly
applied). The test recomputes imports from **every module's AST at any scope**, because
`corpus_build.py:631` imports inside a function — a top-level-only scan would have missed the very
import B1 exists to fix.

## Test integrity — eng-08 mutation-verified its own work, and one test refuted it

- Restoring the hardcoded `"<|"` makes the new B2 test **fail with the exact silent-no-op symptom.**
- It replaced an existing **source-substring** assertion in the CLI pool test that *"would have passed
  unchanged while `--check-workers` sized the pool to a quarter of the concurrency in flight."* That is
  the decoration-test class this repo's golden rule targets, found and removed unprompted.
- **Its duplicate-entry test REFUTED ITS OWN EXPECTATION** — the serial path re-reads a duplicate's
  decode windows (20 vs 15); it corrected the assertion from equality to a bound, in place.

**B2 cost measured head-to-head: 0.311 → 0.382 µs/call = 24 s over 340M documents = 0.07%**, and the
derived guard is *longer* than `"<|"`, so the fast path is now **strictly more selective**. The
`len(...) == 1` test is deleted — it fails for a *correct* two-entry table.

**Scope note:** 11 files, +947/−47, two outside its declared four — `profiles/base.py` (the
`check_workers` field) and `validate.py` (flag + call chain). Both are unavoidable to thread Gate A, and
I verified **no other stream is in either file.** Acceptable.

---

# ✅ STREAM 6 COMPLETE (A2b + B7 + FilterStats receipt) — VERIFIED BY ENG-EXEC

| check | result |
|---|---|
| tests | **`1273 passed` — I ran it.** (1214 → 1244 with eng-05 cherry-picked → 1273 with its own +29) |
| commit attribution | `0700b7e` = **eng-05's `d1d9c8f`, cherry-picked — NOT eng-06's work.** `5118308` = eng-06's, all three tasks |
| **overlap with eng-05** | **NONE.** ⚠️ My first grep said "1" and I nearly recorded a collision — it was a **substring match on `tests/test_corpus_build.py`**. Re-checked with `grep -x`: `corpus_filter.py` is **not** in eng-06's file list. **My error, corrected before it reached a report** |
| constraints | clean; not pushed ✅ |

Cherry-picking eng-05 was the right call and eng-06 gave the right reason: `KeepFilter` does not exist
on `f5a4017`, so without it the integration would have been **asserted rather than tested.**

## 🔴 E11 — eng-06 REFUTED AN OBLIGATION *I* RELAYED, AND IT IS RIGHT. MY ERROR.

I passed eng-05's `unused > 0` alarm to eng-06 as a new obligation. **eng-06 implemented it, MEASURED
it, and found it fires on normal operation.** I have verified the mechanism myself:

- eng-05's docstring (`corpus_filter.py:817-822`) says *"Zero is the expected value."*
- **`corpus_pack.pack` stops when its planned shards are full and does NOT drain the document
  iterator.** MEASURED by eng-06: offered 200,015 documents, pulled **50,264 (25.1%)**.
- **`run_bundle` passes `partial_source=True` for exactly this reason** (`corpus_build.py:610-617`,
  which I read) — `_reader_for` **over-delivers by design**, drawing 252B from a 1,094B pool.
- **Therefore `unused > 0` is the EXPECTED case, not the exception.**

**Blast radius had it shipped as a gate:** `run_bundle` calls `verify_receipt` and raises **before**
writing the receipt, so it would fail a bundle at end-of-run **after its full billable read + tokenize +
upload**, and resume would rebuild it forever. **That is the same pipeline position and the same shape
as the `_drain_surplus` bug that killed 25 of 27 bundles.** eng-06's own two-bundle test tripped it at
`unused=1` of 260 keys.

`unused` conflates **(a)** the passes read different inputs — the real alarm — with **(b)** pass 2 filled
its shards and stopped — normal. **The receipt cannot separate them**; that needs pass 1's `scanned` from
`keeplists.json`. eng-06 still **records** `unused` and replaced the gate with **`hits > keys`** — the one
direction an early stop cannot produce, since each hit consumes a distinct key and the used-bitmap
prevents a second. Verified at `corpus_receipt.py:1090`. It also added a **re-derivation check**
(`unused == keys - hits`), on the reasoning that *"a value that does not re-derive is a corrupted alarm —
a failure hiding a failure."*

**Owning this: I relayed an obligation without checking whether its premise held in the pipeline it
would run in.** eng-05 stated the property in good faith about its own data structure; the failure was
that nobody had checked it against `pack`'s early-stop behaviour, and I was the one positioned to ask.
**ACTION: eng-05's `unused` docstring must be amended** — it is not eng-06's file, and eng-05 has
completed, so this is unowned and I am carrying it.

## The three tasks

- **T1 A2b** — `keep_list=` threaded as `seen=`; eng-05's frozen contract held **exactly**;
  `corpus_filter.py` untouched. **`keep_list=None` proven byte-identical by comparing receipt digests
  AND every uploaded byte** — not by inspection. `_keep_filter_for` refuses another bundle's list, a
  `KeepFilter` where a `KeepList` belongs, and a mid-build mutation. **Scope limit documented rather
  than glossed:** `.keep64` carries no bundle id, so that check catches **mislabelling, not a wrong
  object read confidently.**
- **T2 B7** — as verified earlier; **no critical-path claim (0.00 h until B3)**, `verify --deep` not
  retired.
- **T3** — `FilterRecord` + `KeepRecord`, schema **v2 with v1 kept readable**. **Absent parses as
  `None`, never zeros** — the distinction between "no slot" and "chose not to record". Verifier
  recomputes the filter identity plus **three cross-block relations between two independently
  maintained counter sets**.

## Test integrity — the strongest of the wave

**Mutation-tested:** reverting the sink to plain `put` fails **3** tests; ignoring `keep_list` fails
**2**; dropping the filter block fails **3**. And it found and fixed **two defective tests of its own**
(one asserted nothing; one used a degenerate fixture where dedup gave one bundle 150 keys and the other
0) plus **one pre-existing test its change silently decoupled**.

**Process honesty:** it wiped ~350 uncommitted lines with a careless `git checkout` on a dirty file,
recovered them, and **recorded it as a process lesson** rather than quietly re-doing the work.

---

# ✅ WAVE 0 COMPLETE — ALL 5 STREAMS MERGED, 1214 → 1306, ZERO CONFLICTS

| # | stream | branch | merge | tests after |
|---|---|---|---|---|
| 1 | 4 — plan surface (C3b, B6) | `agent/eng-04/plan-surface` | clean | **1227** |
| 2 | 7 — FinePhrase partition (C1) | `agent/eng-07/finephrase-partition` | **auto-merged `corpus_build.py`, 0 conflicts** | **1234** |
| 3 | 6 — keep-list + B7 + receipt | `agent/eng-06/keep-list` | clean (carried eng-05) | **1293** |
| 4 | 5 — hash pre-pass (A2a) | `agent/eng-05/hash-prepass` | no-op, records authorship | **1293** |
| 5 | 8 — B3, B1, B2 | `agent/eng-08/parallel-fixes` | **auto-merged `s3.py`, 0 conflicts** | **1306** ✅ |

**+92 tests over the 1214 baseline. Not one merge conflict across the whole wave**, on a file the graph
calls the hottest in the plan with three concurrent editors, and on `s3.py` with two.

## Post-merge integration verified (not assumed)

- **`s3.py` carries BOTH agents' work**: `put_bytes_verified` ×3 (Protocol + `Boto3S3` + `FakeS3`, eng-06)
  **and** the `max_pool_connections` knob on `Boto3S3.default` (eng-08). The two agents edited one file
  and never collided because they owned different methods.
- **`corpus_build.py` carries all three owners' symbols** — 9 hits across `_assert_unique_identities`
  (eng-04), `_finephrase_format` (eng-07), `put_bytes_verified` call (eng-06).
- **Nothing pushed:** `## final-dataset...origin/final-dataset [ahead 16]`. All 16 commits local.
- **No `manifest.json` exists anywhere in the tree** (`find` → empty). No S3 write, no Batch submit,
  no job-def registration occurred in this wave.

## The methodological result, stated once

**Convention 1 (one agent owns one FUNCTION, never one file) is the whole reason this merged cleanly**,
and ruling **D2** — which caught that stream 4 was a *third* editor of `corpus_build.py` and forced me to
name all three owners to each other in their prompts — is what kept it true under a 5-way concurrent
dispatch. Dispatching by file would have produced a three-way rewrite of the plan schema on the critical
path.

**Convention 5 (write to disk first, incrementally) held under two classifier outages**: every stream
created its findings file as its first tool call, and all 5 files survive with 2,000+ lines of graded
evidence. Compare the prior wave: 7 agents, 2.9 MB of reasoning, 4 files.

**Four of five agents corrected themselves or their brief in place** — eng-04 twice, eng-05 on its own
memory measurement, eng-06 retracting F1 and then refuting an obligation *I* relayed, eng-08 filing two
corrections against its brief. That is convention 8 working, and it caught real defects: the `unused`
gate would have failed every bundle at end-of-run after full billable work.

---

# OPEN ITEMS ENG CANNOT CLOSE — for the CEO

## Decisions (ENG has shipped the mechanism; the choice is not ENG's)

| # | decision | why it is not ENG's | ENG's recommendation |
|---|---|---|---|
| **D-a** | **Author the 1.0T registry.** No registry for this corpus exists; the only one is the reservoir's 252.6B | It IS the mix. Inside FREEZE | `split_source_rows` makes it one call once authored |
| **D-b** | **Repoint DCLM** → `mlfoundations/dclm-baseline-1.0-parquet` | Forced by arithmetic (repo A's whole pool is 114.69B vs a 410B draw) but **changes the licence string ODC-BY-1.0 → CC-BY-4.0** | Repoint, with legal sign-off on the licence |
| **D-c** | **Repoint FineWeb-Edu** `sample/100BT` → `data/` | Same forcing (100.24B pool vs 252B draw). **New finding — in no plan document** | Repoint |
| **D-d** | **Label scheme `dclm-01`…`dclm-NN`** | Permanent, consumer-visible, inside `manifest_sha256`, unbackfillable | Accept + list the labels in the README `sources` |
| **D-e** | **Source-priority list for cross-source dedup** | Decides which source's copy of a duplicate survives. Default (plan order) makes `dclm` beat everything alphabetically | Set it explicitly before pass 1 runs |
| **D-f** | **"FinePhrase, one partition" — resolve the 3.3× ambiguity** | Mix reading. One reading is **INFEASIBLE** (`math` 1.52 epochs, `table` 1.66) | Reading (B): all four configs, each keeping its own quarter |
| **D-g** | **Wave shape** | Compute allocation | **DCLM 5-way @32 vCPU + FineWeb-Edu 16-way @8 vCPU = 288 of 384.** ⚠️ DCLM 10-way + FWE 16-way = 448, **exceeds the cap** |
| **D-h** | Should a missing filter block eventually **block** a publish? | Policy | eng-06 made absence a non-violation deliberately; revisit post-FREEZE |

## Unowned work items (both agents that could own them have completed)

1. **Amend `KeepFilter.unused`'s docstring** (`corpus_filter.py:817-822`) — it says "zero is the expected
   value"; MEASURED, non-zero is normal. **E11. My relay error; it must not reach a future reader as
   stated.**
2. **Fix the fail-open pool guard (E9)** — `CorpusSpec.__post_init__` compares `target_tokens` against the
   **undivided** `pool_tokens`, but a FinePhrase row now reaches only a quarter. Silent band 65–111B/row;
   bites above ~21.74B. One line per row in the registry.
3. **`corpus_build._s3`'s `Config` is now redundant** with `Boto3S3.default` — one-line cleanup.
4. **Wire `keeplists.json` into the verifier** for the real pass-1/pass-2 divergence check (needs pass 1's
   `scanned`, which the receipt cannot supply).
5. **A2a's DRIVER is not built** — the 256-way pass-1 fan-out over staged text. eng-05 stopped at its
   territory boundary (it needs `_reader_for`); `resolve_keep_lists(partitions_subset=)` is the seam.
   **A2a's data structure is done; A2a is not.**
6. **§4.3 anti-join** — `IdSet.contains` still has zero callers; 100% of an edu-web draw has a synthetic
   sibling. Mix question, no owner.

## Carried to the Phase 2 smoke test (ENG is forbidden from S3 and cannot run these)

- **Assert a deliberate digest corruption on the BYTES-shaped verified put.** The 2026-08-01 `BadDigest`
  evidence is **file-shaped**; the bytes path is the same API call, which is **identity, not
  measurement.** One extra assertion in a job already running.
- **Measure DCLM's real tokens/document** (the 1,261.5 is from the `dclm_100BT` sample, not the mirror).
- **Request the full wave shape once** — 12 concurrent `c7i.8xlarge` has never been demonstrated.

## Documentation hazards (CEO/AUDIT, not ENG code)

- `artifacts/impl-plan/pipeline-scale-audit.md` reasons throughout from the **withdrawn** 50,003,968 as
  though decided (12 sites), and its `:106` has the polarity **backwards**. Recommend a one-line
  withdrawal banner, not a rewrite.
- `DATASET-DESIGN-reservoir.md:539` rejects 10M on a **now-false** timeout premise. 10M stays rejected on
  the trainer hot loop. Do not cite its `:537` table (false by ~15×).
- **CLAUDE.md is stale in 3 places** confirmed this wave: the 786 baseline (now **1306** on this branch),
  the job-def table (validator is **rev 14 / 14400 s**), and the EventBridge rule (**ENABLED**).


---

# 🌊 WAVE 1 — two fail-open gates. Both CEO claims INDEPENDENTLY RE-VERIFIED by me before dispatch.

Tip check: **`5450f53` is already an ancestor of my HEAD** — the pushed `edullm/final-dataset-phase0`
is my Wave 0 tree plus the CEO's account-ID scrub. No rebase needed; workers branch from current HEAD.
Baseline for this wave is **1306**.

## ✅ Claim 1 re-verified — `problems()` is dead code on the build path

```
$ grep -rn "problems()" src/   →  0 callers
$ grep -rn "problems()" tests/ →  5 callers
```

**Exactly as the CEO stated.** I also read the guard (`corpus_read.py:839-869`) and confirm the
three-deep picture, in code:

- `drop_fraction > max_drop_fraction` (default **0.4**) is the guard that would have caught the 79.6%
  dolma3-QA attrition. Its own message names the failure: *"at this rate the pool arithmetic in
  §2.1/§3.2 was computed on tokens this source will not deliver."*
- The **mean guard structurally cannot fire** in that scenario, and I can now state the mechanism from
  the code rather than by assertion: it tests `mean_kept_tokens < MIN_MEAN_DOC_TOKENS`, i.e. **the mean
  of the SURVIVORS**. Trimming the short tail of a distribution *raises* the kept mean. So the harder
  the source fails, the safer this guard reports it. It is not merely silent — **it is
  anti-correlated with the defect.**
- So the only guard that fires is the one with **zero production callers**.

**Same class as the `families/` bug** (CLAUDE.md gotcha 2): passes in a checkout, protects nothing in
production. That bug is why a live corpus was validated at 50% EOS while declaring 5%.

**The priority argument I am passing to the worker verbatim:** DATA's domain-purity clearance is a
*pre-flight prediction from sampled means*. `problems()` is the **only runtime check that would catch a
source whose true distribution differs from its sample.** Shipping the prediction without the check is
the posture this repo exists to refuse.

## ✅ Claim 2 re-verified — THREE tables, and the one that runs is the narrowest

| # | table | site | accepts |
|---|---|---|---|
| 1 | `READABLE_FORMATS` | `corpus_build.py:130`, gates `_assert_readable:324` and `:946` | `parquet`, `json.gz` |
| 2 | **the inline dict that ACTUALLY RUNS** | **`corpus_build.py:1250-1253`** (inside `_reader_for`) | `parquet`, `json.gz` |
| 3 | `_READERS` | `corpus_read.py:748-751` | `parquet`, `json.gz`, **`jsonl.gz`** |

**Table 3 uniquely accepts `jsonl.gz`, and its reader is the same function** — `read_jsonl_gz_documents`
serves both keys. So a `jsonl.gz` row is rejected by tables 1 and 2 **while a working reader for it
exists**, which is W4's MEASURED live false negative: the rejection is real, and it *looks* like a
legitimate format check.

⚠️ Note the line numbers moved from the CEO's brief (`:908-911` → **`:1250-1253`**) because Wave 0 added
~340 lines to `corpus_build.py`. **Same code, same defect** — flagging so nobody thinks it is a different
site.

**`read_documents` IS dead code — confirmed:** `grep -rn "read_documents(" src/` excluding its own def
and the two concrete readers → **0 callers.** It is the only consumer of `_READERS`, which is why the
one *correct* table is the one nothing on the build path reads.

**`zstandard` correctly NOT to be added — confirmed:** 6 `zst` hits in `src/`, **zero import lines**,
all comments/error strings. The `.zst` gap closes **by removal**, not by a dependency nothing reads.

## Not redone, per the CEO (I re-confirmed both rather than assuming)
- **#23 DONE** — `tokenizers>=0.21,<0.23` + `pyarrow>=24,<26` in `pyproject.toml`, 3 tests. That is
  eng-08's Wave 0 work, already merged.
- **Mirror airlock policy** — applied, not a code item.

## Wave 1 DISPATCHED — 2 concurrent workers, new worktrees at `5450f53`

| stream | task | branch | findings file |
|---|---|---|---|
| 9 | wire `ReadStats.problems()` + `FilterStats` short-doc counter | `agent/eng-09/readstats-wiring` | `stream-09-readstats-wiring.md` |
| 10 | one canonical format table; `read_documents` verdict | `agent/eng-10/format-table` | `stream-10-format-table.md` |

**Both edit `corpus_build.py` AND `corpus_read.py`** — the same two-file overlap that made Wave 0's
convention pay. Each was told the other by name, function and line, and given explicit stay-out lists:
eng-09 out of `_reader_for`/`_assert_readable`/`READABLE_FORMATS`/`_READERS`/`read_documents`; eng-10
out of `run_bundle`/`problems()`/`FilterStats`.

⚠️ **eng-10 additionally warned that it owns only the DISPATCH TABLE inside `_reader_for`, not the
function** — Wave 0 (eng-07) rewrote that function's budget arithmetic and `keeps_id` filtering, and
`_CHARS_PER_TOKEN` is a WITHDRAWN change. A Wave-1 agent editing Wave-0 logic it did not read is a real
risk, so I named it explicitly.

**Both told the anti-pattern for their own task**, since "write a test" is not sufficient instruction
here: for eng-09, *a `problems()` test that only checks the method exists is the same decoration we are
removing*; for eng-10, *a test hardcoding the format set in a fourth place has added a fourth table*.
eng-09 must also report a **mutation result** — break the wiring, watch a test fail.

**eng-09 given the raise-vs-warn question as a decision to justify, not a default.** `problems()`
deliberately returns strings rather than raising, and its docstring says the caller owns the policy. A
gate raising at end-of-run costs the full billable read + tokenize + upload — the `_drain_surplus` shape
that killed 25 of 27 bundles, and the exact reason eng-06 removed the `unused > 0` gate in Wave 0. I
pointed eng-09 at that precedent rather than dictating the answer.


---

# 🔴 WAVE 2 — AUTHOR THE 1.0T REGISTRY. ENG-EXEC takes this directly (critical path, sole blocker).

CEO lifted the wave hold on the rate question; PLAT refused the launch order and was **right**. The
registry on disk is the **finished 252.6B reservoir** and `edullm-reservoir-build:9`'s baked
`PLAN_ID=d5c9bcd38735e1f0` is that completed corpus — a GO would have re-tokenized finished work on
stale code (0.7.4, not the preflight-verified 0.9.1) at the wrong fan-out (27 vs 48).

**This is decision D-a from my Wave-0 report, unactioned for a wave.** I flagged it and did not re-raise
it when the hold lifted. Logging that: **a decision I own does not stop being mine because I reported
it once.**

## CEO rulings this wave must encode (all now RULED — nothing waits on the owner)

| # | ruling |
|---|---|
| **fineweb-edu** | `config: "data"`, pool 1,583,146,000,000, target 252,000,000,000. **NOT `sample/350BT`** — that satisfies size while **silently breaking the FinePhrase anti-join** |
| **DCLM** | repointed; licence ODC-BY-1.0 → **CC-BY-4.0** (owner-approved). Rows 1a/1b are child AND parent |
| **row 14 dolma3 QA** | **STRUCK.** 54.4 tok/doc, 2.7× off the EOS bound, 79.6% under `MIN_DOC_TOKENS`. Free: worst epoch 0.558 vs 0.900 |
| **row 17** | `source_label` = **`math-textbooks`**, NOT `nemotron-math-textbooks` |
| **row 15** | `Nemotron-Pretraining-Specialized-v1`/`InfiniByte-Reasoning`, ungated CC-BY-4.0, **not** `nemotron-*` |
| **nemotron source** | `s3://edullm-landing/_src/nemotron-cc-math-v1/`, **`3/` and `4plus/` by explicit prefix, never a glob** |
| **shape** | **48 × 8-vCPU** children |
| **labels** | `dclm-01…NN` zero-padded; **no label may be a string-prefix of another** |
| **priority** | mine to decide and document |
| **FinePhrase** | take the FEASIBLE reading (I found `math` at 1.52 epochs infeasible) |
| **zstandard** | do NOT add — row 14 struck leaves it no consumer |

## The prefix-collision rule — why it is a REAL hazard, not naming taste

`SAFE_SEGMENT_RE` validates characters **within one segment** and is **structurally incapable** of
catching a prefix collision. So `--prefix tokens/nemotron-` would sweep in a CC-BY-4.0 source carved out
of the NVIDIA agreement. This is a **licence-boundary** failure reachable by a routine prefix operation.
Generalised by the CEO to: **no `source_label` may be a string-prefix of any other.** I am making that a
recomputing test over all rows, not a naming convention.


---

# ✅ STREAM 9 COMPLETE — and it REFUTED TASK 2's PREMISE. **I verified the refutation myself.**

Commit `fb29a02`. **Tests 1306 → 1314 (+8), verified by me.** Scope: `corpus_build.py`,
`corpus_read.py` + their tests. **Clean separation from eng-10 confirmed** — grep of its
`corpus_read.py` diff for `READABLE_FORMATS|_READERS|read_documents|_assert_readable|_reader_for`
returns **empty**. Not pushed.

## 🔴 E12 — TASK 2'S PREMISE IS FALSE. The short-doc counter would be PERMANENTLY ZERO.

The CEO's brief and the LEDGER (`:1055-1058`, `:1148-1151`) say the short-doc loss is recoverable as
`seen − kept − duplicates − contaminated`. **It is not. I proved it by execution, not by reading:**

```
$ 100 documents, 50 of them short
seen 100  kept 2  dup 98  contam 0
RESIDUAL seen-kept-dup-contam = 0
actual short docs in the input  = 50
```

**The residual is identically 0 while 50 short documents exist.** The mechanism, read out of
`corpus_filter.py:318-327`: `dedup_and_decontaminate`'s branch is exhaustive — every document
increments exactly one of `duplicates`/`contaminated`/`kept` and then `continue`s or yields. **The
residual is the closure identity a test already asserts, not an unlabelled bucket.** And the function
**takes no tokenizer** (verified: no `token`/`length`/`min_tokens` in its signature) — it runs *before
any length is known*, so it cannot count short documents even in principle.

**Adding the counter would have shipped exactly the decoration the CEO told us to remove.** eng-09
declined and was right. **The counter the LEDGER wants already exists** on the class that can compute
it: `corpus_read.FilterStats.dropped_short`.

⚠️ **This is the SECOND obligation I relayed whose premise did not hold** (E11 was the `unused > 0`
gate). Both times the worker caught it. **The pattern in both: a property true of a data structure in
isolation, asserted about a pipeline nobody had run it through.** I am adopting a rule for myself —
**before relaying a "just add X" obligation, execute the claim it rests on.** Both refutations cost a
worker's time that a two-minute script would have saved.

## 🔴 E13 — THE REAL HOLE, which the false premise was hiding

`run_bundle` returned a `length` block with **no reader anywhere in `src/`** — `_cmd_run` printed only
the dedup block. `FilterRecord`'s docstring excuses omitting `length` because *"it is returned below and
printable"* — **true of the value, false of the program.** So on Batch, **the number that killed the
dolma3 QA row survived nowhere**: not the receipt, not stdout, not CloudWatch. Fixed — `_cmd_run` now
prints it with a `drop_fraction` next to both its terms.

## Task 1 landed, and the "anti-correlated" claim is now MEASURED, not argued

eng-09 reproduced the QA shape end-to-end through `run_bundle` on `FakeS3` (mean 54.4 tok, CV 0.212,
4,000 docs, seed 42, floor 64):

| | value |
|---|---|
| `length.seen` / `kept` / `dropped_short` | 2,200 / 462 / **1,738** |
| `drop_fraction` | **0.790** — 1.97× over the 0.4 threshold |
| `mean_kept_tokens` vs floor | **70.09 vs 20 — 3.5× SAFE** |
| **`run_bundle` verdict** | **SUCCEEDED. 2 shards, receipt written, silent.** |

All three checks that look like they cover this miss it structurally: `_verify_shard` passes
*correctly* (the shards are fine); the mean clause reports it **safer the worse it gets**;
`receipt-empty-bundle` fires only at 100% loss.

## Raise-vs-warn: **WARN**, and the reasoning is stronger than mine was

I left this open deliberately. eng-09's answer, with two arguments I had not made:
1. **The shards are already in S3** when the counters finalise (upload is inside `pack`'s sink), so a
   raise pays full billable work then refuses — the `_drain_surplus` shape, and the third instance of
   this pattern.
2. **A raise would ORPHAN the uploaded shards** — written but un-receipted, so `bundle_is_done` reports
   unbuilt and the next run rewrites the same keys. **Strictly worse than warning.**
3. **Fatal needs no code change:** `-W error::edullm_data.corpus_read.AttritionWarning`. Its own
   category *because* `corpus_pack` already emits `RuntimeWarning` on this path — a shared category
   would be unfilterable and a test on it would pass for the wrong reason.
4. Also printed to **stdout** (`ATTRITION <bundle_id>: …`) because a 27-child array job's stderr
   interleaves.

eng-09 states the limit itself: **"we warn" ≠ "we are covered."** The place this should gate is a
**sampled pre-flight before the read** — plan-stage work, unowned.

## Mutation testing — 6 mutants, and one exposed a defective test THE AGENT HAD JUST WRITTEN

| mutation | result |
|---|---|
| un-wire `problems()` | 2 failed ✅ |
| delete the drop-rate clause | 4 failed ✅ |
| threshold 0.4 → 0.5 | 3 failed ✅ |
| `_cmd_run` stops reading `info["length"]` | **SURVIVED** → fixed → 1 failed |
| delete the closure check | 1 failed ✅ |
| `_cmd_run` stops printing `ATTRITION` | 1 failed ✅ |

🔴 **Its first `_cmd_run` test used `inspect.getsource` + a re-implementation of the format strings —
it asserted the function's TEXT, not its BEHAVIOUR.** That is the CEO's exact anti-pattern, shipped by
the agent assigned to remove it, and **only the mutation run caught it.** Replaced with a test driving
the real `_cmd_run` through monkeypatched I/O seams. **This is the strongest argument yet for requiring
a mutation result rather than a passing suite** — I will require it for every guard-wiring task.

Also: `1314 passed` **under `-W error::AttritionWarning`**, so the new guard is not noise.

## Naming corrections (eng-09, verified)
**There is no class named `ReadStats`** — `grep -rn "ReadStats" src/ tests/` → **0**. It is
`corpus_read.FilterStats` (`:785`), aliased `LengthStats` at `corpus_build.py:765`. **Two classes are
named `FilterStats`, and that collision is the root cause of E12.** `problems()` had **5** test callers,
not 4. The CEO brief, the LEDGER and my own prompt all used the wrong class name.

## Decisions needed
1. **Attrition in the receipt?** Not added — it would be a **fourth denominator** in an artifact Wave 0
   worked to keep at three, and needs a **schema v3** call. eng-09 did not bump silently. ✅ right call.
2. **Pre-flight gate is unowned.**
3. **`max_drop_fraction = 0.4` is UNVERIFIED for any specific source** — nobody has measured it. A
   per-source threshold would be a registry field. **Noting for the registry I am authoring now.**
4. **The LEDGER's subtraction claim must be corrected** so it is not re-assigned to a future agent.


---

# ✅ THE 1.0T REGISTRY IS AUTHORED — `artifacts/final-dataset/corpus-registry.json`

**40 rows (39 drawn + 1 RESERVE). Summed `target_tokens` = 986,000,000,000.**
**`PLAN_ID = a5df0404b640e4c9`** · 75 bundles · 39,400 shards · planned tokens 985,078,169,600.

**986B, not 1,000B** — the report's nominal less the 14B dolma3-QA row the CEO struck. I caught my own
error mid-authoring: a first pass summed to **940B** because I used the report's **stage-1** figures as
if they were the whole draw. **The report's two epoch tables are PER STAGE and are never summed there**
(§ "The epoch columns are PER STAGE"), but a source is **READ ONCE**, so a registry row must carry the
**combined** stage-1 + stage-2 draw. DCLM is 378+32=410B, code 90+18=108B, Nemotron-CC-Math 45+16=61B.

## What I did NOT re-derive
Every identity string — repo, config, 40-char revision, `text_column` `path_in_schema`, id column,
domain column, licence — is **inherited verbatim from DATA's dossier**, per instruction. I re-derived
only the two things the dossier does not carry: the **split-row `config` strings** and their pools.

## The two split families, MEASURED not assumed

**DCLM → 10 rows**, one `global-shard_NN_of_10` each, at the **4-level prefix** (a bare
`global-shard_NN_of_10` is a hard HTTP 404 — dossier B7). Aggregated from DATA's own 27,938-entry tree
artifact: **7,419,668,271,828 bytes, skew max/min = 1.0025.** 41.0B per child.
⚠️ **10 ways, not the CEO's 5.** At 8 vCPU a 5-way child is 39.0 h. 10 ways is the shape that fits, and
each row still names exactly one disjoint directory.

**FineWeb-Edu → 16 rows**, one `data/CC-MAIN-*` each. I paged the HF tree API read-only and
**independently reproduced the dossier to the byte: 110 dirs, 2,410 files, 4.5227 TB**, max
`CC-MAIN-2023-40` 70.9 GB, min 22.8 GB, **skew 3.11×**, and **exactly 31 dirs ≥ the 15.75B per-row
target — eng-04 said 31.** Selected the 16 largest: 332.8B pool for a 252B draw (1.32× headroom).

**Pool discipline:** DCLM children are sized against the **733B UNIQUE** pool
(`artifacts/recount/web.json`), **not** the 3,764B all-copies figure. So 410B reads as **~52% of every
unique DCLM token in existence** — dossier B10's escalation, carried into the row traps rather than
hidden by a friendlier denominator.

## The test recomputes, and I mutation-tested it — 7 of 7 caught

`tests/test_final_dataset_registry.py`, **15 tests**. Not "the file parses":

| mutation | result |
|---|---|
| collide a label (`math-textbooks` → `nemotron-math-textbooks`) | **CAUGHT** |
| drop 1B from one target | **CAUGHT** |
| truncate a revision to 12 chars | **CAUGHT** |
| point `fineweb-edu-01` at `sample/350BT` | **CAUGHT** |
| bare `global-shard` config on `dclm-01` | **CAUGHT** |
| overdraw a pool | **CAUGHT** |
| remove a row from `_source_priority` | **CAUGHT** |

The prefix-collision test checks **every ordered pair**, because `SAFE_SEGMENT_RE` is structurally
incapable of comparing two labels. The format test recomputes against the **live `_READERS`** rather
than restating the set — restating it would have been eng-10's fourth table.

## Source priority — my decision, documented (CEO ruling 5)

`_source_priority` + `_source_priority_basis` in the header: **public-domain/curated reference >
academic > targeted math+reasoning > code/forum > curated web > synthetic > bulk web.** DCLM ranks
**last** — under alphabetical order it beat every other source by the accident of the letter d, and it
is the least curated pool in the mix. **Recorded as a judgement, not a measurement**, so it can be
argued with rather than inherited silently.

## FinePhrase — I took reading (A), one config, and here is why

CEO ruling 6 says take the FEASIBLE reading. **Reading (B) — all four configs each keeping their own
quarter — is what the report's 123.3B/0.29 describes**, but it needs **four rows**, and eng-07's
`_finephrase_format` applies `keeps_id` per config, so four rows would draw 9B each. **Reading (A) —
one config (`faq`) keeping its quarter — is one row at 36B against a 36.9B quarter-pool: 0.98 epochs.**
That is feasible but it is **the tightest row in the corpus after finepdfs-edu**, and it is what I
encoded. ⚠️ **If the CEO prefers the report's own (B), it is a 4-row edit and I will make it** — (B) is
materially safer at 0.29 epochs. Flagging rather than silently choosing the tighter one.

---

# 🔴🔴 E14 — THE REGISTRY PLANS, BUT THE MAKESPAN IS **51.38 h**, NOT 9.96 h. **THREE MORE SOURCES NEED SPLITTING.**

**This is the most important finding of the wave and I found it by simulating the plan I had just
written, not by reading one.** I packed the 75 real bundles onto 48 × 8-vCPU children, longest-first,
at the MEASURED 72,615 tok/s/vCPU:

```
MAKESPAN over 48 x 8-vCPU children = 51.38 h      (floor 9.96 h)
single largest bundle              = 51.38 h      <- no packing beats this
```

**The makespan IS the largest single bundle.** 47 children idle while one runs. The 48-child shape
cannot help, because `--shard/--of` strides **bundles** — the same constraint that made C3b mandatory
for DCLM applies to sources nobody split:

| bundle | tokens | 8-vCPU child | ways for ≤9.96 h |
|---|---|---|---|
| **`stackv2-edu--train`** | **107.5B** | **51.38 h** | **6** |
| **`finepdfs-edu--train`** | **62.7B** | **29.97 h** | **4** |
| **`nemotron-cc-math-3`** | 38B | 18.17 h | **2** |
| **`finephrase`** | 36B | 17.21 h | **2** |
| **`nemotron-cc-math-4plus`** | 23B | 11.00 h | **2** |
| `dclm-NN` (×10) | 40.8B | 19.50 h | ⚠️ see below |

**⚠️ And my own DCLM 10-way is still 19.50 h per child — it needs ~20 ways, not 10.** I sized the split
on the CEO's 5-way ruling scaled to 8 vCPU and **did not re-simulate until now.** Correcting in place:
**10 ways is not enough either.**

**So the plan's "split DCLM and FineWeb-Edu" is incomplete by three sources.** `IMPLEMENTATION-PLAN.md`
§8A.3 names DCLM (410B) and FineWeb-Edu (252B) as the only bundles needing splits — but **code at 108B
is larger than a single DCLM child**, and it was never listed because the reservoir drew far less of it.

## Why I did NOT just add more split rows

**Only DCLM and FineWeb-Edu have a disjoint-subdirectory carve.** The other three do not:
- **`stackv2-edu`** and **`stackexchange`** ship a **`domain_column`**, so they fan out *naturally* —
  and that is the right fix, not a subdirectory split. **But I tested it and it RAISES:**
  `stackexchange/site00/val: 2,250,000 tokens yields zero shards — short by 22,751,984`. That is
  `shard_plan`'s guard working **correctly** (a skipped stream gets no ordinals, so `pack` would find
  no destination and the corpus would publish clean with a source missing). It means the domain fan-out
  needs a **val-split decision** per domain — plan-shaped work inside FREEZE.
- **`finepdfs-edu`** (`data/eng_Latn/train`) and **`finephrase`** and the **Nemotron** rows have no
  documented disjoint subdirectory carve in the dossier. Inventing `config` strings I have not walked
  is exactly the "guessed key" failure D3 struck B4 for.

**I am not fabricating carves at 04:00 on the critical path.** The registry is correct and buildable as
written; the **wave shape is not yet 9.96 h**, and that is a plan decision with three inputs I do not
have.

## What this does NOT change
- The registry's identity strings, licences, priority, and token sum are all still right.
- `plan_document` succeeds; `PLAN_ID a5df0404b640e4c9` is real and reproducible.
- **PLAT can register the job def against it** — the shape question changes the child count and the
  wall clock, not the plan's content. But **a 48-child launch will take 51 h, not 10**, and someone
  should know that before the owner wakes.


---

# 🌊 WAVE 3 — CEO ruled both E14 decisions. ENG-EXEC executing directly.

- **DECISION 1: WALK the subdirectories.** `stackv2-edu` (6 ways), `finepdfs-edu` (4 ways), DCLM
  re-split to ~20. **Do NOT take the domain fan-out** — a walk is a measurement, the val-split ruling
  is a design change inside FREEZE; and DCLM/FineWeb-Edu already use the subdirectory mechanism, so
  one split mechanism stays auditable. **If a source has no walkable disjoint structure: STOP and
  report. Do not fall back, do not fabricate.** *"An unsplit 51 h bundle we know about beats a wrong
  carve we don't."*
- **DECISION 2: reading (B)** — four FinePhrase rows at 9B each. A 0.98-epoch row has no margin; any
  attrition pushes it over 1.0 and surfaces as a bundle that will not fill **at the end of the run** —
  the failure mode row 14 was struck for.
- **Target: every bundle ≤ 9.96 h. Re-simulate and report the makespan — do NOT report the split as
  done on the arithmetic that motivated it.** (Both of us made that mistake once tonight.)
- **Short-doc counter: DROPPED by the CEO** — *"I ordered decoration."* `problems()` wiring is the fix.


## Wave 3 AMENDMENT (CEO): add nemotron-3 (>=4 ways) + nemotron-4plus (>=2), split to <=8 h then STOP,
## quote 11-15 h and NEVER 9.96 h. PLAT: the plateau is always the bundle you did not split.

Executing. Two verifications first, because the amendment names a mechanism I must confirm exists:
`config` is consumed by `hf_files` as a TREE PATH (`{base}/{spec.config}?recursive=1`), so a FILE
RANGE may not be expressible. Checking before I write a single row.

### 🛑 STOPPING AS INSTRUCTED — the amendment's carve is NOT expressible. Three sources, one cause.

**CEO: *"If any source has no walkable disjoint structure, stop and tell me — do not fall back, do not
fabricate."* Doing exactly that.** All four walks are MEASURED, read-only, at the pinned revisions:

| source | tree at pinned rev | subdirectories | verdict |
|---|---|---|---|
| **`stackv2-edu`** | repo root | **0 dirs, 97 files** (`stack-edu-NNNN.json.gz`) | ❌ FLAT — no walk can split it |
| **`finepdfs-edu`** | `data/eng_Latn/train` | **0 dirs, 100 files** | ❌ FLAT |
| **`nemotron-cc-math-3`** | `3/` | **0 dirs, 57 files** (`part_000000`–`part_000056`) | ❌ FLAT |
| **`nemotron-cc-math-4plus`** | `4plus/` | **0 dirs, 46 files** | ❌ FLAT |

**DATA's file counts are exactly right (57 and 46). What is not right is that a file range is
expressible.** Two independent code facts, both MEASURED-IN-CODE:

1. **`config` is a TREE PATH, not a file selector.** `hf_files` builds
   `{base}/{spec.config}?recursive=1` and then takes **every** file under it
   (`corpus_build.py`). There is no start/end, no glob, no slice. `config: "3/part_00000*"`
   would be requested as a *directory* and 404.
2. **`file-shard` — "one bundle's source files split across children" — is listed in
   `IMPLEMENTATION-PLAN.md:1629` as `DOES NOT EXIST`.** That is the exact capability the amendment
   assumes. It is the same row that made C3b mandatory.

**And the `_src/` S3 staging does not rescue it:** every reader resolves
`https://huggingface.co/datasets/{repo}/resolve/{revision}/{path}` (`corpus_read.py:391`). **No
registry row can read `s3://` at all** — grep for `_src/` in `src/` returns nothing. The staged bytes
are real and byte-verified, but no code path reaches them from a registry row.

**So the amended list cannot be executed as specified, and neither could my own three.** The finest
expressible unit is a **directory**, and these four sources have none.

**PLAT's doctrine — "was this sized on the reservoir?" — applied to the amendment itself:** the
file-range route is exactly the mechanism the plan already records as absent. It is the same class of
inherited assumption, one level up.

### The numbers, MEASURED on the real bundle list — and they CONFIRM the CEO/PLAT band

**Current registry** (DCLM 100-way + FineWeb-Edu 16 + FinePhrase reading B) — `PLAN_ID
9f969e08a5bbbd07`, **161 bundles**, 986,000,000,000 target:

| bundle over 8 h | tokens | 8-vCPU child |
|---|---|---|
| `stackv2-edu--train` | 107.46B | **51.38 h** |
| `finepdfs-edu--train` | 62.68B | **29.97 h** |
| `nemotron-cc-math-3--train` | 37.80B | **18.08 h** |
| `nemotron-cc-math-4plus--train` | 22.88B | **10.94 h** |

**MAKESPAN 48×8 = 51.38 h — exactly the longest bundle, and IDENTICAL to before my splits.**
DCLM 10→100 and FinePhrase 1→4 rows bought **0.00 h** of makespan, precisely as PLAT predicted:
*"the makespan is always whichever bundle you didn't split."* **I re-simulated instead of reporting
the split as done — the discipline the CEO required, and it is the only reason this is visible.**

**Exactly 4 bundles exceed 8 h, and they are the 4 flat sources.** That settles the PLAT-vs-CEO count
disagreement the CEO asked my walk to arbitrate: **PLAT's 15 and the CEO's 4 were counting different
objects** — 161 bundles come from 133 rows via train/val decomposition, and **on the real bundle list
the answer is 4.** The CEO's count matches; PLAT's 15 was rows-before-decomposition.

**If file-sharding existed** (splitting only those 4 to ≤8 h, DERIVED):

| | value |
|---|---|
| longest bundle | **7.49 h** |
| **MAKESPAN 48×8** | **11.19 h** ✅ |
| MAKESPAN 64×8 | 7.79 h |
| aggregate floor @48 | 9.79 h |

**11.19 h sits inside the CEO's 11–15 h band and independently reproduces PLAT's 11.00–13.07 h from a
third method.** Three estimates, three routes, one answer. **Quote 11–15 h. Never 9.96 h.**

**The residual above the floor is bin-packing, exactly as the CEO said**, and 64 children beat 48
(7.79 h) only because the array stops being the constraint — not because the floor moved.

⚠️ **Both caveats the CEO and PLAT flagged on their own numbers apply to mine and I am not exempting
them:** these are `DERIVED` from `target_tokens` at a **uniform 72,615 tok/s/vCPU**, which is certainly
false — **PDF and code do not tokenize like web text**, and `finepdfs-edu` and `stackv2-edu` are two of
the four sources in question, so the error lands exactly where it matters most. **Shape, not answer.**

### ONE FIX UNBLOCKS ALL FOUR, and it is the item the plan already scoped

**Implement file-sharding** — `IMPLEMENTATION-PLAN.md:1629` marks it `DOES NOT EXIST`, and §8A.5a
scopes it: a bundle split into K children each reading a **disjoint slice of its source files** and
writing a **disjoint ordinal range**. The plan's own note is that *"ordinals are the hard part, not the
reading"* — `allocate_ordinals` walks the plan with one counter per split, so K children must get their
ranges **from the plan, at plan time**. It is **plan-shaped work, therefore inside FREEZE.**

**This is the same capability C3b needed and got around** by using registry rows on disjoint
subdirectories. **Four sources have no subdirectories, so the workaround is exhausted and the real fix
is now load-bearing.** It is ~1 file-list slice in `_reader_for` + an ordinal-range field in the plan.

**I did NOT implement it**: it is plan-shaped, it lands in `_reader_for` and `allocate_ordinals` (two
other agents' Wave-0 surfaces), and it changes every `plan_id`. **CEO's call.**


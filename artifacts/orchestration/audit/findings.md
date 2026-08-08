# AUDIT-EXEC findings — wave 0, 2026-08-08

**Written by the CEO, not by AUDIT-EXEC.** AUDIT's first tool call was the required write of this file and
it was **blocked by a harness guard** ("Subagents should return findings as text, not write report files").
It returned everything as text instead; this file is the CEO transcribing that return verbatim in substance.
**Process consequence recorded at the bottom — the hard incremental-write rule is not enforceable for
every subagent, so the CEO persists returns on arrival.**

Grades: MEASURED · MEASURED-IN-CODE · DERIVED · CARD · UNVERIFIED.

---

## 🔴 F1 — Promote is **3.3–5.9 h**, not 1 min and not 20–30 min. Ruling R3 is answered.

**Both plan figures are wrong. The CEO hypothesis is REFUTED** — §8.2's "20–30 min" is *not* misscoped to
publish's copy phase; it is promote proper, and still ~4× optimistic. The "~1 min" figure is off by ~200×.

### The requested arithmetic, and why the formula is the error

`objects × round-trips ÷ workers × per-call latency`:

| input | value | grade |
|---|---|---|
| objects, stage 1 | 36,000 (40,001 both stages) | DERIVED — `1e12 / 25,001,984 = 39,997` |
| round trips/object | **3, not 2** | **MEASURED-IN-CODE** |
| workers | 16 (`--promote-workers 16`, rev 14) | MEASURED |
| per-call latency | 63.4 ms (507.5 ms ÷ 8) | DERIVED from the Gate A anchor |

`40,001 × 3 ÷ 16 × 63.4 ms = 476 s = 7.9 min`. At 2 rt: 5.3 min.

**Round trips are 3, not ~2.** `Boto3S3.copy` (`s3.py:436-449`) issues `head_object` for `ContentLength`
before `copy_object`; `_crc_for` (`validate.py:~2095`) issues a third `head_object` on the destination.
`_destination_matches` adds a `hash_object` stream but only for `vendored` payloads, and
`_PayloadSnapshot.vendored` = `profile.startswith("vendored/")` (`validate.py:493`) — this corpus is
`pretrain_tokens/v1`, so that path does not fire.

**But the formula itself is a unit error.** It models `CopyObject` as a control-plane call. It is not — it
moves 100 MB of payload server-side. A latency model of a bulk data mover is the exact error class the
brief warned about.

### The measurement that settles it — the only promote ever run at scale

`infra/DEPLOY.md:820-821`, MEASURED 2026-08-05: *"7200s is what SIGKILLed the reservoir promotion at
**6,324 of 10,051 objects**."* Same job ran Gate A at ~85 min. Same `--promote-workers 16`. Same 100 MB
shards — `1,004,872,007,680 B ÷ 10,049 = 100.0 MB`, **identical** to this corpus's
`SHARD_TOKENS × 4 = 100,007,936 B`. Denominators match; the transfer is valid.

```
promote window    = 7200 − (85 × 60)          = 2,100 s
observed rate     = 6,324 / 2,100             = 3.01 obj/s = 332 ms/object
implied bandwidth = 6,324 × 100.0 MB / 2,100  = 301 MB/s
```

301 MB/s corroborates **M13's 362 MB/s** server-side copy anchor (`wallclock-audit.md:162`) — two
unrelated runs, same order. The latency model predicts 12 ms/object; reality is 332 ms. **28× wrong.**

| | stage 1 (36,000) | both stages (40,001) |
|---|---|---|
| **central @ 301 MB/s** | **3.32 h** | **3.69 h** |
| pessimistic (Gate A 25% fast) | **5.34 h** | **5.93 h** |
| optimistic (Gate A 25% slow) | 1.30 h | 1.45 h |

±25% is the repo's own stated Gate A run-to-run variance (`HANDOFF.md:877`).

### 🔴 The consequence that matters more than the number

Rev 14's command is a **single `validate --promote`**, so Gate A and promote share one job and one timeout:

`Gate A 4.48 h + promote 3.32 h = **7.80 h against a 4.00 h (14,400 s) timeout.**`

**Submitting stage 1 on rev 14 today gets SIGKILLed partway through promotion — the exact failure that
already happened on 2026-08-05.** A partial promote leaves an **unsealed prefix in `edullm-data` that
cannot be deleted** (Delete Deny). `promote()` refuses every *sealed* prefix but not an unsealed partial;
recovery runs through `_sealed_snapshot_matches`, which is CLI-only and narrow.

**Remedies, in order:** (1) land B3/#10 so Gate A is 0.32 h not 4.48 h; (2) raise the timeout past 8 h —
it is our own setting and AWS imposes no maximum; (3) split Gate A and promote into separate jobs.

**Blast radius.** The release gate was about to be answered on a number **60–200× too small**.
`BUILD-DEPENDENCY-GRAPH.md:53` sums `PR1 = 0.02 h` into the 11.0 h job floor and `:316` puts it on the
critical path. The plan's stated reason to trust ~1 min ("already threaded") is true **and irrelevant** —
it *was* threaded at 16 during the run that was killed.

**No contamination:** `plat/status.md:38` still lists task 7 as `⏳ pending` and the file ends at 132 with
nothing further. AUDIT saw no PLAT answer.

---

## 🔴 F2 — HIGHEST BLAST RADIUS. The 9.96 h floor holds **only at ~48 × 8-vCPU children.** The graph draws 12 × 32, which is **33.2 h**.

**Confirmed digit for digit:** `1e12 ÷ (384 × 72,615) ÷ 3600 = 9.9618 h` ✅ · `0.328e6 ÷ 72,615 = 4.517×` ✅
· `89.3 ÷ 14.4 = 6.20×` ✅ · 22.1% / 77.9% encode/filter reproduces from the plan's own anchors ✅ ·
`1e12 ÷ 25,001,984 = 39,997` ✅.

**The scope error nobody caught:** 72,615 tok/s/**vCPU** was measured on **8-vCPU containers**
(`task-28-briefing.md:120-123`, `edullm-reservoir-build:9` = 8 vCPU / 14336 MiB). The plan then applies it
per-vCPU at **32-vCPU** children (`IMPLEMENTATION-PLAN.md:1444`, `BUILD-DEPENDENCY-GRAPH.md:119`
"12 children × 32 vCPU"). **Invalid by the plan's own physics** — 78% of the work is single-threaded Python
holding the GIL, so only the 22% encode half scales with vCPU:

```
8-vCPU container : 580,920 tok/s  (= 72,615 × 8)                     MEASURED
  encode 22.1% → scales with vCPU ; filter 77.9% → FIXED, GIL-bound
32-vCPU container: 696,598 tok/s  = 1.20× the 8-vCPU box, NOT 4×
  → per-vCPU at 32 = 21,769 tok/s/vCPU, not 72,615
```

| claim | plan | recomputed | source |
|---|---|---|---|
| 384 vCPU as **12 × 32** | 9.96 h | **33.2 h** | `BUILD-DEPENDENCY-GRAPH.md:119`, `IMPLEMENTATION-PLAN.md:1381` |
| 384 vCPU as **48 × 8** | 9.96 h | **9.96 h ✅** | the shape the rate was measured on |
| DCLM 410B, one 32-vCPU child | 49.0 h | **163 h** | `IMPLEMENTATION-PLAN.md:1420` |
| DCLM ways at 32 vCPU | **5** | **16** (= 525 vCPU > the 384 cap) | `IMPLEMENTATION-PLAN.md:1444` |
| FineWeb-Edu ways at 32 vCPU | 4 | **10** | same |

**The 49.0 h figure is arithmetically impossible on the plan's own model.** Filter-only rate is
`1/(1/580,920 − 1/2,624,000) = 746,096 tok/s` per container, **independent of vCPU count**. DCLM as one
child therefore floors at `410e9 ÷ 746,096 ÷ 3600 = 153 h at infinite vCPU`. A claimed 49.0 h at 32 vCPU
is **below its own asymptote**. It came from dividing 196 h by 4 — treating a GIL-bound serial workload as
linearly scaling, the very error §8A.3 exists to correct.

**§8A.3's recommendation is invalidated directly.** It recommends 5-way DCLM + 4-way FineWeb-Edu at
32 vCPU as "fits in 288 of 384 vCPU." Recomputed: DCLM 5 ways × 82B = **32.7 h/child**; FWE 4 ways × 63B =
**25.1 h/child**, against a 9.96 h `BUILD`. **The recommended wave shape misses the floor by 3.3×.** The
8-vCPU column (20 ways / 13 ways) is the correct one and is the shape the measurement supports.

**Why this is pre-FREEZE and irreversible:** it decides how DCLM is split, which sets `plan_id` and the
registry rows. **Must be settled before the rows are written.**

Note the trap the brief named is live: the plan states aggregate-vs-per-child correctly in prose (§8A.5)
and then gets it wrong in the table at `IMPLEMENTATION-PLAN.md:1444`.

---

## 🟠 F3 — The 15.5 h critical path is not defensible; four places are stale

**Verified as arithmetic:** `4.00 + 0.50 + 0.40 + 9.96 + 0.30 + 0.32 + 0.02 = 15.50 h` ✅ internally
consistent (`BUILD-DEPENDENCY-GRAPH.md:316-322`); job floor `0.40+9.96+0.30+0.32+0.02 = 11.00 h` ✅.
**But two of the seven terms are wrong:** `PR1 = 0.02 h` → **≥3.32 h** (F1); `BUILD = 9.96 h` holds only at
48 × 8-vCPU, else **33.2 h** (F2).

**Corrected: ~18.8 h** at the 8-vCPU shape with F1's promote; **~42 h** at the shape the graph draws. The
**54.5 h unsplit** figure is the one number AUDIT could not fault — and it is now *closer* to truth than 15.5 h.

**`--shard/--of` trap: CONFIRMED and correctly handled** at `HANDOFF-FINAL-DATASET.md:211` (strides
bundles; DCLM is one child; an aggregate floor is not a per-child bound). **But the same file quotes
10.85 h** for that child, computed at the withdrawn 0.328 M rate — at the measured rate it is 196 h.
**Stale in the exact sentence that warns against this error class.**

| file:line | figure | status |
|---|---|---|
| `HANDOFF-FINAL-DATASET.md:211` | 10.85 h per-child; 13.31 → 21.31 h | **STALE — presented as current** |
| `docs/IMPLEMENTATION-PLAN.md:1845,1855,1858` | *"Quote 21.31 h to anyone asking when the corpus will exist"* | **STALE — an active instruction to quote a dead number** |
| `docs/IMPLEMENTATION-PLAN.md:1714` | 21.31 h | STALE |
| `docs/IMPLEMENTATION-PLAN.md:1025` | "21.31 → 20.14 h" | STALE |
| `docs/IMPLEMENTATION-PLAN.md:1530` | "path is 7.75–15.75 h" | **STALE — 7.75 is the withdrawn 2.21 h row** |
| `docs/BUILD-DEPENDENCY-GRAPH.md:242` | "why the path is 21.31 h" | STALE |
| `docs/BUILD-DEPENDENCY-GRAPH.md:344-347`, `:510`, `docs/TASKS.md:23` | 21.31 / 20.14 / 24.90 h | STALE (B3 comparison table) |

---

## 🟠 F4 — The `verify --deep` retirement rests on a **fail-open write path**; B7 is not "~5 lines"

§8.3a retires `VD1` (1.49 h off the path) because "a verified PUT is rejected server-side on mismatch."
**CONFIRMED about `put_file_verified`** (`s3.py:290-346`, declares `ChecksumSHA256`, live-verified
2026-08-01). **REFUTED about this pipeline:** `corpus_build.py:463`'s sink calls plain
`s3.put(bucket, key, payload)`, and `Boto3S3.put` (`s3.py:264-281`) declares **no checksum at all** —
`ChecksumSHA256` appears only inside `put_file_verified`. The mechanism the retirement rests on **does not
run on the build path.**

The plan flags this as B7 "~5 lines" and **understates it**: `put_file_verified` takes a **`local_path`**,
while the sink holds **bytes in memory** and deliberately never touches disk. B7 is a new bytes-accepting
verified-put method, not a call-site swap.

**The plan's ordering rule is correct and must not be inverted** (`BUILD-DEPENDENCY-GRAPH.md:290`: "Ship B7
first, then delete VD1. Never the reverse"). **If inverted, 4 TB ships with zero write-path integrity
check** — and the critical path already books the 1.17 h saving.

---

## 🟡 F5 — The vacuous check is CONFIRMED and worse than stated

Gate A's per-entry loop (`validate.py:399-431`) does `s3.head` for SIZE then set-membership on the
*declared* digest, never re-reading payload bytes. **CONFIRMED.**

**Extension:** `promote()` inherits it. `_destination_matches` — promotion's only `s3.hash_object` caller —
is gated on `payload.vendored` (`validate.py:2036`, `:2059`), and `vendored` =
`profile.startswith("vendored/")` (`:493`). This corpus is `pretrain_tokens/v1`, so **every integrity
re-check in `promote()` is skipped**: no source-ETag precondition, no destination re-hash, no post-copy
verification. 4 TB moves on `copy_object` + a CRC HEAD.

Real defences (`s3.py:571-575`): `CopyObject` recomputes CRC64NVME server-side, and the seal's
`crc64nvme` map gives fsck a same-length-overwrite detector for a HEAD. **Adequate for the copy hop.** But
with F4 (no checksum on the write into landing) the net is: **from `encode_batch`'s output to the sealed
object in `edullm-data`, no process ever re-reads a payload byte and compares it to an independently
computed digest.** §8.3a removes the **last** re-read while the write path is still unverified.

---

## 🟡 F6 — Ledger anchor audit

| anchor | verdict |
|---|---|
| `SHARD_TOKENS` 25,001,984 (`corpus.py:89`) | ✅ CONFIRMED — `3052 × 8192`. `1e12/25,001,984 = 39,997`; +4 controls = 40,001 |
| 384 vCPU → **12 children at 32 vCPU (not 16×8)** | ⚠️ **ARITHMETIC RIGHT, CONCLUSION BACKWARDS.** 384/32=12 ✅, but 12×32 → **33.2 h**; the 16×8 shape the CEO dismissed is closer to the measurement. Correct shape is **48 × 8** |
| `gpu-8xa100` largest working shape | ✅ CONFIRMED (`PLATFORM-INTEGRATION.md:19`; H100 CEs ENABLED, zero SUCCEEDED, `desiredvCpus: 0` — capacity not quota). **Caveat the ledger drops:** `:23` says the submission *form* may still refuse it; job history proves the CE works, not that the form accepts it. Off this corpus's critical path |
| build floor 9.96 h | ✅ arithmetic; ⚠️ **shape-dependent — F2** |
| ~6.2× ceiling | ✅ CONFIRMED (`89.3/14.4 = 6.20`) |
| 15.5 h path | ❌ F3 |
| 4.52× rate trap | ✅ CONFIRMED |

**Ledger correction:** `LEDGER.md:36` cites `validate.py:1943-1948` for "~2 round trips/object." That range
is `promote()`'s **docstring**, not its behaviour — the code does 3 (F1). Anchor A4
(`IMPLEMENTATION-PLAN.md:1362`) inherits it. Graded MEASURED-IN-CODE in both places; it is
**UNVERIFIED-from-prose**. `wallclock-audit.md:883` flagged this honestly; the flag never propagated.

---

## 🟡 F7 — "Nobody has measured this" — findings in their own right

1. **DCLM's throughput is unmeasured.** 72,615 is the *reservoir* mix, whose per-bundle spread was **3×**
   (`task-28-briefing.md:185`: stackv2-edu 916k vs finephrase 357k tok/s/container). At the low end `BUILD`
   is 2.5× worse again. `IMPLEMENTATION-PLAN.md:1687` carries the caveat; the graph and the ledger do not.
   **A "reservoir read as this corpus's" adjacent error — not the mix, its throughput.**
2. **12 concurrent `c7i.8xlarge` has never been demonstrated** (`desiredvCpus: 0`). Honestly flagged. And
   48 × 8-vCPU (F2's correct shape) is a *different* obtainability question, also undemonstrated.
3. **`tokenizers` is unpinned** (§7.6) — absent from `pyproject.toml` while `corpus_build.py:631` imports
   it. A version-string-is-not-a-code-identity instance **on the token ids themselves**. No task id.
4. **Promote had exactly one real data point and it was a failure** (`wallclock-audit.md:883`). All three
   "~1 min" citations are model, no measurement. F1 is the first use of the measurement.

---

## 🟢 F8 — PLAT's findings, attacked; they hold

| PLAT claim | verdict |
|---|---|
| Rule is **ENABLED** | **CONFIRMED as reported**; who/when correctly UNVERIFIED; suffix-only pattern verbatim in the quoted `EventPattern`. **Hazard understated:** target unversioned → rev 14 → per F1 **SIGKILLed mid-promote** on 40k objects, leaving an unsealed partial in an undeletable bucket. An accidental trigger can freeze *half* a `vN` |
| Airlock Deny **fires** | **CONFIRMED**; the scope note (`airlock-verification.md:44-49`) is exemplary — negative half MEASURED, positive half DERIVED and labelled unprovable read-only. Nothing to attack |
| Rev 14 / 14400 s / `--head-workers 16` | **CONFIRMED**, independently corroborated by `infra/DEPLOY.md:809` which PLAT does not cite. **Rev 14's 4 h timeout is exactly what F1 shows is insufficient** |
| Container `vcpus: 4 / memory: 8192` "correct shape for I/O fan-out" | **CONFIRMED as read; partially REFUTED as judgement.** At 301 MB/s aggregate through one 4-vCPU container this is likely near its network share, not a pure-latency regime. "Correct shape" is UNVERIFIED |

**Ledger correction:** `LEDGER.md:134` presents rev 14 / 14400 s as newly found; `infra/DEPLOY.md:809`
recorded it **2026-08-05**. A documentation-propagation failure, not a discovery — it does not change the
fact, but it changes how much to trust CLAUDE.md's other tables.

---

## Clean bills

- **No Maple citation.** `M20` / `1.279B` / `15.63:1` grepped across `docs/` and `artifacts/orchestration/`:
  zero hits outside the memory index. Compliant.
- **Denominator discipline is genuinely good where explicit** — `IMPLEMENTATION-PLAN.md:1548-1552` ("all
  three are `objects × 507.5 ms`, only the denominator differs") could not be faulted.
- `orchestrator-findings.md` F22/F2/F1 already record the 6-vs-8 correction and the withdrawn
  50,003,968 shard size. **Not re-refuted.**

---

## Process finding — the incremental-write rule is NOT enforceable for every subagent

AUDIT's first tool call, the write of this file, was **blocked by a harness guard** telling subagents to
return findings as text rather than write report files. Prior-wave agents (PLAT, ENG, DATA) wrote status
files successfully, so the guard is **not uniform** — it cannot be relied on either way.

**Amended convention, in force:** an agent writes incrementally **if it can**, and **returns full findings
as text regardless**. The **CEO persists every return to disk on arrival.** A guard-blocked write must be
reported, not worked around. This file is the first application.

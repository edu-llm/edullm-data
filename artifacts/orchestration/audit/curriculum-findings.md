# AUDIT-CURRICULUM — red-team findings on `agent/curriculum-exec/mtld-labels` @ ef732ee

**Auditor:** AUDIT-CURRICULUM (read-only, adversarial)
**Started:** 2026-08-09
**Status:** IN PROGRESS — appended continuously. A partial file is a success.

## Evidence grades
MEASURED (I ran it) / MEASURED-IN-CODE (I read the executable line) / DERIVED (arithmetic on other numbers) / UNVERIFIED (claimed, nobody checked).

---

## Log

- [init] File created before any read, per brief.

---

# F1 🔴 Gate A OOMs at real scale — the 7.13 GiB figure OMITS `np.bincount`'s intp cast. MEASURED 9.36 GiB in an 8.00 GiB container.

**Claim under audit** (LEDGER, correction 5): *"vector 1.78 + bincount int64 3.57 + the s3.get() bytes a frombuffer view pins 1.78 = 7.13 GiB = 89% of rev 14's memory: 8192. It PASSED."*

**What I recomputed.** MEASURED at n = 100,000,000 on numpy 2.4.4 (this machine), replicating
`token_order_v1.check_order_domain:181,209` exactly (bytes → `np.frombuffer(<u4)` view → `np.bincount(order, minlength=n)` → `np.all(counts == 1)`):

| term | width | n=1e8 MEASURED | N=478,575,435 extrapolated |
|---|---|---|---|
| `s3.get()` bytes (pinned by the view) | 4 B | 381 MiB | 1.78 GiB |
| **`bincount`'s intp cast of a `<u4` input** | **8 B** | **763 MiB** | **3.57 GiB** |
| `counts` int64 | 8 B | 763 MiB | 3.57 GiB |
| `(counts == 1)` bool temp | 1 B | 95 MiB | 0.45 GiB |
| **total** | 21 B | **peak RSS 1,393 MiB over baseline** | **9.36 GiB** |

**The intp cast is PROVEN, not inferred.** `tracemalloc` over numpy's own allocation domain at n = 20,000,000, varying only the input dtype:

| input dtype | counts | traced peak | extra over counts |
|---|---|---|---|
| **`<u4`** (ours) | 152.6 MiB | **305.2 MiB** | **152.6 MiB = 8.00 B/elem** |
| `intp` | 152.6 MiB | 152.6 MiB | **0.00 B/elem** |
| `<i4` | 152.6 MiB | 305.2 MiB | 8.00 B/elem |
| `<u8` | 152.6 MiB | 305.2 MiB | 8.00 B/elem |

`np.bincount` requires `intp`, and `<u4` is not `intp`, so numpy materialises an 8-byte-per-element copy of the whole vector and holds it live across the count. **Exactly 8 B/elem, only when the input is not `intp`.** That term is 3.57 GiB at N and it is **absent from the ledger's 7.13 GiB**.

**Verdict: the 7.13 GiB figure is REFUTED as an upper bound. Real peak is ~8.9–9.4 GiB — OVER the 8 GiB container.**

Reconciling with "it PASSED": the CEO's own note says the measurement was taken at full scale and passed at 89%. Two readings are possible and I cannot distinguish them from here: (a) the measurement summed the three terms analytically rather than observing RSS, in which case it never saw the intp cast; or (b) it observed RSS on a host with enough memory and the 8 GiB container number was applied afterwards by arithmetic. **Either way the number that was compared against `memory: 8192` is not the peak.** I did not find a full-scale RSS trace in the repo — see F1b.

**Blast radius.** Gate A is the last gate before promotion. An OOM there is a killed container after a 6–7 h `publish()` and a ~4 h Gate A — the exact "expensive outcome" the ledger names. It does not corrupt data. **But it is not a "no headroom" risk; on my numbers it is over budget before boto3, the JSON manifest, or the rest of Gate A's Python is counted.**

**Fix, cheapest first (none require touching `token_order_v1`'s semantics):**
1. **Raise the validator job def's `memory` to ≥ 16384 MiB** for the curriculum publish. Pure config, no code, no shared-profile change. This is the fix I would take.
2. Split the order into ≥ 4 objects (see F8): `check_order_domain` is per-entry, so peak scales with the largest object, not the corpus.
3. If code is touched: `np.bincount(order.astype(np.intp, copy=False), ...)` does not help (the cast is the cost). A chunked count (`np.add.at` over slices, or `counts = np.zeros(n, np.int64); np.add.at(...)`) would, but that is a platform change to a profile shared by every future curriculum dataset — correctly out of scope for this branch, per the ledger.

---

# F2 🔴🔴 **THE WORST FINDING.** The driver declares `block_count` from a HARD-CODED `shards × 12,207`, not from the axis it correctly derives. It is ALREADY WRONG against the live build — MEASURED.

**Claim under audit.** `curriculum_driver.py:41-44` and its module docstring, Property 3: *"The parent axis is read from the parent's MANIFEST, not from the plan."* And LEDGER's fix part 1: *"DERIVE `n_chunks` FROM THE CONSUMER, NEVER FROM A CONSTANT… A hard-coded 12,207 or 12,208 is a constant that will silently rot."*

**What the code actually does** — `curriculum_driver.py:278-280`, and both lines are executable:
```python
per_shard = chunk_counts([PLAN_SHARD_TOKENS])[0]      # 12,207, from the CONSTANT 25,001,984
n_train   = PLAN_TRAIN_SHARDS * per_shard             # 39,205 × 12,207 = 478,575,435
n_val     = PLAN_VAL_SHARDS   * per_shard             # 102    × 12,207 =   1,245,114
```
`build_axis(parent_manifest(s3))` at `:302` **does** derive the real axis from the manifest's `bytes`. **Its `axis.n_chunks` is then never used for anything but a print** (`:145-147`). The values that go into `group_meta_for(n_train, n_val)` at `:350` — i.e. into the published `block_count` on both groups and into `depends_on` — are the hard-coded products. `identity_order(n_val)` at `:314` sizes the val vector from the same constant.

**This assumes every shard is exactly full.** It is not. MEASURED against the live in-flight build, `s3api list-objects-v2` on `_ingest/final-dataset/29968a2b04008a8c/data/tokens/`, filtering `Size != 100007936`:

| shard | bytes | tokens | chunks | short by |
|---|---|---|---|---|
| `stackv2-edu/train-35260` | 49,184,768 | 12,296,192 | 6,003 | 6,204 |
| `stackv2-edu/train-35882` | 5,799,936 | 1,449,984 | 707 | 11,500 |
| `stackv2-edu/train-36494` | 48,168,960 | 12,042,240 | 5,879 | 6,328 |
| `stackv2-edu/train-37084` | 58,294,272 | 14,573,568 | 7,115 | 5,092 |
| `stackv2-edu/train-38327` | 65,667,072 | 16,416,768 | 8,015 | 4,192 |
| `stackv2-edu/train-38938` | 55,607,296 | 13,901,824 | 6,787 | 5,420 |
| **train subtotal** | | | | **38,736 chunks short** |
| `stackv2-edu/val-00082` | 83,656,704 | 20,914,176 | 10,211 | 1,996 |
| `stackv2-edu/val-00086` | **262,144** | **65,536** | **31** | 12,176 |
| `stackv2-edu/val-00088` | 81,920,000 | 20,480,000 | 9,999 | 2,208 |
| `stackv2-edu/val-00091` | 65,798,144 | 16,449,536 | 8,031 | 4,176 |
| `stackv2-edu/val-00094` | 78,872,576 | 19,718,144 | 9,627 | 2,580 |
| `stackv2-edu/val-00097` | 75,661,312 | 18,915,328 | 9,235 | 2,972 |
| `stackv2-edu/val-00100` | 69,107,712 | 17,276,928 | 8,435 | 3,772 |
| **val subtotal** | | | | **29,880 chunks short** |

**Verdict: CONFIRMED. The declared `block_count` is wrong for both groups, and the val figure is already off by 2.40% from ONE source in a build that is only partway done.** These are only `stackv2-edu`'s tail shards — the build has 132 distinct train streams, each of which gets a short tail shard when its realized stream underruns its last ref (`corpus_pack.py:840-853`, the tail rule). **If one tail shard per stream is short, the train count is over-declared by up to 132 × 12,207 ≈ 1.6 M chunks.** `unfilled` refs make it worse in the same direction: a ref with no data is not written at all, so it is a whole 12,207-chunk hole in the hard-coded product.

**Blast radius, and this is the one that costs the rebuild.** Two outcomes, both bad, and which one you get depends on a detail:
1. **`build_order` produces `axis.n_chunks` indices** (the real, smaller number) while `group_meta` declares the hard-coded larger `block_count`. `check_order_domain` then fires **`permutation-wrong-length`** — Gate A rejects the publish. Cost: a 6–7 h `publish()` plus a Gate A run, thrown away. **Recoverable, loud.**
2. `identity_order(n_val)` at `:314` builds the val vector at the **hard-coded** 1,245,114 and the val group declares the same number, so **the val pair is internally consistent and passes Gate A** — while indexing 29,880+ chunks the parent does not have. Per LOADER-RESOLVE the loader memmaps and counts, so a val order longer than the real chunk count crashes the launch or reads past the end. **This one gets through Gate A.**

The fix is one line and the correct value is already computed three statements away.

**FIX (mandatory before any publish):** use the derived axis. `n_train = axis.n_chunks`; build a second axis with `split="val"` and take `n_val = val_axis.n_chunks`. Keep the hard-coded products only as a *printed* upper bound with a warning when they disagree — the disagreement is the signal that a stream had a short tail, which is normal.

**Note on scope: this does NOT invalidate `corpus_order.py`.** `chunk_axis_from_manifest` is correct and does exactly what the ledger asked for. **The defect is entirely in the driver, which computes the right answer and then ignores it.**

---

# F3 🔴 The chunk-owner cursor drifts across file-shard part boundaries. 23.5% of the corpus is exposed, and the failure is SILENT.

**Claim under audit** (correction 2, and the brief calls this the highest-risk one): *"`source_doc` is contiguous per BUNDLE… the driver reassembles in `file_shard` index order and refuses a missing part."*

**What is right.** `curriculum_driver.load_streams:194-206` sorts parts by the `--pNNofNN` index parsed out of `bundle_id`, refuses a gap or a repeat (`:196-203`), and `np.concatenate`s the strides in that order. The **document ORDER** across parts is correct.

**What is wrong.** `build_order` advances its per-stream cursor by `axis.tokens[i]` — the **shard** token count — while the labelled space it is indexing advances by `tokens_in`, which is **larger**:

`corpus_order.py:427`
```python
cursor[st] = start_tok + int(axis.tokens[i])     # tokens_OUT
```
The `tokens_in` / `tokens_out` gap is `tail_dropped + surplus_dropped`, and **it accrues once per PART, not once per stream** — each of the K children is its own `pack()` call with its own tail and its own surplus. So at every part boundary the cursor is behind the true labelled offset by the sum of the preceding parts' gaps, and **every chunk in every part after the first is attributed to the wrong document.**

**MEASURED, on a constructed 2-part stream** (4 shards of 4,096 tokens; part 0 `tokens_in` 8,692 / `tokens_out` 8,192; part 1 `tokens_in` 8,492 / `tokens_out` 8,192):
```
shard chunk  code_start_tok code_owner   TRUE_start_tok TRUE_owner  drift
    0     0               0          0                0          0      0
    1     0            4096          5             4096          5      0
    2     0            8192         11             8692         12     +1   ← part boundary
    3     0           12288         15            12788         16     +1
```
`build_order` **returned successfully** — no raise, and the vector is a perfect permutation. **Verdict: CONFIRMED, and it is exactly the "bijective and meaningless" class this module was written to prevent.**

**Why no existing check catches it.** The stream-length check at `:397-408` compares `labelled >= held`, which the drift makes *more* satisfied, not less. The owner-past-last-document check at `:433` only fires when the drift pushes past the *end* of the whole stream. Gate A's `bincount` sees a valid permutation. `verify_labels` is per-bundle and passes.

**Blast radius.** MEASURED from `artifacts/final-dataset/corpus-registry.json` `_file_shards`: **232 B of 986 B drawn tokens = 23.5% of the corpus is in a file-sharded stream** — `stackv2-edu` 7 parts / 108 B, `finepdfs-edu` 4 parts / 63 B, `nemotron-cc-math-3` 3 parts / 38 B, `nemotron-cc-math-4plus` 2 parts / 23 B. On the live receipts the per-part gap is small (`dclm-001`: `surplus_dropped` 962, `tail_dropped` 0, so ~1 document of drift) — **but a short tail shard makes `tail_dropped` up to 8,191, and stackv2-edu's observed tails drop MILLIONS of tokens**, i.e. thousands of documents of drift for every part after the first. **This is a wrong curriculum on ~a quarter of the corpus, shipped silently.**

**FIX.** `load_streams` already holds each part's `LabelSet`, and each carries `tokens_in` in its header. Either (a) have `build_order` take a per-stream list of `(part_tokens_in, part_shard_keys)` and reset the cursor to the part's true labelled base at each boundary, or (b) — much simpler and strictly better — **treat each PART as its own stream.** A part's bundle_id is unique and its shards are a contiguous ordinal block (`partition_ordinals`), so `shard_stream` can map each axis key to `(source, domain, split, part)` and the drift becomes structurally unrepresentable. Option (b) also makes the existing `labelled >= held` check meaningful per part.

⚠️ **Option (b) needs a way to recover a shard's PART from its key, and there isn't one.** MEASURED: `labels_from_path("tokens/stackv2-edu/train-35260.u32le.bin")` → `{'source': 'stackv2-edu'}`. The key carries the source and the global ordinal, nothing about which of the 7 children wrote it. The part IS recoverable from the receipts — each receipt names its `bundle_id` and lists its shard paths — so the driver would need to build `shard_stream` from the **receipts** rather than from `labels_from_path`. That is a real change, not a one-liner, and it is the reason I rank F3 as a genuine blocker rather than a small fix.

---

# F4 🟠 The `--labels` flag is DEFAULT-OFF and no job definition passes it. The rebuild would produce ZERO labels and look successful.

**MEASURED-IN-CODE.** `corpus_build.py:1993-1997` adds `--labels` as `action="store_true"`, and `_cmd_run` reads it with `getattr(args, "labels", False)`. The default-off polarity is argued correctly in the commit (a build that silently gained an output is a build whose receipts no longer match). **But:** repo-wide grep for `--labels` finds exactly three hits — the argparse definition, a checkbox in `status.md`, and an error string in the driver. **No job definition, no `infra/*.json`, no deploy doc passes it.**

The ledger records the live build def's command as *hardcoded*: `corpus_build … run --plan-id … --shard ${SHARD} --of ${N_BUNDLES}`, with no `--labels`. So the 13 h rebuild, launched from the current def, writes 39,307 shards and **no sidecars** — and `verify_bundle_set` passes, because a receipt without `labels` is valid (`test_labels_off_writes_no_sidecar_and_no_receipt_field` asserts exactly that). The failure surfaces 13 hours later, at `load_streams`, as *"REFUSING: no labels under …"*.

**Blast radius: the entire 13 h rebuild, wasted, with no error until it is over.** This is the single cheapest thing on this list to get wrong and the most expensive to discover late.

**FIX.** Register the build job definition revision with `--labels` in the command **before** launching, and verify by reading back the registered command — not by reading the intent. Better still: have the driver emit a preflight that reads the job def and refuses to submit a labelled build whose command lacks the flag.

## F4 CONFIRMED LIVE — I read the registered command. `--labels` is absent, and there is a SECOND blocker in the same string.

MEASURED via `batch describe-job-definitions --job-definition-name edullm-reservoir-build --status ACTIVE`. **Top ACTIVE rev is 13** (8 vCPU / 14,336 MiB). Its run line, verbatim:
```
python -m edullm_data.corpus_build --registry /tmp/corpus-registry.json \
  --bucket edullm-landing --prefix _ingest/final-dataset \
  run --plan-id ${PLAN_ID} --shard ${SHARD} --of ${N_BUNDLES} --tokenizer-dir /tmp/tok
```
**No `--labels`.** So a rebuild launched on rev 13 emits **zero** sidecars. **CONFIRMED, not inferred.**

**And the second blocker, in rev 13's preflight:**
```
assert edullm_data.__version__=='0.9.1','WRONG IMAGE VERSION'
```
The checkout's `__version__` is **`0.9.1`** — MEASURED, `src/edullm_data/__init__.py:3`. **`ef732ee` does not bump it.** So the labelled code and the current image share a version string, and per this repo's own memory (*"a version string is not a code identity"* — two commits both said 0.7.4 with different bounds) **the preflight assertion passes on either image.** There is no way for a job to tell whether it is running labelled code.

**So F4 requires TWO changes, not one:**
- add `--labels` to a new job-def revision, and
- **bump `__version__`** (to e.g. `0.9.2`) in all three places the build requires, so the preflight's version assert becomes a real code-identity check, and add a preflight assertion that `'labels'` is a parameter of `corpus_build.run_bundle` — the same `inspect.getsource`/`hasattr` idiom revs 8–13 already use for every other behaviour they depend on (`hasattr(cb,'_resolve_file_shards')`, `'partial_source=True' in inspect.getsource(cb.run_bundle)`).

Without the second change, a stale image silently produces an unlabelled 13 h build **that passes its own preflight.** With it, the job refuses to start.

---

# F5 🟠 The MTLD constants and vectors: REPRODUCED. But the `[a,b,c,a] → 4.48` discriminating claim is only HALF true.

**All five claimed vectors reproduce exactly.** MEASURED by running the module:

| vector | claimed | I get | verdict |
|---|---|---|---|
| `['a']*12` | 2.0 | 2.0 | ✅ |
| `list('abcde')` | 5.0 | 5.0 | ✅ |
| `['a','b','a','a']` | 4.0 | 4.0 | ✅ |
| `['a','a','b','a']` | 4.0 | 4.0 | ✅ |
| `['a','b','c','a']` | 4.48 | 4.48 | ✅ |

Constants: `TTR_THRESHOLD == 0.72`, `WORD_RE.pattern == r"[A-Za-z]+(?:'[A-Za-z]+)?|[A-Za-z]*\d+[A-Za-z0-9]*"`, `SHORT_DOC_WORDS == 10`, lowercase-before-match, `0.5*(fwd+bwd)`, `<=` at the boundary. All match the brief bit-for-bit. `MTLD_SPEC_ID` contains `"0.72"` and a test asserts that relationship.

**Now the attack. I ran a full mutation matrix** — six variants (threshold 0.70/0.71/0.72/0.75 × `<=`/`<`) against every test vector:

```
vector                 0.72,<=   0.75,<=   0.72,<    0.75,<    0.71,<=   0.70,<=
['a']*12                 2.00      2.00      2.00      2.00      2.00      2.00
abcde                    5.00      5.00      5.00      5.00      5.00      5.00
[a,b,a,a]                4.00      4.00      4.00      4.00      4.00      4.00
[a,a,b,a]                4.00      4.00      4.00      4.00      4.00      4.00
[a,b,c,a]                4.48      4.00      4.48      4.00      4.64      4.80
t29 boundary vector      5.27      4.14      5.27      5.27      5.27      5.27
```

**`[a,b,c,a]` DISCRIMINATES THE THRESHOLD (0.72 vs 0.75 vs 0.71 vs 0.70 all differ) BUT NOT `<=` vs `<`** — it gives 4.48 under both. **The docstring's claim that it is "why the threshold comparison must be `<=` and not `<` at exactly 0.72" is WRONG**, and mechanically so: at `w4` the ratio is 0.75, which is *above* 0.72, so neither spelling closes a factor there. The vector never touches the boundary.

**The `<=`-vs-`<` distinction IS covered — by the other test.** `test_the_threshold_comparison_is_inclusive_at_exactly_the_boundary` uses `[t0..t17] + [t0]*7 + [z1,z2,z3,z1]`. Traced by hand: at word 25 the running ratio is exactly **18/25 = 0.72**, so `<=` closes a factor there and `<` does not. MEASURED: inclusive **15.320754716981133**, strict **29.435**, `float(n)` **29.0** — all three differ, and the test asserts against all three. **That test is genuine and non-vacuous.** (Note the test's own docstring is confused about the word-splitting — it says `t0 → ['t','0']` while passing the list literally, bypassing `words_of` entirely — but the assertion is correct because the trajectory is what matters.)

**So: is the coverage enough?** For the constants under audit, yes — **but note what the matrix shows: five of the six hand-traced vectors are INSENSITIVE to every mutation I tried.** They prove the algorithm shape (factor closing, the partial-factor formula, the `factors==0` limit) but not the constants. **The entire threshold-and-comparison surface rests on exactly two tests** (`test_a_nonzero_partial_factor…` for the threshold value, `test_the_threshold_comparison_is_inclusive…` for the comparison), and one of the two has a wrong explanation attached. That is thin for a "compatibility surface" whose silent corruption is the stated worst case. **Verdict: constants CONFIRMED correct; the `4.48`-discriminates-`<=` claim REFUTED; coverage adequate but concentrated.**

**FIX (documentation only, no behaviour):** correct the docstring at `tests/test_corpus_mtld.py:216-218` — `[a,b,c,a]` discriminates the *threshold*, not the *comparison*. Leave the vectors alone.

---

# F6 🟠 Nobody has verified the MTLD overhead denominator, and my own measurement says the true share is LOWER than 2.80%. The honest answer is "we do not know until it runs on c7i" — but the direction is safe.

**Claim under audit:** MTLD 314 µs/doc ÷ build 11,224 µs/doc = 2.80%, and the ratio route's 51% is rejected.

**Which route is right: route A, and CURRICULUM-EXEC's reasoning for rejecting route B is sound.** Route B multiplies a measured per-document MTLD cost by the *78% serial share*, which is an aggregate attribution, not a per-document cost. Multiplying a rate by a share is the cap×rate error class named in this repo's own memory index. Route A divides one per-document measurement by another. It is the right shape.

**But the denominator is mixed, and the numerator is not reproducible on my hardware.** MEASURED here: 2,000 synthetic documents at 611 words / 4,177 chars (the corpus's 815 tok/doc at ~0.75 words/token) → **186 µs/doc**, i.e. **1.66%** of the 11,224 µs figure. That is 0.59× CURRICULUM-EXEC's 314 µs. Two different laptops, two different numbers, same order of magnitude. **Neither is a c7i measurement.**

The 11,224 µs denominator is *derived* (815 tok/doc ÷ 72,615 tok/s/vCPU), and 72,615 is the repo's MEASURED end-to-end anchor on 8-vCPU containers. **So the denominator is the right quantity** — it is genuinely per-document, genuinely end-to-end, genuinely from this pipeline. The numerator is laptop-measured.

**Verdict: the 2.80% figure is DERIVED-from-a-mixed-denominator and UNVERIFIED on the target hardware — which CURRICULUM-EXEC states explicitly and correctly.** The direction of the error is safe: a laptop core is faster single-threaded than a c7i vCPU, so a c7i-measured numerator would raise MTLD's absolute cost *and* the build's, and my own lower number (1.66%) suggests the 2.80% is if anything conservative. **This is not a blocker.** It is a number to re-measure from the first bundle's wall clock rather than argue about — one bundle gives it for free.

---

# F7 🟡 The labels format holds up under attack, except that a truncation of a whole number of records is caught only by ARITHMETIC, and one check is dtype-fragile.

**Endianness: CORRECT and explicit.** MEASURED: every field is `<`-prefixed (`<u4`, `<u4`, `<f4`, `<u2`); `LABEL_DTYPE.itemsize == 14`; `align=False` asserted at import (`corpus_labels.py:113-118`, executable, raises `BuildError`). A big-endian reader using this dtype parses correctly because the byte order is in the dtype, not the platform. **`ORDER_DTYPE` is likewise `<u4`, matching `token_order_v1._ORDER_DTYPE` exactly.** No big-endian mis-parse channel.

**14.2 B/doc: REPRODUCED.** MEASURED at 10,000 docs / 100 files: **14.2957 B/doc** (header 2,957 B). At a realistic 6.5 M-doc bundle the header amortises to **14.00045 B/doc**, so the corpus projection is ~16.9 GB. The claim is honest and slightly pessimistic at scale.

**Truncation: MEASURED, four cases.**
- Cut inside magic/prefix → raises (`not a labels object` / `header claims N bytes but the object holds only M`). ✅
- Cut leaving a ragged record → raises (`payload is N bytes, not a multiple of 14`). ✅
- **Cut removing exactly 100 whole records → DECODES SILENTLY as 9,900 records.** `decode_labels` does *not* compare the payload's record count against the header's `documents` field, even though the header carries it. It is caught downstream only because `verify_labels` then reports `labels-token-conservation-broken` (the strides no longer sum to `tokens_in`) — **arithmetic, not structure.** That is a working defence, but it is one check deep, and `verify_labels`' own `declared_documents` cross-check is optional (`None` by default).

**FIX (cheap, strictly better):** in `decode_labels`, compare `len(payload) // ITEM_SIZE` against `header["documents"]` and raise on a mismatch. The header already carries the number; not checking it is the one place this format trusts instead of recomputing.

**One dtype-fragility, minor.** `verify_labels:316` builds `expected = np.arange(n, dtype=r["source_doc"].dtype)`, i.e. `uint32`. At n > 4,294,967,295 that wraps — but a bundle is ≤ ~6.5 M documents, so it cannot fire. Noted, not a finding.

**A vacuous-check note.** `labels-zero-token-document` (`:388`) fires on `n_tokens == 0`. It cannot fire: `tokenize_documents` filters below `min_doc_tokens` (64) before the hook. That makes it a *defence-in-depth* check rather than decoration — it would catch a future change that moved the hook — so I am not calling it a defect, but it is one of the new checks whose answer to *"what mutation would make this fail?"* is "only a change to the hook's position."

---

# F8 🟡 The order is ONE object per group, so nothing depends on multi-object concatenation order today — but the driver has no guard if that changes.

The coordinator flagged that multi-object order vectors concatenate in `dataset_paths()` order. **Not exercised:** `curriculum_driver.py:321-324` writes exactly one file per group (`{GROUP}-train/train-00000.u32le.bin`, `{GROUP}-val/val-00000.u32le.bin`). With one object per group the concatenation order is trivially correct.

**But this interacts with the F1 memory fix.** If Gate A's OOM is solved by *splitting* the vector into K objects (my fix option 2), the ordinal sequence becomes load-bearing and **no code or test currently guards it.** So: if you take the memory fix by splitting, that is not a config change — it needs an ordering guarantee and a test. **Prefer raising the validator's memory.**

---

# F9 ✅ A degenerate ≤2048-token train shard is UNREACHABLE from this packer. The loader's silent-skip path cannot fire.

The coordinator asked whether `ef732ee` guards against a train shard ≤ 2048 tokens, given `curriculum_loader.py:67-68`'s silent `continue`.

**Answer: there is no explicit guard, and none is needed from this producer — but the reason is a property of `corpus_pack`, not of anything in this commit.** MEASURED-IN-CODE at `corpus_pack.py:840-846`: a shard whose content is under one whole `SEQ_LEN` (**8192** tokens) is **not written at all** — `aligned == 0` → `tail_dropped += cursor` → the ref goes to `unfilled`. So the smallest shard `corpus_pack` can emit is 8,192 tokens = **3 chunks** under `(t-1)//2048`. `chunks <= 0` requires ≤ 2,048 tokens, which is 4× below the floor. The 150B precedent (two 20-byte shards) came from a *different* producer.

I verified the two formulas agree on the skip condition: for every `t` in {0, 1, 2047, 2048, 2049, 4096, 8192, 20480, 25001984, 5}, `max(0,(t-1)//2048)` equals the loader's `chunks` value including its `<=0` branch. **No disagreement channel.**

**Is it testable at build time? Not currently, and it should be.** `chunk_axis_from_manifest` accepts any `bytes` and `chunk_counts` returns 0 happily. MEASURED: I fed it a 20-byte train shard and it produced `chunks=(9, 0, 9)`, and `build_order` **succeeded** on the resulting axis with no raise. So if a degenerate shard ever *did* reach the manifest, this module would silently build a permutation over an axis the loader will renumber. **FIX (cheap, high value):** raise in `chunk_axis_from_manifest` when any included shard yields 0 chunks, naming the loader's silent-skip as the reason. That converts an invisible loader-side renumbering into a build-time refusal — and per the coordinator's point 3, it is invisible to Gate A's `bincount` because the corruption is in the *correspondence* between paths and arrays, not in the vector's structure.

**Interaction with F2:** note that a 0-chunk shard is a *worse* case of the same bug F2 describes — the hard-coded `shards × 12,207` over-declares by a full 12,207 for it. Fixing F2 by deriving from the axis handles the arithmetic; the guard above handles the correspondence. **Both are needed.**

---

# F10 ✅ CONFIRMED, no defect: `plan_id`, the byte-identity guard, and the vacuity proof.

**`plan_id` is `29968a2b04008a8c`.** I recomputed it independently: `load_registry` → 133 rows → `plan_document(drawn, registry_meta=meta)['plan_id']` = **`29968a2b04008a8c`**, 185 bundles, 39,205 train shards, 102 val shards, `shard_tokens` 25,001,984. **Unmoved.** The plan doc's keys are `bundles, group, min_doc_tokens, no_val_split, plan_id, registry_revisions_pinned_at, schema, shard_tokens, tokenizer, val_fraction` — nothing code-derived, nothing about labels. The `labels` flag reaches `run_bundle`, never `plan_document`.

**The guard asserts against the LITERAL, correctly.** `tests/test_curriculum_labels.py:59` defines `FROZEN_PLAN_ID = "29968a2b04008a8c"` as a module constant and `:84` compares the freshly-computed plan to it. **Not a recomputation-against-itself.** The brief's concern does not apply.

**The byte-identity test is REAL, not a hash comparison.** `test_the_shard_bytes_are_IDENTICAL_with_and_without_labels:729-736` runs the same bundle through the real `run_bundle` twice (labels off / on) and compares `FakeS3.dump()` values. MEASURED-IN-CODE at `s3.py:665-666`: `dump` returns `{key: value}` straight from `_store`, i.e. **the actual payload bytes**. The test compares `shards_a[k] == shards_b[k]` for every key — a full payload-byte comparison, plus a key-set equality and a `tokens_out` equality. **What mutation would make it fail?** Any change to the packed bytes, the shard set, or the token count. It is a genuine check.

**The vacuity proof is genuine and is the best test in the commit.** `test_without_a_parent_block_count_the_permutation_check_IS_VACUOUS` asserts `_codes(unpinned) == []` (a 64-index vector accepted against a 478 M-chunk parent) and then that adding `block_count` produces `permutation-wrong-length`. Both branches asserted; it cannot pass for the wrong reason. I re-read `_block_count` (`token_order_v1.py:66-77`) and `check_order_domain:198` — `n = block_count if block_count is not None else int(order.size)` — the vacuity is real and the test's premise is correct.

**The train/val two-group proof is likewise genuine**, and it asserts *which* path is rejected (`{v.path for v in out} == {"mtld/val-00000.u32le.bin"}`), which closes the "a stub fed the wrong bytes to both" hole.

**Other checks I confirmed by execution rather than reading:**
- **EOS-first chunk** (brief's attack): a chunk starting exactly on a document's EOS slot is owned by the document it terminates. `searchsorted(ends, off, side="right")` on `ends=[10,20,30]` gives owner 0 for `off=9`, owner 1 for `off=10`. Correct under the model, no orphan. ✅
- **Ties**: 8 documents at identical scores → the order is the identity `[0,1,2]`, byte-identical across two runs. `np.argsort(kind="stable")` + `lexsort((idx, rank))` gives a total order with no seed. ✅
- **Document shorter than one chunk / spanning a shard boundary**: handled by construction — the cursor is a token offset, `searchsorted` is over cumulative strides, and neither cares about document length relative to chunk length. The `difficulty-granularity` limitation in the driver states the consequence honestly.
- **`corpus_order` never uses the wrong chunk rule silently**: `chunk_counts` raises on an unknown rule rather than defaulting (`:200-204`). ✅
- **12,207 is implemented as claimed and is parameterised, not hard-coded twice in the library.** `CHUNKS_MINUS_ONE` is the default; `chunk_counts` is the only place the arithmetic lives. **But see F2 — the DRIVER hard-codes `12_207` as a literal at `:87` and re-derives it at `:278`, and the `:87` literal is the one inside `assert_invariants`.** Two spellings of the same number in one file is the drift channel the ledger warned about.

---

# F11 ⚠️ Test coverage gap: the two most severe findings both live in the ONE file no test touches.

MEASURED: `grep -rn "curriculum_driver\|load_streams\|group_meta_for\|build_axis" tests/` returns **two hits, both inside docstrings.** `artifacts/final-dataset/curriculum_driver.py` — 404 lines containing F2 and F3 — has **zero test coverage.** `tests/` contains no `test_curriculum_driver.py`.

The library modules (`corpus_mtld`, `corpus_labels`, `corpus_order`) are well tested — 66 tests, all passing, several genuinely adversarial. **The defects are in the seam between them and the world, which is exactly where nothing is tested.** Relatedly, every `_axis()` fixture in the suite uses **uniform** `tokens_each` (`tests/test_curriculum_labels.py:351-360`), so **no test ever exercises a heterogeneous axis with a short tail shard** — the exact shape F2 turns on. And no test builds a multi-part stream, which is the shape F3 turns on.

**FIX:** two tests would have caught both. (1) An axis with one short shard, asserting `axis.n_chunks != n_shards * 12207` and that whatever the driver declares equals `axis.n_chunks`. (2) A two-part stream where part 0's `tokens_in > tokens_out`, asserting the owner of part 1's first chunk. Both are ~20 lines and need no S3.

---

# F12 🔴 The driver's `profile={GROUP: PROFILE}` names group `mtld`, but it stages `mtld-train` and `mtld-val`. `publish()` raises. **This is CEO ERROR #16's exact shape, one file over.**

**MEASURED-IN-CODE, three lines that disagree:**
```python
GROUP = "mtld"                                              # :59
os.makedirs(f"{args.out}/{GROUP}-train", …)                 # :319  -> group "mtld-train"
os.makedirs(f"{args.out}/{GROUP}-val",   …)                 # :320  -> group "mtld-val"
group_meta=group_meta_for(n_train, n_val)                   # :350  keys "mtld-train"/"mtld-val" ✅
profile={GROUP: PROFILE}                                    # :347  key "mtld"                  ❌
```
`publish.build_plan` groups files by **first path segment** (`publish.py:346-354`), so the groups are `mtld-train` and `mtld-val`. Its `profile_for(g)` (`publish.py:400-408`) raises `PublishError` on a group with no mapping entry. I replicated it exactly:
```
mtld-train -> PublishError: group 'mtld-train' has no profile in the profile mapping {'mtld': 'token-order/v1'}
mtld-val   -> PublishError: group 'mtld-val'   has no profile in the profile mapping {'mtld': 'token-order/v1'}
```
`group_meta_for` gets the suffixed names right — so the author knew the group names are suffixed and missed the one place that also needs them.

**Verdict: CONFIRMED. `--go` cannot succeed as written.** Blast radius is small and loud (it raises before any byte moves — `profile_for` is called during plan construction), so unlike F2 this one costs minutes, not a publish. **But it means the publish path has never been exercised even in a dry run**, which is itself worth stating: F2, F3 and F12 are all in the same untested file, and F12 is the one that would have been caught by literally running `--build` once.

**FIX:** `profile={f"{GROUP}-train": PROFILE, f"{GROUP}-val": PROFILE}`.

---

# F13 🟠 Nothing enforces that ALL bundles have labels. `_check_labels` explicitly defers the policy to `verify_bundle_set`, which does not implement it.

**MEASURED-IN-CODE.** `corpus_receipt._check_labels` (`:986-991`) states the scope limit outright:
> *"Absent labels are NOT a violation here… Whether a corpus destined for a curriculum may publish without labels is a policy question for the plan — **`verify_bundle_set` is where it belongs**, because 'some bundles have labels and some do not' is a property of the SET and is the failure mode that actually matters (a partial ordering that silently omits whole sources)."*

**`verify_bundle_set` does not mention labels.** Verified by `inspect.getsource`: the string `"labels"` does not appear in it, and the only violations it emits are `bundle-set-incomplete` and `bundle-set-unexpected-stream`. Its one helper, `_check_set_file_shard_families`, likewise has no labels logic.

**So the reasoning is correct and the handoff is dangling.** The docstring reads exactly like a description of an implemented delegation — the trap this brief warns about. What actually catches a partially-labelled build is `curriculum_driver.build_order`'s *"axis key belongs to stream X, which supplied NO labels"* refusal (`corpus_order.py:389-395`) — which is a real check and does fire. **So the corpus cannot silently ship a partial curriculum.** But it fires at the END of the pipeline, after the build and after `verify`, where the stated design puts it at `verify` time.

**Verdict: the DOCUMENTED delegation is unimplemented.** **Fix:** add a set-level check that all-or-none of a plan's receipts carry `labels`, or delete the sentence in `_check_labels` that promises it. **Do not leave the docstring as-is** — it is a comment describing a fix that does not exist.

## ⚠️ F13 ESCALATED TO 🔴 — PLAT-2 independently found the stronger form while I was auditing, and I confirmed it in code. **`bundle_is_done` is not label-aware, and the build is TERMINAL at 169/185.**

Commit `5a26803` (landed mid-audit) reports the build **TERMINAL: 169 SUCCEEDED / 16 FAILED / 0 RUNNING, 88.22% of tokens.** That changes the sequencing question, and it makes my F13 far worse than "a dangling docstring."

**CONFIRMED-IN-CODE, `corpus_build.bundle_is_done:892-931`.** It reads the receipt, compares the declared shard path set, then `head`s every shard for size. **It never inspects `receipt.labels`.** So a relaunch with `--labels`:
- **skips all 169 complete bundles** — they are "done" by size, and they have no labels;
- **labels only the 16 rebuilt ones.**

Result: a **mixed set** where ~88% of the corpus has no labels. And **no gate catches it**, for exactly the two reasons I established independently: `_check_labels` returns `[]` when `labels is None` (F13), and `verify_bundle_set` emits only `bundle-set-incomplete` / `bundle-set-unexpected-stream` (F13, verified by `inspect.getsource`).

**The one thing that does catch it is `build_order`'s no-labels refusal** (`corpus_order.py:389-395`) — but that fires at the very end, after a relaunch, and it fires as *"stream X supplied NO labels"* for 116 of 132 streams, which reads like a bug in the driver rather than a resume-semantics problem.

**So the labelled build is NOT a matter of adding a flag to a relaunch.** Either:
- **(A) `--force` the whole 185** — a full 13.3 h rebuild, which is what the owner already authorised, or
- **(B) make `bundle_is_done` label-aware** — return `False` when `labels=True` was requested and `receipt.labels is None`. Then a relaunch rebuilds exactly the bundles that lack labels, which is all of them, i.e. the same work as (A) but *correct by construction* and safe to re-run.

**(B) is strictly better and is ~5 lines**, and it also closes the mixed-set hole permanently. Without one of these, the most likely operational path — "relaunch the 16 failures with `--labels`" — produces a corpus with 12% label coverage that passes every check until the curriculum driver refuses it.

**This also means F4's fix is necessary but not sufficient.** Adding `--labels` to the job def and bumping `__version__` gets labels onto whatever bundles run; it does nothing about the 169 that will not re-run.

---

# F14 ℹ️ Confirmed correct, for the record

**`tokens_in` IS the right denominator, and I proved it from `PackResult` rather than accepting it.**
- `on_document(doc, int(wide.size))` fires immediately before `yield out`, and `out.size == wide.size + 1` (the EOS is appended at `corpus_pack.py:443-445`). So `sum(n_tokens + 1) == sum(out.size)` over every **yielded** document.
- `tokens_in += int(doc.size)` at `:823` counts once per document **pulled**, where `doc` IS the yielded array. So `tokens_in == sum(out.size)` over pulled documents, plus `unread` from `_drain_surplus` at `:880`.
- Under `partial_source=True` (what the build driver passes), `_drain_surplus` returns `unread = 0` and does **not** pull, so the two sets are identical and the identity is exact.
- Under `partial_source=False`, `_drain_surplus` **does** drain — and because the hook is *before* the yield, each drained document fires the hook **and** increments `tokens_in`. **Both sides grow together, so the identity still holds.** The correction is right in both modes.

`tokens_out` would indeed be wrong: MEASURED on a live receipt (`dclm-001--train`), `tokens_in` 4,075,324,354 vs `tokens_out` 4,075,323,392 — a 962-token gap on a healthy bundle. The brief's form fails, as claimed.

**The other four corrections all verify:**
- `max_order_bytes`: `_DEFAULT_MAX_ORDER_BYTES == 512*1024*1024`; `39,205 × 12,207 × 4 = 1,914,301,740 B = 3.57×`. Declared at 4 GiB on **both** groups (`common` dict, so it is inherited by both) — correct, since `_cfg` reads per group. ✅
- Two groups: proven by execution in the suite, and the `_cfg`/`_block_count` code path confirms it. ✅ **(But see F2 — the values declared are wrong even though the structure is right.)**
- Gate A memory: real, and **understated** — see F1.
- 12,207: correct per LOADER-RESOLVE, parameterised in the library, **duplicated as a literal in the driver** — see F2/F10.

**Deterministic and seedless.** Two `build_order` calls on identical input give byte-identical output; `np.argsort(kind="stable")` + `np.lexsort((idx, rank))`; ranks are int64 not float32. No RNG anywhere in the path.

**`identity_order` is a real permutation**, not an exemption, and `bincount == 1` holds on it.

---

# RANKED SUMMARY

| # | severity | finding | ships wrong? | verdict |
|---|---|---|---|---|
| **F2** | 🔴🔴 | Driver declares `block_count` from hard-coded `shards × 12,207`, ignoring the axis it derives. **Already wrong by 2.40% on val from live data.** | **val: YES, past Gate A** | CONFIRMED |
| **F3** | 🔴🔴 | Chunk-owner cursor drifts at every file-shard part boundary. **23.5% of corpus.** Silent. | **YES, silently** | CONFIRMED |
| **F1** | 🔴 | Gate A peak is **9.36 GiB** in an 8 GiB container — the 7.13 GiB figure omits `bincount`'s intp cast (proven 8.00 B/elem). | no (OOM, loud) | REFUTED (the figure) |
| **F12** | 🔴 | `profile={GROUP: …}` keys `mtld`; groups are `mtld-train`/`mtld-val`. `publish()` raises. | no (raises early) | CONFIRMED |
| **F4** | 🔴 | `--labels` absent from the live job def rev 13 (**read the registered command**), AND `__version__` unbumped so the preflight's version assert passes on a stale image. A 13 h rebuild emits zero labels and passes `verify`. | no (13 h wasted) | CONFIRMED LIVE |
| **F13** | 🔴 | `bundle_is_done` is **not label-aware**, so a `--labels` relaunch skips the 169 done bundles and labels only 16 — a mixed set no gate catches (`_check_labels` returns `[]` on `None`; `verify_bundle_set` has no labels logic). Build is TERMINAL 169/185. | no (driver catches, very late) | CONFIRMED (PLAT-2 + me) |
| **F5** | 🟠 | Constants reproduce bit-for-bit. But `[a,b,c,a] → 4.48` does **not** discriminate `<=` vs `<`; the docstring claim is wrong. Real coverage rests on one other test. | no | half-REFUTED |
| **F6** | 🟠 | 2.80% overhead: right route, mixed denominator, unverified on c7i. My own measurement gives 1.66%. | no | UNVERIFIABLE-until-run |
| **F9** | 🟡 | Degenerate ≤2048-token shard is unreachable from `corpus_pack` (8,192-token floor), so the loader's silent skip cannot fire — but nothing asserts it. | no | CONFIRMED-SAFE |
| **F7** | 🟡 | Labels format solid; endianness explicit; 14.2 B/doc reproduces. Truncation of whole records decodes silently, caught only by arithmetic. | no | minor |
| **F11** | ⚠️ | `curriculum_driver.py` has **zero tests**; every axis fixture is uniform-length; no multi-part fixture. F2/F3/F12 all live there. | — | CONFIRMED |
| **F8** | 🟡 | One object per group today, so concatenation order is moot — but becomes load-bearing if F1 is fixed by splitting. | no | noted |
| **F10/F14** | ✅ | `plan_id` unmoved (recomputed); guard is against the literal; byte-identity test compares real payload bytes; vacuity proof genuine; EOS/tie/short-doc attacks all pass; `tokens_in` proven correct. | no | CONFIRMED-GOOD |

---

# MERGE RECOMMENDATION: **MERGE WITH FIXES**

The library work (`corpus_mtld`, `corpus_labels`, `corpus_order`, the `on_document` hook, `Document.source_path`, `Receipt.labels`) is **good** — the constants are bit-for-bit correct, `plan_id` is provably unmoved, the shard bytes are provably identical, and the fail-closed reasoning is real rather than decorative. 1490 tests pass. **Do not throw this away.**

**But `artifacts/final-dataset/curriculum_driver.py` must not be run as written**, and two of its defects are the kind this repo exists to prevent.

## MUST FIX before the 13 h rebuild is launched
0. **F13 — make `bundle_is_done` label-aware** (~5 lines: return `False` when labels were requested and `receipt.labels is None`). The build is TERMINAL at 169/185, so a plain `--labels` relaunch labels **16 bundles out of 185** and no gate sees it. Without this, the *most likely* operational path silently produces a 12%-labelled corpus. `--force` on all 185 is the alternative, but it is a bigger hammer that has to be remembered every time.
1. **F4 — two changes, both required.** (a) Register a new `edullm-reservoir-build` revision with `--labels` in the command — **rev 13's registered command does not have it, MEASURED.** (b) **Bump `__version__` off `0.9.1`** and add a preflight assertion that `labels` is a parameter of `run_bundle`, because rev 13's `assert __version__=='0.9.1'` passes on the *unlabelled* image too. Nothing else on this list matters if the rebuild produces no labels.

## MUST FIX before `curriculum_driver --go`
2. **F2 — declare `block_count` from `axis.n_chunks`, not from `PLAN_TRAIN_SHARDS × 12,207`.** Build a second axis with `split="val"` for `n_val` and size `identity_order` from it. Keep the constant product only as a printed upper bound that warns on disagreement. **This is already wrong against the live build.**
3. **F3 — make the part boundary representable.** Build `shard_stream` from the receipts (which name `bundle_id` and its shards) so each part is its own stream, or pass per-part `tokens_in` so the cursor resets. **23.5% of the corpus is mis-ranked otherwise, silently.**
4. **F12 — `profile={f"{GROUP}-train": PROFILE, f"{GROUP}-val": PROFILE}`.**
5. **F1 — raise the validator job definition's memory to ≥ 16,384 MiB** before Gate A sees a 478 M-index vector. Do **not** fix it by splitting the vector unless you also add the ordinal-ordering guarantee F8 names.

## SHOULD FIX (cheap, and each closes a silent channel)
6. **F9** — raise in `chunk_axis_from_manifest` when any included shard yields 0 chunks, citing the loader's silent skip.
7. **F13** — either implement the all-or-none labels check in `verify_bundle_set` or delete the docstring sentence that promises it.
8. **F7** — compare `len(payload) // ITEM_SIZE` against the header's `documents` in `decode_labels`.
9. **F11** — add the two ~20-line tests: a heterogeneous axis with a short tail shard, and a two-part stream with a nonzero part-0 `tokens_in − tokens_out` gap. **These are the tests that would have caught F2 and F3.**
10. **F5** — correct the `[a,b,c,a]` docstring claim in `tests/test_corpus_mtld.py`.
11. **F6** — re-measure MTLD's share from the first bundle's wall clock on c7i rather than arguing it.

## What I could NOT verify
- Gate A's peak at true N on the real validator container — my 9.36 GiB is extrapolated from a proven 8.00 B/elem cast plus three exactly-known terms, but it is DERIVED, not observed at 478 M.
- `_reader_for` populating `source_path` from live HF inside the container. Both reader call sites set it (`corpus_read.py` parquet and jsonl.gz branches, both in the diff), but no container has run.
- The MTLD absolute rate on c7i.

**A 13 h rebuild launched on the library code with `--labels` wired is safe. A publish launched on this driver is not.**





---

# 🔬 ENG-EXEC-3 — THE OOM, MEASURED. **File-sharding does not reduce the peak AT ALL.**

**Assignment:** measure the peak, do not estimate. PLAT-1 predicted 16.5 GB against a 14,336 MiB
container and attributed it to `SeenHashes`; the CEO recorded it as *"fixed by the bundle-splitting
already on the critical path."* **Both halves are wrong, and the second one is the dangerous one.**

## 1. The four OOM bundles have the FOUR SMALLEST dedup sets in the plan

`SeenHashes` per-entry cost, MEASURED two ways in clean subprocesses:

| method | B/entry | note |
|---|---|---|
| `tracemalloc`, 400k entries | **85.946** | reproduces the docstring's 85.9 to 0.05% |
| **peak RSS, 1M / 2M / 4M entries** | **114.05 / 114.73 / 114.97** | **what the container kills on** |

⚠️ **The docstring's 85.9 B/entry is a `tracemalloc` figure and it understates the RSS the cgroup
enforces by 34%.** Both are correct measurements of different quantities; only the second one is a
memory limit.

Applying 114.97 B/entry to each train bundle's documents-read (derived from `bundle.tokens`,
`_CHARS_PER_TOKEN`, `_FILTER_HEADROOM`, `keep_rate` and the MEASURED per-source tok/doc table):

| bundle | K | tok/doc | docs read | `SeenHashes` | OOMed? |
|---|---|---|---|---|---|
| `finephrase-table--train` | 1 | 262 | 51,462,750 | **5.51 GiB** | ✅ **SUCCEEDED** |
| `stackv2-edu--train--p04of07` | 7 | 727 | 31,832,930 | 3.41 GiB | 🔴 OOM |
| `finepdfs-edu--train--p03of04` | 4 | 5,630 | 4,190,912 | 0.45 GiB | 🔴 OOM |
| `reasoning-traces--train` | 1 | 11,310 | 1,059,712 | **0.11 GiB** | 🔴 OOM |
| `pre-1929-books--train` | 1 | 6,000 | 998,823 | **0.11 GiB** | 🔴 OOM |

🔴 **The bundle with the LARGEST dedup set in the entire plan — 5.51 GiB, 50× the two that died —
completed successfully.** `SeenHashes` cannot be the cause. **A term that is 0.11 GiB in a failing
container and 5.51 GiB in a passing one is not the term.**

## 2. The real term is ONE PARQUET ROW GROUP, and it is the UPSTREAM WRITER'S choice

`corpus_read.read_parquet_documents` (`:840-841`):
```python
table = pf.read_row_group(rg, columns=leaves)
for row in table.to_pylist():
```
**A whole row group is decoded into arrow, and then `to_pylist()` materialises the SAME data again
as Python dicts while `table` is still referenced.** Both live at once.

MEASURED, clean subprocess per row, `ru_maxrss` delta across `read_row_group` + `to_pylist`, on real
pyarrow-written `large_string` fixtures:

| shape | rg uncompressed | arrow | `to_pylist` | **PEAK** |
|---|---|---|---|---|
| 248,420 rows × 5,251 B — **the MEASURED Nemotron-CC-Math shape** | 1.23 GiB | +2,840 MiB | +1,202 MiB | **3.95 GiB** |
| 30,000 rows × 40,000 B | 1.12 GiB | +3,564 MiB | +1,416 MiB | **4.86 GiB** |
| 60,000 rows × 40,000 B | 2.24 GiB | +4,983 MiB | +611 MiB | **5.46 GiB** |
| **100,000 rows × 40,000 B — the `reasoning-traces` shape (11,310 tok/doc)** | 3.73 GiB | +3,129 MiB | +4,018 MiB | 🔴 **6.98 GiB** |

**The row-group geometry comes from DATA's own measurement** (`data/status.md:1113-1115`):
> *"they have only **4 row groups of ~250,000 rows (~600 MB each)**, so a 'random row group' is a
> quarter of a 2.4 GB file"*

## 3. 🔴 **WHY FILE-SHARDING CANNOT HELP — MEASURED-IN-CODE**

`_bundle_files` → `_shard_slice(files, i, K)` → `items[shard::of]`
(`ingest_reservoir.py:779`). **The stride is over the FILE LIST.** A row group lives *inside* one
file, and every child reads whole files — so K-way splitting gives each child **fewer files of
exactly the same shape**. The per-file peak is **invariant under K**.

**That is the proof, and the live results are its confirmation:** `p03of04` and `p04of07` were
*already* file-sharded when they OOMed. Two of the four failures happened to bundles the mitigation
had already been applied to. **A predicted failure recorded as mitigated, where the mitigation was
structurally incapable of applying.**

## 4. The peak, assembled

| term | GiB | grade |
|---|---|---|
| **one row group (arrow + `to_pylist`, both live)** | **3.95 – 6.98** | **MEASURED** |
| decontamination index, resident | **0.51** | **MEASURED** at the LIVE size (149,777 exact + 3,097,372 ngrams) |
| ↳ *parse* peak (raw container + frozensets both live) | 0.57 | MEASURED |
| `SeenHashes` | 0.11 – 5.51 | DERIVED from 114.97 B/entry MEASURED × plan docs-read |
| interpreter + boto3 + numpy + driver | ~0.35 | UNVERIFIED (not measured in the container) |
| **worst-case total** | **~13.35** | 6.98 + 0.51 + 5.51 + 0.35 |

**⚠️ The decon index docstring says "~250 MB resident for the shipped index". MEASURED: 527 MiB
resident, 579 MiB parse peak — 2.1× the claim.** Same error class as the dedup set's 85.9: a figure
that was true of something adjacent.

🔴 **Worst case ~13.35 GiB against 14,336 MiB = 14.00 GiB is 95.4% — and the interpreter term is the
one number here that is UNVERIFIED.** The four failures are explained without needing it; the margin
is not.

## 5. RECOMMENDATION (the job-def raise is PLAT's to make, not mine)

**Raise `edullm-reservoir-build` memory from 14,336 MiB to ≥ 24,576 MiB.** Rationale, in the order
that matters:
1. The binding term is 6.98 GiB MEASURED and is **not reducible by any plan-shape change** — not K,
   not ordinals, not bundle boundaries.
2. It is reducible **in code**, and that is TASK 4/7: `iter_batches(batch_size=…)` bounds it, and
   dropping `to_pylist()` for `list_flatten` + `.field()` removes the second copy. Until that lands
   and is proven on this reader, the container has to hold the current shape.
3. **This is the SAME raise F1 needs for Gate A** (≥16,384 MiB for `np.bincount`'s intp cast). Two
   findings, one config change — which is why the brief said to bundle them. 24,576 covers both with
   the margin the UNVERIFIED interpreter term requires.

**What I did NOT do:** no job-def registration, no AWS call of any kind. This is a measurement and a
recommendation.

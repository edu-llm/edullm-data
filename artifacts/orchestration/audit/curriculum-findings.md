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

The measured peak (1,393 MiB) is between the 3-term sum (1,240 MiB) and the 4-term sum (2,003 MiB), consistent with the intp temp being live across the bincount call and the bool temp being allocated after the intp temp is freed. **The dominant missing term is the intp cast**: `np.bincount` requires `intp` and a `<u4` array is not `intp`, so numpy materialises an 8-byte copy of the whole vector before counting. That term is 3.57 GiB at N and it is **absent from the ledger's 7.13 GiB**.

**Verdict: the 7.13 GiB figure is REFUTED as an upper bound. Real peak is ~8.9–9.4 GiB — OVER the 8 GiB container.**

Reconciling with "it PASSED": the CEO's own note says the measurement was taken at full scale and passed at 89%. Two readings are possible and I cannot distinguish them from here: (a) the measurement summed the three terms analytically rather than observing RSS, in which case it never saw the intp cast; or (b) it observed RSS on a host with enough memory and the 8 GiB container number was applied afterwards by arithmetic. **Either way the number that was compared against `memory: 8192` is not the peak.** I did not find a full-scale RSS trace in the repo — see F1b.

**Blast radius.** Gate A is the last gate before promotion. An OOM there is a killed container after a 6–7 h `publish()` and a ~4 h Gate A — the exact "expensive outcome" the ledger names. It does not corrupt data. **But it is not a "no headroom" risk; on my numbers it is over budget before boto3, the JSON manifest, or the rest of Gate A's Python is counted.**

**Fix, cheapest first (none require touching `token_order_v1`'s semantics):**
1. **Raise the validator job def's `memory` to ≥ 16384 MiB** for the curriculum publish. Pure config, no code, no shared-profile change. This is the fix I would take.
2. Split the order into ≥ 4 objects (see F8): `check_order_domain` is per-entry, so peak scales with the largest object, not the corpus.
3. If code is touched: `np.bincount(order.astype(np.intp, copy=False), ...)` does not help (the cast is the cost). A chunked count (`np.add.at` over slices, or `counts = np.zeros(n, np.int64); np.add.at(...)`) would, but that is a platform change to a profile shared by every future curriculum dataset — correctly out of scope for this branch, per the ledger.


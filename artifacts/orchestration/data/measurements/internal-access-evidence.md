# 1a — Evidencing the "internal" basis for `s3://edullm-data`

**Author:** DATA-EXEC. **Date:** 2026-08-08. **Read-only.** Executed against CEO condition 1a on the
owner's ruling to PROCEED with Nemotron-CC-Math, which rests on `edullm-data` being non-public.

**Question:** who can actually read `s3://edullm-data`?
**Answer: nobody outside account `<ACCOUNT_ID>`. The owner's factual premise HOLDS.**
**But see §4 — one adjacent bucket materially qualifies it, and §5 — one thing the ruling should not
be read as having settled.**

Grades: `MEASURED` (I ran the API call) / `DERIVED` / `UNVERIFIED`.

---

## 1. Public access — blocked four ways, at the bucket. `MEASURED`

`s3api get-public-access-block --bucket edullm-data`:
```
BlockPublicAcls      : true
IgnorePublicAcls     : true
BlockPublicPolicy    : true
RestrictPublicBuckets: true
```
All four. And S3's own adjudication, `get-bucket-policy-status`: **`IsPublic: false`** — that is
AWS evaluating the policy, not me reading it, which is the right kind of evidence for this claim.

## 2. Bucket policy — no cross-account principal anywhere. `MEASURED`

`Id: edullm-data-airlock-v2`, three statements. Reproduced in substance:

| Sid | Effect | who |
|---|---|---|
| `OnlyValidatorWrites` | **Deny** `PutObject`/`PutObjectTagging`/`AbortMultipartUpload` to `Principal: *` | except `…:role/sbsandbox-intern-edullm-infra-deployer` and `…:role/sbsandbox-intern-edullm-dataset-validator` |
| `NobodyDeletesPublishedData` | **Deny** `DeleteObject`/`DeleteObjectVersion` to `Principal: *` | no exceptions for non-service principals |
| `AllowS3InventoryDelivery` | **Allow** `PutObject` to `Service: s3.amazonaws.com` on `_inventory/*` only | conditioned on `aws:SourceAccount = <ACCOUNT_ID>` **and** `aws:SourceArn = arn:aws:s3:::edullm-data` |

**Every ARN in the policy is in account `<ACCOUNT_ID>`.** There is **no `Allow` for any external
account, no `aws:PrincipalOrgID` grant, and no wildcard read `Allow`.**

⚠️ **Worth stating precisely, because it is easy to over-read:** the policy contains **no read `Allow`
at all**. Read access is therefore governed entirely by **IAM identity policies inside the account** —
the default-deny for anyone outside it. That is *stronger* for the "internal" claim than a read Allow
would be, but it also means **the set of internal readers is an IAM question, not a bucket question**,
and it is not enumerable from the bucket. `UNVERIFIED`: the exact list of in-account principals with
`s3:GetObject` on this bucket.

## 3. Other exfiltration paths — all checked, all absent. `MEASURED`

| path | result |
|---|---|
| Bucket ACL | **one grant only**: owner canonical id `1e0a8b85…`, `FULL_CONTROL`. No `AllUsers`, no `AuthenticatedUsers`, no cross-account grantee. |
| S3 Access Points | `list-access-points` → **`AccessPointList: []`** — none, so no alternate policy surface. |
| Cross-Region Replication | `get-bucket-replication` → **`ReplicationConfigurationNotFoundError`** — the bucket does not replicate anywhere. |
| Versioning | `Enabled` (relevant to §5, not to access) |

## 4. ⚠️ THE ONE QUALIFICATION — the region mirror has NO BUCKET POLICY

`edullm-data-us-east-2` **exists** (created 2026-08-07, one day before this ruling) and is the mirror
`IMPLEMENTATION-PLAN.md` §8B.3 recommends. `MEASURED`:

| check | `edullm-data` | `edullm-data-us-east-2` |
|---|---|---|
| public access block | all four `true` | **all four `true`** ✅ |
| bucket policy | `edullm-data-airlock-v2`, 3 statements | 🔴 **`NoSuchBucketPolicy` — NONE** |
| contents | 3.52 TB | **empty** (no CloudWatch storage metric) |

**This is NOT a public-exposure finding** — the four public-access blocks are on, so the mirror is not
readable from outside the account either, and **the owner's "internal" premise is not undercut.**
Per the CEO's instruction I checked specifically for a cross-account or public read path: **there is
none.** No urgent escalation.

**It IS an airlock finding, and it is the same class of error as `state: ENABLED`.** The mirror
carries **neither** `OnlyValidatorWrites` **nor** `NobodyDeletesPublishedData`. So on that bucket:
- any in-account principal that can write, can write — **the validator-only invariant does not exist there**;
- **published objects are DELETABLE** — "frozen means frozen" is enforced by a bucket policy that
  this bucket does not have.

It is empty today, so nothing is at risk **yet**. **But §8B.3 recommends mirroring the published
corpus into it, and the moment that happens the copy is governed by no airlock.** Anyone reasoning
"the corpus is protected because the bucket policy denies it" would be wrong about the mirror.
**→ Flagging to the CEO for PLAT: the mirror needs the airlock policy applied BEFORE it receives a
single object.** Not mine to fix (read-only, and infra is PLAT's lane).

**A second-order note on §3.3 that cuts the owner's way:** because the mirror has no
`NobodyDeletesPublishedData`, a mirrored copy would actually be *deletable*, which is the one place
the deletion obligation could be honoured. I record it as an observation, not a recommendation —
**the ruling is made and I am not reopening it.**

## 5. What this evidence does NOT cover — stated so the ruling is not over-read

- **Versioning is `Enabled`** on `edullm-data`. Combined with `NobodyDeletesPublishedData` denying
  both `DeleteObject` **and** `DeleteObjectVersion`, deletion is doubly foreclosed. This is the
  §3.3 tension **as accepted by the owner** — recorded, not re-litigated.
- **The set of in-account human/role readers is not enumerable from the bucket** (§2). If the
  "internal" basis is ever challenged, the missing evidence is an IAM-side enumeration of who holds
  `s3:GetObject`, and `iam:simulate-principal-policy` **lies for the intern role** (11 known false
  denials, per CLAUDE.md) — so it would need live smoke tests, not a simulation.
- I did **not** test a live cross-account read, because I have credentials for exactly one account.
  The evidence above is configuration-side, and configuration **is** the right evidence for "is there
  a path" — but per this project's own standing lesson, **configuration is not evidence of behaviour.**
  The negative half (no policy grants it) is strong; a positive proof of unreachability from an
  outside principal is not obtainable from this session.

## Verdict

**The owner's factual premise — `edullm-data` is internal — is SUPPORTED by every check available
read-only: four public-access blocks on, `IsPublic: false` per S3 itself, no cross-account principal
in the policy, a single owner ACL grant, no access points, no replication.**
**One adjacent bucket (`edullm-data-us-east-2`) is equally non-public but has NO airlock policy, and
that is a real pre-mirror gap for PLAT — not a challenge to the ruling.**

---

# 1b — Label discipline so the Nemotron objects are ENUMERABLE

**The requirement, restated:** mixture cannot span groups, so the math pillar cannot be *separable*
without breaking the mix. **It must nevertheless never be UNKNOWABLE** — if termination ever comes,
we must be able to enumerate exactly which objects are affected. This is provenance work the corpus
needs anyway.

## The mechanism already exists and is enforced, which makes this cheap. `MEASURED-IN-CODE`
The `source` label **is a path segment** — `tokens/<source>/<domain>/train-NNNNN.u32le.bin` — and
Gate A **recomputes** it from the key (`_check_labels_match_path`, `validate.py:1380`, called `:830`,
full dict equality). So a `source` label is not a producer assertion: **it is the key, and the key is
the enumeration.** An `s3api list-objects-v2 --prefix tokens/<source>/` IS the affected-object list,
and it is exact by construction.
**Consequence: no new schema, no new field, no manifest change is required for 1b.** It is satisfied
by choosing the `source_label` strings deliberately — which must happen before FREEZE anyway, because
ordinals are allocated across the whole plan alphabetically.

## 🔴 But the labels W3 proposed have a PREFIX-COLLISION hazard, and it defeats the purpose
W3's paste-ready rows set **`source_label: "nemotron-cc-math"` for BOTH** config `3` and `4plus`
(deliberately — the tiers are one corpus, and §1.1 fuses realness, not tier, into the label).
Reasonable. **But combined with the other Nemotron row it makes prefix enumeration unsafe.**
`MEASURED` — all candidates pass `SAFE_SEGMENT_RE = ^[a-z0-9]+(?:-[a-z0-9]+)*$`, so the regex will
not save us; and:

```
PREFIX COLLISION: 'nemotron-cc-math' is a prefix of 'nemotron-cc-math-3'
PREFIX COLLISION: 'nemotron-cc-math' is a prefix of 'nemotron-cc-math-4plus'
```

Two concrete failure modes for a termination-day enumeration:
1. **Over-capture.** `--prefix tokens/nemotron-` sweeps in **`nemotron-math-textbooks`**, which is
   `Nemotron-Pretraining-Specialized-v1` — **CC-BY-4.0, explicitly CARVED OUT of the Data Agreement.**
   Deleting it would destroy 3.0B tokens we are entitled to keep, and it is exactly the row whose
   licence *differs*. **The org name is NOT the licence boundary. The instrument is.**
2. **Under-capture / ambiguity.** If a future split (e.g. #28-style, or the 61.0B tier split) ever
   emits `nemotron-cc-math-3` as its own segment alongside a bare `nemotron-cc-math`, then
   `--prefix tokens/nemotron-cc-math` catches both — fine — but `--prefix tokens/nemotron-cc-math/`
   (with the delimiter, the *correct* way to avoid #1) **silently misses them.** The safe and the
   unsafe query differ by one character.

## Recommendation to the CEO — a choice, not a decision I am making
**Make the licence boundary visible IN the label, so enumeration is a single unambiguous prefix.**

| option | labels | termination-day query | cost |
|---|---|---|---|
| **A (recommended)** | keep **`nemotron-cc-math`** for both tiers; rename row 17 to **`nvidia-specialized-math-textbooks`** or simply **`math-textbooks`** | `--prefix tokens/nemotron-cc-math/` — exact, one query, no sibling shares the stem | one string, decided pre-FREEZE, free |
| B | prefix the restricted rows, e.g. `restricted-nemotron-cc-math` | encodes a licence *state* in a permanent path segment; if the agreement is ever renegotiated the label is wrong forever, and it is inside `manifest_sha256` | free now, wrong later |
| C | leave as-is, document the query | relies on whoever runs it in a hurry, under legal pressure, using the delimiter correctly | free, fragile |

**A is recommended** because it fixes the collision by removing the shared stem from the *unrelated*
source, rather than encoding a mutable legal status into an immutable key. **It costs one string.**
⚠️ **Whichever is chosen, it must be set BEFORE FREEZE** — `source_label` is a path segment inside
`manifest_sha256`, so changing it later is a republish and a full re-copy, and it renames ordinals.

## What 1b does NOT need
- **No `labels` schema change.** Per my gap-3 finding, `entry.labels` is per-OBJECT and flat-string
  only, with **two** keys total (`source`, `domain`) and a third level refused
  (`manifest.py:693`, `:730-742`). A "licence" label is not expressible **and is not needed** — the
  `source` segment already carries it.
- **No separate group.** Mixture cannot span groups; splitting the math pillar into its own group
  would break the mix, which is precisely what the CEO's framing rules out.

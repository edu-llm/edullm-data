# Dataset Standard — Diagrams

Visual companion to [`DATASET-STANDARD.md`](DATASET-STANDARD.md).

Scale for context: **2.53 TB** of datasets across 6 buckets (~$58/mo STANDARD). Note that
`olmo-150b-dolma2/` (633 GB, 13,840 objects) is a *prefix inside* `edullm-datasets` (1.57 TB, 16,539
objects) — not a separate dataset.

---

## 1. Anatomy of a dataset

```mermaid
flowchart TB
  ROOT["<b>s3://edullm-data/&lt;family&gt;/&lt;name&gt;/&lt;version&gt;/</b><br/>one bucket · no dates in names · vN monotonic"]

  ROOT --> DJ["<b>dataset.json</b> — typed root pointer<br/>written FIRST (reserves version)"]
  ROOT --> G["<b>groups</b> — typed payload"]
  ROOT --> RM["README.md — generated<br/>+ notes / limitations from dataset.json"]
  ROOT --> SU["<b>_SUCCESS</b> — written LAST, by CI only"]
  ROOT --> DEP["dependents/ — refcount<br/>(excluded from manifest_sha256)"]

  DJ --> CORE["<b>INVARIANT CORE</b><br/>schema_version · dataset_id · version{id,relation,of}<br/>created_at · owner (a group) · purpose<br/>class · mutability · inventory{objects,bytes}<br/>groups[] · sources[] · build{}<br/>license{id,basis} · pii{class,basis}<br/>notes · limitations[]"]

  G --> G1["group: tokens<br/>profile: pretrain-tokens/v1"]
  G --> G2["group: sidecars<br/>profile: tabular/v1"]
  G --> G3["group: orderings<br/>profile: token-order/v1"]

  G1 --> M1["tokens/manifest.json"]
  G2 --> M2["sidecars/manifest.json"]
  G3 --> M3["orderings/manifest.json"]

  M1 --> SH["<b>per-entry</b><br/>path · sha256 · bytes<br/>count{unit,value} — omissible<br/>format{container,dtype,byte_order,<br/>header_bytes,codec}"]

  SU --> BIND["<b>binds everything</b><br/>canonical_uri · dataset_id · version<br/>dataset_json_sha256 · readme_sha256<br/>groups_merkle_root<br/>object_count · total_bytes"]

  style DJ fill:#1a3a5c,color:#fff
  style SU fill:#1a5c2a,color:#fff
  style CORE fill:#2d2d2d,color:#fff
  style BIND fill:#1a5c2a,color:#fff
```

**Why `groups[]` is the key idea:** `profile`, `format`, and `partitions` all live on the *group*, not
the dataset. One dataset can hold integer tokens + float weight sidecars + index vectors without any
field having to lie.

---

## 1b. The airlock — where enforcement actually lives

```mermaid
flowchart LR
  subgraph PROD["producers — any origin, same door"]
    P1["AWS Batch"]
    P2["EC2 / SageMaker"]
    P3["FarmShare / laptop"]
  end

  P1 --> L
  P2 --> L
  P3 --> L

  L["<b>s3://edullm-landing</b><br/>ANY producer may write<br/>create-only · 14d expiry<br/>MPU abort 1d (load-bearing)<br/><i>nothing trains from here</i>"]

  L -->|"manifest.json arrives<br/>= commit point<br/>→ EventBridge"| V

  V["<b>validator — AWS Batch</b><br/>runs as existing role<br/>sbsandbox-intern-edullm-batch-workload<br/>trust policy: ecs-tasks only<br/>→ NO human can assume it<br/><br/>the ONLY principal with<br/>PutObject on edullm-data"]

  V -->|"Gate A passes<br/>→ server-side copy<br/>(multipart >5GB)"| D
  V -->|"any assertion fails"| R

  D["<b>s3://edullm-data</b><br/>versioned · deny-delete<br/>READ-ONLY to everyone<br/>+ _catalog/&lt;id&gt;/&lt;version&gt;.json"]
  R["landing/.../_REJECTED.json<br/>failing assertions listed<br/>bytes expire in 14d"]

  D --> T["trainers · eval harnesses<br/>read only"]

  BG["break-glass: infra-deployer<br/>human-assumable, alarmed<br/>(deletion only)"]
  BG -.->|"refuses if<br/>dependents/ non-empty"| D

  style L fill:#3a2a1a,color:#fff
  style V fill:#1a3a5c,color:#fff
  style D fill:#1a3a2a,color:#fff
  style R fill:#3a1a1a,color:#fff
  style BG fill:#2d2d2d,color:#fff
```

**Why an airlock beats a sentinel:** with sentinel-gating, unvalidated bytes are already sitting in the
published namespace — create-only writes mean they can never be overwritten, they're visible to anyone
globbing, and the sentinel merely declines to bless them. With an airlock they never arrive.

**Why this answers "datasets are made in AWS":** a Batch job writing straight to landing never stages a
local copy — `publish()` reads sizes and hashes via `HEAD` and no byte leaves S3. Same door as a laptop,
and strictly cheaper. There is no internal fast path to route around because there is no faster path.

---

## 2. Publish order — the chain that can't be forged

```mermaid
sequenceDiagram
  participant P as Publisher<br/>(FarmShare / GPU box / laptop)
  participant S as s3://edullm-data
  participant V as Validator (CI)

  Note over S: PRECONDITION: AbortIncompleteMultipartUpload 7d<br/>else an in-flight upload can outlive the commit

  P->>S: 1. PUT dataset.json — IfNoneMatch:*
  Note over P,S: reserves version · ConditionalConflict → vN+1, retry<br/>doubles as publish-in-progress marker

  loop each payload object
    P->>S: 2. PUT shard — IfNoneMatch:* + CRC64NVME + meta.sha256
  end
  Note over P,S: create-only, enforced by BUCKET POLICY<br/>(needs s3:ObjectCreationOperation exemption or MPU 403s)<br/>SHA-256 is client-asserted: multipart SHA-256 is COMPOSITE,<br/>so CRC64NVME is the only server-verifiable witness

  P->>S: 3. PUT <group>/manifest.json (canonical JSON)

  V->>S: 4. GATE A — recompute, never read
  Note over V,S: LIST↔manifest both ways · HEAD sizes · distinct digests<br/>count×dtype==bytes · magic bytes · decode smoke<br/>partitions[].rows · no shared sha256 with parents
  V--xP: any failure → REFUSE (nothing published)

  V->>S: 5. PUT _SUCCESS — CI ONLY
  Note over V,S: binds canonical_uri + dataset_json_sha256<br/>+ readme_sha256 + groups_merkle_root

  V->>S: 6. PUT _catalog/<id>/<version>.json (immutable)
```

**Why CI doesn't write the bytes:** CodeBuild cannot read FarmShare `/scratch`, so CI can never be the
writer of a 633 GB corpus. Anyone writes bytes (create-only, checksummed); **only CI writes
`_SUCCESS`** — and `_SUCCESS` is what makes a dataset visible to readers.

---

## 3. Choosing a profile

```mermaid
flowchart TD
  START{"What is in this<br/>payload group?"}

  START -->|verbatim third-party bytes| VEN["<b>vendored/v1</b><br/>naming_exempt · vendor_root<br/>sentinels[] accounted"]
  START -->|transfer archive parts| DIST["<b>distribution-artifact/v1</b><br/>no rows/tokens of its own"]
  START -->|run records, never closed| PROV["<b>provenance-log/v1</b><br/>mutability: append-only"]
  START -->|telemetry over time| MET["<b>metrics-timeseries/v1</b>"]
  START -->|text| TXT{"tokenized?"}
  START -->|derived from another group| DER{"what kind?"}
  START -->|benchmark| EV{"inputs or outputs?"}
  START -->|images/video/audio| MED["<b>media/v1</b><br/>labels required only if<br/>intended_use claims supervised"]
  START -->|rows and columns| TAB["<b>tabular/v1</b><br/>container-agnostic"]

  TXT -->|yes, packed| PT["<b>pretrain-tokens/v1</b><br/>tokenizer{} pinned · .u32le.bin"]
  TXT -->|no, raw docs| TC["<b>text-corpus/v1</b>"]
  TXT -->|conversations| SFT["<b>sft-conversations/v1</b><br/>messages[] · heldout · leakage report"]

  DER -->|index vectors| TO["<b>token-order/v1</b><br/>depends_on[] · permutation check"]
  DER -->|per-record metrics| AN["<b>annotations/v1</b><br/>row-i == parent row-i"]
  DER -->|parallel float arrays| WS["<b>weights-sidecar/v1</b><br/>same cardinality, diff dtype"]

  EV -->|inputs| EI["<b>eval-items/v1</b><br/>stable per-item id"]
  EV -->|outputs/scores| ER["<b>eval-results/v1</b><br/>status_counts{} · refuse n_ok==0"]

  START -->|none of these fit| EXP["<b>experimental/v1</b><br/>quota 2 per family · lossy<br/>exception lives OUTSIDE artifact"]

  style EXP fill:#5c1a1a,color:#fff
  style PT fill:#1a3a5c,color:#fff
  style ER fill:#1a3a5c,color:#fff
```

---

## 4. Validation gates — and what each one catches

```mermaid
flowchart LR
  subgraph A["GATE A — publish-time (free/cheap)"]
    direction TB
    A1["prefix == dataset_id/version"]
    A2["manifest EXHAUSTIVE<br/>(LIST ↔ manifest, both ways)"]
    A3["HEAD: ContentLength == bytes<br/>meta.sha256 == manifest.sha256"]
    A4["shard digests pairwise DISTINCT"]
    A5["count × dtype_size == bytes"]
    A6["magic bytes agree with format<br/>(the honesty rule)"]
    A7["DECODE SMOKE<br/>0≤id&lt;vocab · distinct≥K<br/>eos/zero fraction in bounds"]
    A8["partitions[].rows verified<br/>in ONE scan"]
    A9["no shared sha256 with depends_on"]
    A10["pii ≥ max(sources.pii)"]
    A11["status_counts: n_rows==ok+err+filt<br/>REFUSE if n_ok==0"]
    A12["bincount(order)==1 for permutations"]
  end

  subgraph B["GATE B — nightly wu-fsck (LIST+HEAD only) · owner: Eric Wu"]
    direction TB
    B1["re-resolve sources[].uri"]
    B2["re-resolve depends_on"]
    B3["catalog ↔ reality counts/bytes"]
    B4["image_digest still in ECR"]
  end

  subgraph C["GATE C — spot-check"]
    C1["rebuild ONE shard<br/>in recorded env, compare"]
  end

  A7 --> SMOKE["<b>decode smoke test — how</b><br/>seed = sha256(dataset_id|version|shard)<br/>→ N offsets, ~64 KB/shard, dtype-aligned<br/>recorded as {seed, offsets, window}<br/><br/>random offsets NOT head-only:<br/>a zero-filled tail leaves a<br/>correctly-sized file with a valid head"]
  style SMOKE fill:#5c3a1a,color:#fff

  A --> B --> C

  A0["<b>MPU-abort lifecycle rule</b><br/>(not a check — a precondition)<br/>in-flight uploads are invisible to LIST,<br/>so _SUCCESS can be written while<br/>parts are still outstanding"]
  A0 --> A

  A6 -.catches.-> F1["7,557 lying .npy extensions"]
  A5 -.catches.-> F2["truncated / wrong-dtype shards"]
  A7 -.catches.-> F3["all-zeros / all-EOS shard<br/>(valid sha256, correct size)"]
  A11 -.catches.-> F4["12 × 66-byte header-only CSVs<br/>3 × all-error files"]
  A2 -.catches.-> F5["stray unlisted shard<br/>a globbing reader would train on"]
  A9 -.catches.-> F6["37 GB re-materialized CAS"]
  A4 -.catches.-> F7["duplicated shard"]
  A12 -.catches.-> F8["curriculum ordering that is<br/>2M copies of block 0"]
  B1 -.catches.-> F9["source bucket deleted AFTER publish<br/>(every real failure was post-publish)"]

  style A fill:#1a3a2a,color:#fff
  style B fill:#1a2a3a,color:#fff
  style C fill:#2a2a1a,color:#fff
```

**Gate B exists because publish-time checks are architecturally mismatched to the failure history** —
every dangling pointer in the audit appeared *after* publication.

---

## 5. Referencing vs copying

```mermaid
flowchart TB
  subgraph WRONG["❌ What the naive rule forces"]
    direction TB
    W1["pool: objects/&lt;sha256&gt;.bin"]
    W2["rule: shards MUST be<br/>&lt;split&gt;-&lt;NNNNN&gt;-of-&lt;NNNNN&gt;"]
    W3["→ same block needs two ordinals<br/>→ CAS is non-compliant<br/>→ COMPLIANT PATH IS TO COPY"]
    W4["result: 37 GB duplicated<br/>(this actually happened)"]
    W1 --> W2 --> W3 --> W4
  end

  subgraph RIGHT["✅ What the standard does"]
    direction TB
    R1["parent: pretrain/datamix1/v1<br/>objects/&lt;sha256&gt;.bin<br/>(CAS group — naming EXEMPT)"]
    R2["child: curriculum/linear-flesch/v1<br/>profile: token-order/v1<br/>payload = order vectors ONLY"]
    R3["depends_on[]:<br/>dataset_id + version + uri<br/>success_sha256 + manifest_sha256<br/>block_count"]
    R4["VALIDATOR: no sha256 may appear<br/>in both child and parent"]
    R5["dependents/&lt;child&gt;.json in parent<br/>→ delete refuses while non-empty"]
    R1 --> R3
    R2 --> R3 --> R4 --> R5
  end

  WRONG -.->|"fix: naming is a PROFILE rule,<br/>never a core rule"| RIGHT

  style WRONG fill:#3a1a1a,color:#fff
  style RIGHT fill:#1a3a1a,color:#fff
```

**The rule:** never make referencing harder than copying. Any standard where copying is the compliant
path will get copies.

---

## 5b. Adding a profile vs reaching for `experimental`

```mermaid
flowchart TD
  Q{"No profile fits<br/>my data"}
  Q -->|"recurring kind of data<br/>I can name the bug<br/>it prevents"| NEW["<b>WRITE A PROFILE</b> ← expected path<br/>1. registry entry: profiles/&lt;name&gt;/v1.py<br/>2. JSON Schema fragment for new fields<br/>3. check fn(s) that RECOMPUTE something<br/>4. two fixtures: one passing, one broken<br/><br/>review: does each check recompute?<br/>does the broken fixture fail?"]
  Q -->|"must ship TODAY<br/>shape still unknown"| EXP["<b>experimental/v1</b> ← rare<br/>max 2 live per family<br/>not resolvable by dataset_id<br/>can't feed anything reported<br/>exception lives OUTSIDE the artifact"]

  EXP -->|"once the shape is clear"| NEW

  NEW --> GOOD["good profile:<br/>• names the failure it prevents<br/>• only fields its checks consume<br/>• prefers arithmetic identities<br/>• starts at /v1, never mutates one"]

  style NEW fill:#1a3a2a,color:#fff
  style EXP fill:#5c1a1a,color:#fff
```

Registry grew 7 → 15 by taking real shapes seriously. It should keep growing — that's the design working,
not failing.

---

## 6. Splits that aren't directories

```mermaid
flowchart LR
  subgraph OLD["❌ splits{name → {shards,rows,tokens}}"]
    O1["assumes splits are PATHS"]
    O2["breaks: index-range holdout<br/>(source_index ≥ 1e9)"]
    O3["breaks: record-field split<br/>(meta.split)"]
    O4["breaks: peer × round × phase<br/>(3 orthogonal axes)"]
    O5["breaks: views that REPEAT blocks<br/>→ summed tokens &gt; pool tokens"]
  end

  subgraph NEW["✅ partitions[] — 4 closed forms, on the GROUP"]
    N1["by: path → glob ← the common case<br/>train-*.u32le.bin / val-*.u32le.bin"]
    N2["by: field → field + equals"]
    N3["by: range → field + min/max"]
    N4["by: indices → uri + dtype"]
    N5["<b>every form declares rows</b><br/>→ ONE scan falsifies all<br/>→ this is how train/val overlap is caught"]
    N6["coverage: partition |<br/>overlapping | incomplete<br/>(views repeat blocks → overlapping)"]
    N7["read it with:<br/>dataset_paths(id, ver, split='train')<br/>→ paths + correct dtype"]
  end

  OLD -->|"move to profile level"| NEW

  style OLD fill:#3a1a1a,color:#fff
  style NEW fill:#1a3a1a,color:#fff
```

Closed set only — no jq, no SQL, no expressions. Arbitrary predicates are unverifiable and are a
code-execution surface. Where a split is encoded twice, `by: field` is canonical and the validator
asserts the filename agrees.

---

## 7. Governance by class

```mermaid
flowchart TB
  B["<b>s3://edullm-data</b> — ONE bucket, one region (us-east-1)<br/>class is a FIELD; lifecycle + IAM applied per PREFIX"]

  B --> R["<b>released/</b>"]
  B --> S["<b>staging/</b>"]
  B --> C["<b>scratch/</b>"]

  R --> R1["versioning ON<br/>deny-delete except break-glass<br/>SSE-S3 (AES256)<br/>no expiry<br/>tags: Project/Owner/Purpose/DatasetId"]
  S --> S1["versioning ON<br/>noncurrent expiry 30d<br/>abort MPU 7d"]
  C --> C1["versioning off<br/>expiry 14d"]

  R1 --> PROM["promotion = METADATA FLIP<br/>(class field), not a copy"]
  S1 --> PROM

  NO["<b>NOT the default:</b><br/>Object Lock (either mode)"]
  NO --> NO1["protects a VERSION, not a PATH<br/>delete markers NOT WORM-protected<br/>→ delete+recreate walks around it"]
  NO --> NO2["lifecycle can't delete a locked version<br/>→ COMPLIANCE + long retention =<br/>unbounded growth nobody can stop"]
  NO --> NO3["GOVERNANCE is bypassable —<br/>the S3 console sends the<br/>bypass header automatically"]
  NO --> NO4["VERIFIED live: memorysplit-stephen<br/>Purpose=FrozenCorpus, GOVERNANCE,<br/>RetainUntil 2026-08-24T15:14:42Z<br/>→ lapses silently in &lt;1 month"]

  IMM["immutability comes from:<br/>create-only writes + versioning + deny-delete<br/>(prefer legal holds over timed retention)"]

  MPU["<b>DO FIRST — zero code:</b><br/>AbortIncompleteMultipartUpload 7d<br/>116 orphaned MPUs billing in<br/>edullm-datasets right now"]

  style MPU fill:#5c3a1a,color:#fff

  style R1 fill:#1a3a2a,color:#fff
  style NO fill:#3a1a1a,color:#fff
  style IMM fill:#1a3a5c,color:#fff
```

---

## 8. Cost of compliance — why the small case decides everything

```mermaid
flowchart LR
  subgraph L["633 GB · 13,840 objects<br/>(a PREFIX inside the 1.57 TB bucket)"]
    L1["sha256 ~21 min<br/>(overlaps transfer)"]
    L2["Gate A: minutes, threaded"]
    L3["overhead ≈ 1%"]
    L4["✅ nobody bypasses this"]
  end

  subgraph SM["869 MB · 92 objects"]
    S1["artifact takes 40 SECONDS to make"]
    S2["naive compliance:<br/>20-60 min metadata archaeology"]
    S3["overhead 50-100×"]
    S4["❌ goes in scratch, every time"]
  end

  subgraph FIX["The fix: 6 hand-written fields"]
    F1["dataset_id · purpose · pii<br/>profile · (version auto) · (owner from identity)"]
    F2["DERIVED: hashes, bytes, counts, formats,<br/>code_sha256, packages_lock_sha256, timestamps"]
    F3["INHERITED from family.json:<br/>license, sources[], tokenizer"]
    F4["publish() = ONE call, &lt; 2 min"]
  end

  SM -->|"high-volume case<br/>= source of the sprawl"| FIX
  L --> FIX

  style L fill:#1a3a2a,color:#fff
  style SM fill:#3a1a1a,color:#fff
  style FIX fill:#1a3a5c,color:#fff
```

---

## 9. What was cut, and why

```mermaid
mindmap
  root((Cut from<br/>draft v0))
    Unsatisfiable
      -of-NNNNN in names
        Slurm array tasks don't know global N
        path-set equality is stronger
      image_digest required
        no container on FarmShare
      commit_sha required
        no root .git in repo
      CI-only payload writes
        CodeBuild can't read /scratch
    Self-contradictory
      3 lifecycle buckets
        promotion invalidates its own gate hash
        breaks CAS byte-sharing
      _catalog/index.json
        mutable file under create-only writes
        concurrent publishers race
    Too rigid
      dataset-level storage{}
        can't hold 2 formats in 1 dataset
      dataset-level splits{}
        splits aren't paths
        profiles disagree on split names
      scalar profile
        real datasets have many payload groups
    Unenforceable decoration
      intended_use free text
      known_limitations free text
      team
        redundant once owner is a group
      ETag as trust anchor
        multipart ETag = part count
    Counterproductive
      Object Lock COMPLIANCE default
        suppresses publishing entirely
```

---

## 10. One-screen cheat sheet

```mermaid
flowchart TB
  Q1["<b>Where?</b><br/>write → s3://edullm-landing<br/>read → s3://edullm-data/&lt;family&gt;/&lt;name&gt;/&lt;version&gt;/<br/>family ∈ pretrain|curriculum|sft|eval|probe|vendor"]
  Q2["<b>What files?</b><br/>dataset.json (first, reserves version)<br/>&lt;group&gt;/manifest.json (LAST = commit)<br/>README.md generated"]
  Q3["<b>How many hand-typed args?</b><br/>FOUR — source, dataset_id, purpose, profile<br/>everything else derived or inherited"]
  Q4["<b>Shard names?</b><br/>&lt;split&gt;-&lt;NNNNN&gt;.&lt;honest-ext&gt;<br/>NO -of-N · CAS + vendored exempt"]
  Q5["<b>Packed tokens?</b><br/>.u32le.bin — NEVER .npy<br/>OLMo-core memmaps from byte 0"]
  Q6["<b>Splits?</b><br/>partitions[] — path/field/range/indices<br/>every one declares rows"]
  Q7["<b>Derived data?</b><br/>depends_on[] pinned by hash<br/>no shared sha256 with parent"]
  Q8["<b>Doesn't fit?</b><br/>experimental/v1 · quota 2/family<br/>lossy · exception outside artifact"]
  Q9["<b>Unknown license?</b><br/>say so — basis: unknown<br/>honest unknown &gt; false MIT<br/>(no PII field — none in scope)"]

  Q1 --> Q2 --> Q3 --> Q4 --> Q5 --> Q6 --> Q7 --> Q8 --> Q9

  style Q3 fill:#1a3a5c,color:#fff
  style Q5 fill:#5c3a1a,color:#fff
  style Q9 fill:#1a3a2a,color:#fff
```

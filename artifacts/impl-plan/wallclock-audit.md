# Wall-clock audit of the 251.2B reservoir build, and a 1.0T projection

**Written** 2026-08-07. **Scope:** every measured wall-clock number in
`/Users/ericwu/Developer/Capstone_LLM-worktrees/edullm-data/final-dataset/`, a
self-inflicted-slowness audit, a fix ranking, and a 1.0T projection twice over
(as-measured and fixed).

**Labels used throughout, per the grading contract:**

- **MEASURED** — a real run produced it. Cited to `file:line`.
- **DERIVED** — arithmetic from MEASURED inputs. The inputs are shown.
- **PROJECTED** — a model with assumptions. The assumptions are named.
- **RETRACTED** — appears in the repo but has been superseded. What superseded it is named.
- **NEVER MEASURED** — a finding in its own right.

**Method note.** This audit takes `artifacts/reservoir/INGEST-CALIBRATION.md` as its
epistemic model: that file contains a correct measurement (0.44 files/s) plus two
confidently wrong conclusions drawn from it, both later retracted in-place, plus a
self-flagged 16x unit error that propagated to another file. Every projection below is
therefore stated with its binding-constraint hypothesis explicit, because the
calibration file's own failure mode was recording a rate without recording *what
limited it*.

---

<!-- Sections are appended one numbered question at a time. -->

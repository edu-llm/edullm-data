# `pretrain/final-dataset` — the mix

> **This file is superseded. Read [`FINAL-DATASET-REPORT.md`](FINAL-DATASET-REPORT.md) instead** —
> it is the current, self-contained plan, and a PDF of it sits beside it.
>
> This stub is kept because earlier commits and notes link to this filename. Its previous contents
> described a mix for a **40B-total / 4B-active** model at a **1.3T** budget with a **flat 54% web
> share**. All three of those are wrong for the model we are building, so the file was replaced
> rather than left to be read by mistake.

## The current plan in five lines

- **Models:** 20.0B total / 96 experts (2 shared + top-4) and 7.11B total / 32 experts (2 shared +
  top-4). Both activate 6 experts, so **active params are 1.876B and 1.873B — 0.17% apart.**
- **Corpus:** ~1.0T unique tokens, dolma2, 50,003,968-token shards.
- **Training:** 1.25–2.0T tokens = 1.25–2.0 epochs. The corpus size and the training budget are
  deliberately different numbers; see the report's §5.
- **Shape:** two stages — 900B bulk at 77% web, then a 100B cooldown at 32% web that concentrates
  math, code, QA and reasoning traces.
- **The baseline model reuses the same corpus** at a different ratio vector. No second dataset.

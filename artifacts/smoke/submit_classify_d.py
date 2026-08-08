#!/usr/bin/env python3
"""Submit candidate model D's classification pass as an AWS Batch GPU job.

Phase 0 task E of DATASET-DESIGN-reservoir.md §9.3/§9.4. `EAI-Distill-0.5b` has **no HF inference
provider** (verified: `inferenceProviderMapping` is empty), so unlike judges A and B it must be
self-hosted. §5.7 requires it run in AWS, not on a laptop — and the local torch install is broken
anyway (`torchvision::nms` ABI mismatch), which is a second, independent reason.

This script only PRINTS the submission payload by default. Pass `--submit` to actually submit.
That split is deliberate: a Batch submission spends money, and Phase 0's rule (§9.6) is that
anything irreversible or over $50 stops for a human. This job is neither — a `g5.xlarge` for well
under an hour — but the dry-run default means the payload can be reviewed before anything starts.

## What the job does

1. `pip install` transformers + torch deps not already in the image
2. download `artifacts/smoke/samples/*.jsonl` and `classify_d.py` from S3
3. run `classify_d.py --device cuda`
4. upload `d_labels.jsonl` back to S3

The samples are small (~2,500 docs x 256-token prefixes = a few MB), so staging them through S3 is
cheap and keeps the job hermetic.

## Why `g5.xlarge` is enough

One A10G, 24 GB. The model is 0.5 B params — ~1 GB in fp16, ~1.3 GB with activations for a batch
of 16 at 256 tokens. The GPU is oversized for this by more than an order of magnitude, which is
fine: it is the only GPU compute environment available (`sbsandbox-intern-edullm-gpu`, verified) and
the job is short.

⚠ **The GPU job def carries `attemptDurationSeconds: 3600`** (verified live). 2,500 documents at
even 2 docs/s is ~20 min, so there is margin — but if this is ever reused for the full 112 M-document
run, that timeout is the first thing that breaks. The full run needs its own job def, more GPUs, and
a re-registered timeout, and it is on the far side of the §9.1 hard stop anyway.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

QUEUE = "sbsandbox-intern-edullm-gpu"
JOB_DEF = "sbsandbox-intern-edullm-gpu-run"

# ⚠️ The staging prefix is NOT arbitrary. `sbsandbox-intern-edullm-batch-gpu-workload`'s only inline
# policy scopes GetObject/PutObject/ListBucket to `teams/*/runs/*` — and its ListBucket carries a
# `s3:prefix` condition on the same pattern. Staging to `teams/data-prep/smoke/` failed with:
#
#   AccessDenied ... not authorized to perform: s3:ListBucket ... because no identity-based
#   policy allows the s3:ListBucket action
#
# The fix is to use the prefix the role already grants, NOT to widen the IAM policy. Least privilege
# here is deliberate infrastructure, and this project's whole premise is that a narrow write scope is
# a feature (the airlock is the same idea one bucket over).
BUCKET = "sbsandbox-intern-edullm-outputs"
PREFIX = "teams/data-prep/runs/smoke-classify-d/"
OUT_PREFIX = "teams/data-prep/runs/smoke-classify-d/out/"
STAGE = f"s3://{BUCKET}/{PREFIX}"

# Installed into the image at runtime rather than baked in: the ECR image is shared infra and
# rebuilding it for a smoke test would be a much larger change than this task warrants.
SCRIPT = r"""
set -euo pipefail
echo "=== candidate D classification (Phase 0 task E) ==="
nvidia-smi || echo "(no nvidia-smi)"
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
pip install -q --no-input "transformers>=4.44" accelerate boto3 2>&1 | tail -2

mkdir -p /tmp/smoke/samples
python - <<'PY'
import boto3, os
s3 = boto3.client("s3")
BUCKET = "sbsandbox-intern-edullm-outputs"
PREFIX = "teams/data-prep/runs/smoke-classify-d/"
# Explicit key list, no ListBucket. The role's ListBucket is prefix-conditioned and a plain
# list_objects_v2 was denied; GetObject on these exact keys is granted, and we know the names
# because we uploaded them. Fewer permissions needed, and it fails loudly on a missing file
# rather than silently classifying a short list.
KEYS = ["classify_d.py"] + [f"samples/{s}.jsonl" for s in
                            ("academic", "finemath", "qa-forum", "reference", "dclm")]
for rel in KEYS:
    dest = os.path.join("/tmp/smoke", rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        s3.download_file(BUCKET, PREFIX + rel, dest)
        print("downloaded", rel, os.path.getsize(dest), "bytes")
    except Exception as e:
        # dclm may legitimately be absent -- it is the source whose upstream API is broken in
        # every direction, and the gate is scoreable without it.
        print("SKIP", rel, type(e).__name__)
PY

cd /tmp/smoke
ls -la samples/ || true
python classify_d.py --samples-dir samples --out d_labels.jsonl --device cuda --batch-size 16
wc -l d_labels.jsonl

python - <<'PY'
import boto3
boto3.client("s3").upload_file(
    "/tmp/smoke/d_labels.jsonl", "sbsandbox-intern-edullm-outputs",
    "teams/data-prep/runs/smoke-classify-d/out/d_labels.jsonl")
print("uploaded d_labels.jsonl")
PY
echo "=== DONE ==="
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--submit", action="store_true", help="actually submit (default: print only)")
    p.add_argument("--job-name", default="edullm-smoke-classify-d")
    args = p.parse_args()

    payload = {
        "jobName": args.job_name,
        "jobQueue": QUEUE,
        "jobDefinition": JOB_DEF,
        "containerOverrides": {"command": ["sh", "-lc", SCRIPT]},
        # Well inside the job def's 3600s; a smoke test that hangs should die, not idle.
        "timeout": {"attemptDurationSeconds": 2700},
    }

    print("=" * 78)
    print("Batch submission payload (candidate D, Phase 0 task E)")
    print("=" * 78)
    print(f"  queue:      {QUEUE}")
    print(f"  job def:    {JOB_DEF}  (g5.xlarge, 1x A10G 24GB, 4 vCPU, 15 GB)")
    print(f"  stage in:   {STAGE}/")
    print(f"  stage out:  {STAGE}-out/d_labels.jsonl")
    print(f"  timeout:    2700s")
    print()
    print("PRE-FLIGHT — upload these first:")
    print(f"  aws s3 cp artifacts/smoke/classify_d.py {STAGE}/classify_d.py")
    print(f"  aws s3 cp artifacts/smoke/samples/     {STAGE}/samples/ --recursive --exclude '*' --include '*.jsonl'")
    print()

    if not args.submit:
        print("DRY RUN — nothing submitted. Re-run with --submit to launch.")
        print("\npayload JSON:")
        print(json.dumps(payload, indent=2)[:1500])
        return 0

    print("SUBMITTING…")
    r = subprocess.run(
        ["aws", "batch", "submit-job", "--cli-input-json", json.dumps(payload)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print("submit FAILED:", r.stderr[:800], file=sys.stderr)
        return 1
    print(r.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

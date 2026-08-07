#!/bin/bash
# =============================================================================================
# measure_finephrase_overlap.sbatch -- the `hash` phase, as a FarmShare job array.
#
# Submit with (see scripts/README-finephrase-overlap.md for the full runbook):
#
#   sbatch --exclude=wheat-01 --array=0-63 \
#     /scratch/users/$USER/finephrase-overlap/measure_finephrase_overlap.sbatch
#
# The `tree` phase runs FIRST, once, on the login node -- it is a few hundred KB of Hub API
# calls and it must not be run per-task (64 tasks each paginating the tree API is both rude and
# a correctness risk: a listing fetched per-task can differ per-task). The `reduce` phase runs
# LAST, once, as a separate single-task job with a different memory shape. Neither belongs here.
#
# ---------------------------------------------------------------------------------------------
# THE RESOURCE REQUEST, AND WHERE EVERY NUMBER COMES FROM
# ---------------------------------------------------------------------------------------------
# All timings below were MEASURED against real files at the pinned revision, from a residential
# link, before this script was written. FarmShare's network is better, so these are conservative.
#
# WORK: 27,104 FinePhrase parquet files (5.16 TB on disk) + 472 FineWeb-Edu sample/350BT files
#       (0.998 TB) = 27,576 files. We read ONLY the `id` column: measured 3.5 MB per FinePhrase
#       file and 33.9 MB per FineWeb-Edu file, so ~111 GB of HTTP in total against 6.16 TB of
#       files -- 1.8%. That ratio is the whole reason this job is affordable.
#
# --cpus-per-task=4
#       This job is I/O bound, not compute bound, and asking for more would only lengthen the
#       queue wait. Measured: sha256-hashing ids costs 0.71 us/id, so the ENTIRE 2.29e9-id census
#       is 0.45 core-hours of actual CPU. Everything else is HTTP latency. 4 CPUs cover the
#       Python thread that hashes, the ~24 prefetch threads (which are blocked on sockets, not
#       runnable), and pyarrow's decompression.
#
# --mem=8G
#       Per task, and it is generous. Peak RSS is dominated by the uint64 key array a task
#       accumulates in memory before writing it: 431 files/task over the whole census
#       = 2.29e9/64 = 36 M ids x 8 B = 286 MB. Add ~2 GB of headroom for the prefetch cache (~34 MB
#       per concurrent reader x 8 readers), pyarrow buffers, and the id strings materialized one
#       row-group-batch at a time (~3 MB). Measured peak on a real 5-file probe: 230 MB. 8 G
#       leaves a >10x margin; 2 G would be tight on the 728-row-group FineWeb-Edu files.
#
# --time=08:00:00
#       Measured 7.9 s per FinePhrase file and 84.8 s per FineWeb-Edu file, single-threaded with
#       prefetch enabled. Total serial work = 27,104 x 7.9 s + 472 x 84.8 s = 71.6 core-hours.
#       At --array=0-63 that is 1.12 h per task; at --workers 8 inside each task the network
#       concurrency cuts it further, but tasks are NOT equal -- the FineWeb-Edu files are 10x
#       slower each and the deterministic stride assignment spreads only 472 of them over 64
#       tasks, so the worst task carries ~8 of them. 8 h is ~7x the mean task and ~4x the worst
#       plausible task. This is deliberately loose: the hash phase is IDEMPOTENT, so a timeout
#       costs only a resubmission, but a task killed at 95% has still done its I/O for nothing.
#       Tighten to 03:00:00 once a first array has reported real per-task times.
#
# --array=0-63
#       64 tasks x 8 in-task readers = 512 concurrent range requests against the Hub. That is
#       the number to lower if HF starts returning 429 (the retry path handles them, but they
#       cost wall clock). It is NOT a correctness parameter -- but --nshards below MUST equal the
#       array size, and the `reduce` phase refuses to run if any shard index is missing.
#
# NO GPU. NO --gres. Nothing here is a tensor.
# =============================================================================================

#SBATCH --job-name=fp-overlap-hash
#SBATCH --output=/scratch/users/%u/finephrase-overlap/logs/hash-%A_%a.out
#SBATCH --error=/scratch/users/%u/finephrase-overlap/logs/hash-%A_%a.err
#SBATCH --array=0-63
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=08:00:00
#SBATCH --exclude=wheat-01

set -euo pipefail

# --- configuration; override by exporting before sbatch, or with --export ---
WORK="${FP_WORK:-/scratch/users/${USER}/finephrase-overlap}"
SCRIPT="${FP_SCRIPT:-${WORK}/measure_finephrase_overlap.py}"
REVISION="${FP_REVISION:-78cf4a5e}"
WORKERS="${FP_WORKERS:-8}"
SAMPLE_MOD="${FP_SAMPLE_MOD:-1}"    # 1 = exact census. See the README before changing.

# NSHARDS must equal the array size, and Slurm tells us that rather than us duplicating it.
NSHARDS="${SLURM_ARRAY_TASK_COUNT:-1}"
SHARD="${SLURM_ARRAY_TASK_ID:-0}"

echo "=== fp-overlap hash shard ${SHARD}/${NSHARDS} on $(hostname) at $(date -u +%FT%TZ)"
echo "    work=${WORK}  revision=${REVISION}  workers=${WORKERS}  sample_mod=${SAMPLE_MOD}"

# Scratch, never home. Home is small and shared; the array writes ~18 GB of key arrays, and the
# reduce phase's sorted spills add ~18 GB more, plus logs.
mkdir -p "${WORK}/logs" "${WORK}/hash"
if [[ "${WORK}" == "${HOME}"* ]]; then
  echo "REFUSING: WORK (${WORK}) is under HOME. Point FP_WORK at /scratch/users/${USER}/..." >&2
  exit 3
fi

# --- environment -------------------------------------------------------------------------------
# Only numpy + pyarrow are needed. Prefer a venv the operator built once (see the README);
# fall back to a module. Fail loudly rather than run on whatever python happens to be first.
if [[ -n "${FP_VENV:-}" ]]; then
  # shellcheck disable=SC1091
  source "${FP_VENV}/bin/activate"
elif [[ -f "${WORK}/venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "${WORK}/venv/bin/activate"
else
  source /etc/profile.d/z00_lmod.sh
  module load python/3.11 || module load python
fi

python3 - <<'PYCHECK'
import sys
import numpy, pyarrow
print(f"    python {sys.version.split()[0]}  numpy {numpy.__version__}  "
      f"pyarrow {pyarrow.__version__}")
PYCHECK

# Keep numeric libs from spawning a thread per core inside an I/O-bound job; the CPU we asked
# for is for hashing and decompression, and oversubscription here only adds context switches.
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export MKL_NUM_THREADS=2

# HF token if the operator staged one. These repos are public, so absent is fine -- but an
# authenticated request gets a higher rate limit, which matters at 512 concurrent readers.
if [[ -z "${HF_TOKEN:-}" && -f "${HOME}/.cache/huggingface/token" ]]; then
  export HF_TOKEN="$(cat "${HOME}/.cache/huggingface/token")"
fi

# --- preflight: the tree phase must already have run --------------------------------------------
if [[ ! -f "${WORK}/tree.json" ]]; then
  echo "REFUSING: ${WORK}/tree.json is missing. Run the tree phase ONCE on the login node:" >&2
  echo "  python3 ${SCRIPT} tree --work ${WORK} --revision ${REVISION}" >&2
  exit 4
fi

# --- the work ------------------------------------------------------------------------------------
# Idempotent: a shard whose .npz + .json already exist and validate is skipped, so resubmitting
# the array after a partial failure resumes rather than redoes. --force overrides.
srun --cpu-bind=none python3 "${SCRIPT}" hash \
  --work "${WORK}" \
  --shard "${SHARD}" \
  --nshards "${NSHARDS}" \
  --workers "${WORKERS}" \
  --sample-mod "${SAMPLE_MOD}" \
  ${FP_EXTRA_ARGS:-}

echo "=== shard ${SHARD} done at $(date -u +%FT%TZ)"
ls -la "${WORK}/hash/shard-$(printf '%05d' "${SHARD}")".* || true

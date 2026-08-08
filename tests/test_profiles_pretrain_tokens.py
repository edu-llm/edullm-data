

# --------------------------------------------------------------------------------------
# One HEAD per object per run, shared across checks
# --------------------------------------------------------------------------------------


def _spy_ctx(n=6, seq_len=8192, check_workers=1):
    """A GroupContext over `n` clean shards whose S3 counts its own calls.

    The counters are lock-guarded because the profile now fans its reads out over threads and
    `self.heads += 1` is a read-modify-write: an unguarded spy would UNDERCOUNT under concurrency
    and the round-trip assertions below would pass for the wrong reason.
    """
    import hashlib
    import threading

    import numpy as np

    from edullm_data.profiles.base import GroupContext

    objs = {}
    entries = []
    for i in range(n):
        ids = np.random.default_rng(i).integers(1, 100000, size=seq_len * 2, dtype="<u4")
        ids[-1] = 100257
        body = ids.tobytes()
        path = f"tokens/train-{i:05d}.u32le.bin"
        objs["pfx/" + path] = body
        entries.append(
            {
                "path": path,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "count": {"unit": "tokens", "value": len(body) // 4},
                "format": {
                    "byte_order": "little",
                    "codec": "none",
                    "container": "raw",
                    "dtype": "uint32",
                    "header_bytes": 0,
                },
                "split": "train",
            }
        )

    class _Spy:
        def __init__(self):
            self._lock = threading.Lock()
            self.heads = 0
            self.head_keys = []
            self.ranges = 0
            self.range_keys = []
            self.threads = set()

        def head(self, bucket, key):
            with self._lock:
                self.heads += 1
                self.head_keys.append(key)
                self.threads.add(threading.current_thread().name)
            return {"size": len(objs[key])}

        def get_range(self, bucket, key, off, n_):
            with self._lock:
                self.ranges += 1
                self.range_keys.append((key, off, n_))
                self.threads.add(threading.current_thread().name)
            return objs[key][off : off + n_]

    return GroupContext(
        dataset_id="pretrain/x",
        version="v1",
        landing_bucket="b",
        prefix="pfx",
        group={"name": "tokens", "profile": "pretrain-tokens/v1", "seq_len": seq_len},
        manifest={"entries": entries},
        s3=_Spy(),
        rng_seed="00" * 32,
        family_defaults={},
        resolved={"tokenizer": {"vocab_size": 100278, "eos_token_id": 100257}},
        observations={},
        check_workers=check_workers,
    )


def test_the_size_head_is_shared_between_decode_smoke_and_seq_len():
    """Both checks need the same fact (the REAL object size) about the same key, and each used to
    HEAD for it separately — three HEADs per entry counting `validate`'s own loop, i.e. ~30,000
    round trips to learn 10,049 sizes. Measured live: Gate A ran 85 min at 0.3% CPU, and that is
    what pushed the first promotion attempt past its 2 h timeout.
    """
    from edullm_data.profiles import pretrain_tokens_v1 as P

    ctx = _spy_ctx(n=6)
    assert P.check_decode_smoke(ctx) == []
    assert P.check_seq_len_alignment(ctx) == []
    # 6 objects, two checks that each want a size => 6 HEADs, not 12.
    assert ctx.s3.heads == 6, ctx.s3.heads
    assert len(set(ctx.s3.head_keys)) == 6
    assert len(ctx.observations["object_sizes"]) == 6


def test_the_cached_size_is_the_observed_one_not_the_declared_one():
    """The reason these checks HEAD at all is that a truncated tail must be sampled and checked
    against REALITY. Caching must not quietly turn that into trusting `entry.bytes` — which would
    make both checks decoration under the golden rule."""
    from edullm_data.profiles import pretrain_tokens_v1 as P

    ctx = _spy_ctx(n=3)
    # Declare a size larger than the object really is, exactly like a truncated upload.
    for e in ctx.manifest["entries"]:
        e["bytes"] = e["bytes"] + 4096

    entry = ctx.manifest["entries"][0]
    key = "pfx/" + entry["path"]
    observed = P._observed_size(ctx, key)
    declared = entry["bytes"]
    assert observed == declared - 4096, (observed, declared)
    assert observed != declared, "the cached size came from the manifest, not from S3"

    # And the check that depends on it still fires against reality: a declared size 4096 over the
    # real one is a whole multiple of the stride, so a manifest-trusting version would report clean.
    ctx.observations.clear()
    assert P.check_decode_smoke(ctx) == []  # real bytes are fine; only `bytes` was inflated


def test_a_repeated_key_costs_one_head():
    """Idempotent per key within a run — the cache is keyed on the object key, so asking twice is
    one round trip."""
    from edullm_data.profiles import pretrain_tokens_v1 as P

    ctx = _spy_ctx(n=2)
    key = "pfx/" + ctx.manifest["entries"][0]["path"]
    a = P._observed_size(ctx, key)
    b = P._observed_size(ctx, key)
    assert a == b
    assert ctx.s3.heads == 1, ctx.s3.heads


# --------------------------------------------------------------------------------------
# B3 / task #10 — the profile checks fan their reads out, and the verdict does not move
# --------------------------------------------------------------------------------------


def _run_all_checks(ctx):
    from edullm_data.profiles import pretrain_tokens_v1 as P

    out = []
    for check in P.CHECKS:
        out.extend(check(ctx))
    return out


def test_threading_does_not_change_the_round_trip_COUNT_only_who_waits():
    """Concurrency must move latency, never work. A fan-out that re-reads what the sequential path
    read once is not a speedup, it is a bill — and at 40,001 objects an accidental extra call per
    object is 40,001 extra round trips that no test asserting "it got faster" would catch.

    RECOMPUTED, not declared: the spy counts every call, and the two worker counts are compared to
    each other rather than to a hardcoded number, so this stays true if the per-object call count
    is later reduced (which is exactly what step 3 of §8.3 proposes).
    """
    serial = _spy_ctx(n=12, check_workers=1)
    threaded = _spy_ctx(n=12, check_workers=8)

    assert _run_all_checks(serial) == []
    assert _run_all_checks(threaded) == []

    assert (threaded.s3.heads, threaded.s3.ranges) == (serial.s3.heads, serial.s3.ranges), (
        f"threaded {threaded.s3.heads}H/{threaded.s3.ranges}R vs "
        f"serial {serial.s3.heads}H/{serial.s3.ranges}R"
    )
    # And the sets of calls are identical, not merely equinumerous: same keys, same byte windows.
    assert sorted(threaded.s3.head_keys) == sorted(serial.s3.head_keys)
    assert sorted(threaded.s3.range_keys) == sorted(serial.s3.range_keys)

    # The point of the exercise: the serial run stayed on one thread, the threaded one did not.
    assert len(serial.s3.threads) == 1, serial.s3.threads
    assert len(threaded.s3.threads) > 1, (
        "check_workers > 1 issued every call from one thread — the fan-out is not wired"
    )


def test_the_violation_LIST_is_element_for_element_identical_at_every_worker_count():
    """A threading change that alters results is a correctness bug, not a speedup.

    Gate A's verdict is a property of the violation SET, but the report a human reads is an ORDERED
    list and `duplicate-shard-digest` elsewhere decides which of two identical objects to name by
    iteration order. So the bar here is equality of the LIST, not of the set: the checks must still
    decide sequentially in manifest order no matter how the bytes arrived.

    The corpus is deliberately broken in four different ways at four different positions, so an
    ordering change would show up as a reordered list rather than as a silent pass.
    """
    import numpy as np

    def broken(workers):
        ctx = _spy_ctx(n=9, check_workers=workers)
        objs = {}
        # Rebuild the payloads so the defects are in the BYTES, which is what gets recomputed.
        for i, e in enumerate(ctx.manifest["entries"]):
            key = "pfx/" + e["path"]
            if i == 2:
                body = b"\x93NUMPY" + b"\x00" * 65530  # npy magic where raw was declared
            elif i == 4:
                body = np.full(16384, 7, dtype="<u4").tobytes()  # all one token
            elif i == 6:
                body = np.zeros(16384, dtype="<u4").tobytes()  # zero-filled tail
            elif i == 7:
                body = np.full(16384, 100257, dtype="<u4").tobytes()  # all-EOS
            else:
                body = np.random.default_rng(i).integers(
                    1, 100000, size=16384, dtype="<u4"
                ).tobytes()
            objs[key] = body
            e["bytes"] = len(body)
            e["count"] = {"unit": "tokens", "value": len(body) // 4}
        # Repoint the spy at the new bodies.
        import threading

        lock = threading.Lock()

        class _S:
            def __init__(self):
                self.heads = 0
                self.ranges = 0

            def head(self, bucket, key):
                with lock:
                    self.heads += 1
                return {"size": len(objs[key])}

            def get_range(self, bucket, key, off, n_):
                with lock:
                    self.ranges += 1
                return objs[key][off : off + n_]

        ctx.s3 = _S()
        return _run_all_checks(ctx)

    serial = broken(1)
    assert serial, "the fixture stopped producing violations; the comparison would be vacuous"
    codes = [v.code for v in serial]
    assert "npy-magic-bytes" in codes and "distinct-too-few" in codes, codes

    for workers in (2, 4, 16, 64):
        assert broken(workers) == serial, (
            f"check_workers={workers} produced a different violation list:\n"
            f"  {[str(v) for v in broken(workers)]}\n!= {[str(v) for v in serial]}"
        )


def test_a_prefetch_failure_degrades_to_the_sequential_read_rather_than_failing_the_group():
    """The prefetch must never be load-bearing.

    If a warm-up call raised, `_validate_group` would convert it into one group-level
    `profile-check-error` and DESTROY the precise per-entry violation the sequential path produces
    — turning "shard 7 has npy magic bytes" into "group tokens: check errored". So a failed
    prefetch leaves the cache cold and the sequential read re-issues the call and raises (or
    succeeds) exactly as it does today.
    """
    import threading

    ctx = _spy_ctx(n=6, check_workers=8)
    real_range, real_head = ctx.s3.get_range, ctx.s3.head
    main = threading.current_thread()

    # Fail EVERY call made off the calling thread — i.e. every prefetch, and only prefetches. That
    # isolates the claim: with the entire warm-up failing, the checks must still be correct.
    def off_main():
        return threading.current_thread() is not main

    rejected = []

    def guard(real):
        def wrapper(*a):
            if off_main():
                rejected.append(a)
                raise RuntimeError("transient")
            return real(*a)

        return wrapper

    ctx.s3.get_range = guard(real_range)
    ctx.s3.head = guard(real_head)

    assert _run_all_checks(ctx) == []
    # And it really did try: the fan-out ran, every one of its calls was rejected, and the answer
    # still came out right — so the prefetch is an optimisation, not a dependency.
    assert rejected, "no call was made off the calling thread; the fan-out never ran"
    assert ctx.s3.threads == {main.name}, (
        f"a worker call succeeded, so the fallback was not what produced the result: {ctx.s3.threads}"
    )

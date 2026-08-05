

# --------------------------------------------------------------------------------------
# One HEAD per object per run, shared across checks
# --------------------------------------------------------------------------------------


def _spy_ctx(n=6, seq_len=8192):
    """A GroupContext over `n` clean shards whose S3 counts its own calls."""
    import hashlib

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
            self.heads = 0
            self.head_keys = []
            self.ranges = 0

        def head(self, bucket, key):
            self.heads += 1
            self.head_keys.append(key)
            return {"size": len(objs[key])}

        def get_range(self, bucket, key, off, n_):
            self.ranges += 1
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

"""`Boto3S3.default` must be able to size its HTTP connection pool.

The failure this guards against is SILENT and shaped like an absent speedup, which is the hardest
kind to notice: botocore's default `max_pool_connections` is 10, and botocore does not pass
`block=True` to urllib3, so exceeding the pool neither raises nor waits — urllib3's `_put_conn`
DISCARDS the surplus connection and logs "Connection pool is full". Workers 11..N then pay a fresh
TLS handshake per request. Nothing fails; the number just does not improve.

Gate A is where that bites: `--head-workers 16` plus a threaded profile-check fan-out puts far more
than 10 requests in flight against one client.
"""


def test_botocores_default_pool_is_still_ten():
    """RECOMPUTED from the installed botocore rather than quoted from a comment.

    Every "size the pool" decision in this repo is justified by this number. If a botocore upgrade
    changes it, that should be a failing test naming the assumption, not a stale comment in four
    files.
    """
    from botocore.config import Config

    assert Config().max_pool_connections == 10, (
        "botocore's default pool size changed; revisit Boto3S3.default, validate.main and "
        "corpus_build._s3, all of which are written around 10"
    )


def test_default_builds_the_untouched_client_when_no_pool_size_is_asked_for():
    """The single-threaded path must be byte-for-byte what it was before the parameter existed:
    no `Config` passed at all, so botocore applies its own defaults."""
    from edullm_data.s3 import Boto3S3

    s3 = Boto3S3.default()
    assert s3._c.meta.config.max_pool_connections == 10


def test_default_sizes_the_pool_when_asked():
    """RECOMPUTED off the constructed client's own config, not off the argument we passed in —
    asserting `18 == 18` against our own input would be decoration. This reads back what botocore
    actually built."""
    from edullm_data.s3 import Boto3S3

    assert Boto3S3.default(max_pool_connections=18)._c.meta.config.max_pool_connections == 18
    assert Boto3S3.default(max_pool_connections=34)._c.meta.config.max_pool_connections == 34


def test_the_region_is_still_honoured_on_the_sized_path():
    """A second construction path is a second chance to drop an argument. Both branches must
    produce a client for the same region — the validator runs in us-east-1 and a client pointed
    somewhere else fails at request time, not at construction."""
    from edullm_data.s3 import Boto3S3

    for kwargs in ({}, {"max_pool_connections": 18}):
        assert Boto3S3.default("us-east-2", **kwargs)._c.meta.region_name == "us-east-2"

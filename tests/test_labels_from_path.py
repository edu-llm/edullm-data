"""``entry.labels`` is derived from the key and recomputed by Gate A.

A corpus that keeps its sources in the key already states which slice a shard belongs to.
Before this, that statement was readable only by string-parsing a URI in the trainer — the
raw-globbing the reader exists to replace — and ``entry.labels`` sat unpopulated, so the
schema-v2 field was decoration in the sense ``CONTRIBUTING.md`` warns about: nothing wrote it
and nothing read it.

Two halves, and the second is what makes the first worth anything:

* ``publish()`` derives ``labels`` (and ``split``) from the object's own key. Nothing is asked
  of the caller, so nothing can be mistyped.
* Gate A RECOMPUTES both from that same key and rejects a disagreement. A hand-typed label
  would be a producer assertion no gate falsifies — exactly the class of claim that let a
  ``.npy`` extension sit on headerless bytes for months.

The urgency is that ``labels`` lives inside ``manifest_sha256`` (``ManifestEntry.to_dict``
emits it; ``manifest_sha256`` hashes the canonical JSON). A published dataset therefore cannot
gain labels later without republishing every payload byte — for the 150B corpus, a 630 GB
re-copy. Labels are cheap to write now and expensive to add afterwards.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from edullm_data import publish as P
from edullm_data import validate as V
from edullm_data.contracts import canonical_json
from edullm_data.manifest import ManifestEntry, labels_from_path
from edullm_data.s3 import FakeS3

CREATED = "2026-07-30T00:00:00Z"
ENV = {"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64}
TOKENIZER = {
    "repo_id": "allenai/dolma2-tokenizer",
    "revision": "abc123",
    "fingerprint_sha256": "c" * 64,
    "vocab_size": 100278,
    "eos_token_id": 100257,
}


# ---- the derivation itself ----

@pytest.mark.parametrize(
    "path,expected",
    [
        ("tokens/train-00000.u32le.bin", {}),
        ("tokens/wikipedia/train-00040.u32le.bin", {"source": "wikipedia"}),
        (
            "tokens/s2pdf-redacted/games/train-00861.u32le.bin",
            {"source": "s2pdf-redacted", "domain": "games"},
        ),
        (
            "tokens/all-dressed-snazzy2/travel_and_tourism/val-00000.u32le.bin",
            {"source": "all-dressed-snazzy2", "domain": "travel_and_tourism"},
        ),
    ],
)
def test_labels_come_from_the_segments_between_group_and_basename(path, expected):
    assert labels_from_path(path) == expected


def test_a_flat_key_yields_no_labels_rather_than_empty_strings():
    """A flat layout has nothing to say. It must not invent ``{"source": ""}``."""
    assert labels_from_path("tokens/train-00000.u32le.bin") == {}


def test_a_tree_deeper_than_we_can_name_is_refused_not_guessed():
    """Silently dropping a segment ships a label that is true but incomplete.

    Inventing ``level_3`` would put an unnamed dimension in the hash chain forever. Both are
    worse than refusing, because labels cannot be corrected without republishing the payload.
    """
    with pytest.raises(ValueError) as e:
        labels_from_path("tokens/a/b/c/train-00000.u32le.bin")
    assert "3 levels" in str(e.value)
    assert "manifest_sha256" in str(e.value)  # says WHY it cannot just be fixed later


def test_the_key_vocabulary_is_overridable_for_a_corpus_with_another_axis():
    got = labels_from_path("tokens/en/wiki/legal/train-00000.u32le.bin",
                           keys=("lang", "corpus", "domain"))
    assert got == {"lang": "en", "corpus": "wiki", "domain": "legal"}


# ---- publish populates it ----

def _publish(s3: FakeS3, dsid: str, *, deep: bool = False):
    d = Path(tempfile.mkdtemp())

    def shard(p: Path, n: int, seed: int) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(seed)
        p.write_bytes(rng.integers(1, 100278, size=n, dtype=np.uint32).tobytes())

    shard(d / "tokens" / "s2pdf-redacted" / "games" / "train-00000.u32le.bin", 30000, 1)
    shard(d / "tokens" / "s2pdf-redacted" / "games" / "val-00000.u32le.bin", 20000, 2)
    shard(d / "tokens" / "wikipedia" / "train-00001.u32le.bin", 30000, 3)
    if deep:
        shard(d / "tokens" / "a" / "b" / "c" / "train-00002.u32le.bin", 30000, 4)
    return P.publish(
        d,
        dataset_id=dsid,
        purpose="fixture corpus for key-derived entry labels and their Gate A recompute",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta={"tokens": {"tokenizer": TOKENIZER}},
        env=ENV,
    )


def _entries(s3: FakeS3, dsid: str, version: str) -> dict[str, dict]:
    raw = json.loads(s3.get("edullm-landing", f"{dsid}/{version}/tokens/manifest.json"))
    return {e["path"]: e for e in raw["entries"]}


def test_publish_writes_labels_and_split_without_being_asked():
    """The regression: both fields existed, were validated, and were never populated."""
    s3, dsid = FakeS3(), "pretrain/labelwrite-10b"
    plan = _publish(s3, dsid)
    entries = _entries(s3, dsid, plan.version)

    nested = entries["tokens/s2pdf-redacted/games/train-00000.u32le.bin"]
    assert nested["labels"] == {"source": "s2pdf-redacted", "domain": "games"}
    assert nested["split"] == "train"

    one_level = entries["tokens/wikipedia/train-00001.u32le.bin"]
    assert one_level["labels"] == {"source": "wikipedia"}

    assert entries["tokens/s2pdf-redacted/games/val-00000.u32le.bin"]["split"] == "val"


def test_a_flat_publish_emits_no_labels_key_at_all():
    """Byte-for-byte unchanged for flat datasets, so no existing manifest shifts."""
    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    (d / "tokens").mkdir(parents=True)
    rng = np.random.default_rng(9)
    for name, n in (("train-00000.u32le.bin", 30000), ("val-00000.u32le.bin", 20000)):
        (d / "tokens" / name).write_bytes(
            rng.integers(1, 100278, size=n, dtype=np.uint32).tobytes()
        )
    plan = P.publish(
        d,
        dataset_id="pretrain/flatlabels-10b",
        purpose="confirm a flat layout still emits no labels key so its manifest is unchanged",
        profile="pretrain-tokens/v1",
        s3=s3,
        created_at=CREATED,
        group_meta={"tokens": {"tokenizer": TOKENIZER}},
        env=ENV,
    )
    for entry in _entries(s3, "pretrain/flatlabels-10b", plan.version).values():
        assert "labels" not in entry


def test_publishing_a_tree_too_deep_to_label_fails_loudly_at_publish_time():
    """Before the copy, not after — and never as an unlabelled shard."""
    with pytest.raises(P.PublishError) as e:
        _publish(FakeS3(), "pretrain/deepnest-10b", deep=True)
    assert "nests 3 levels" in str(e.value)


# ---- Gate A recomputes it ----

def _tamper(tamper, slug: str):
    s3, dsid = FakeS3(), f"pretrain/{slug}-10b"
    plan = _publish(s3, dsid)
    key = f"{dsid}/{plan.version}/tokens/manifest.json"
    man = json.loads(s3.get("edullm-landing", key))
    tamper(man)
    s3.seed("edullm-landing", key, canonical_json(man))
    return V.validate_dataset(
        "edullm-landing", f"{dsid}/{plan.version}", s3, data_bucket="edullm-data"
    )


def test_an_untampered_nested_publish_validates_clean():
    s3, dsid = FakeS3(), "pretrain/labelclean-10b"
    plan = _publish(s3, dsid)
    res = V.validate_dataset(
        "edullm-landing", f"{dsid}/{plan.version}", s3, data_bucket="edullm-data"
    )
    assert res.ok, [str(v) for v in res.violations]


def test_a_label_that_lies_about_its_source_is_rejected():
    """THE check. Without it a mixture computed from labels is silently wrong."""
    res = _tamper(
        lambda m: m["entries"][0].__setitem__("labels", {"source": "wikipedia", "domain": "games"}),
        "labellie",
    )
    assert "labels-contradict-path" in {v.code for v in res.violations}
    assert not res.ok


def test_labels_stripped_from_a_nested_entry_are_rejected():
    """Absence on a nested key is not neutral — a label-partitioned read drops the object."""
    res = _tamper(lambda m: m["entries"][0].pop("labels", None), "labelstrip")
    assert "labels-contradict-path" in {v.code for v in res.violations}


def test_an_extra_label_key_nothing_recomputes_is_rejected():
    """Equality both ways: a key the path cannot justify is a claim no gate can check."""
    res = _tamper(
        lambda m: m["entries"][0].__setitem__(
            "labels", {"source": "s2pdf-redacted", "domain": "games", "secret": "x"}
        ),
        "labelextra",
    )
    assert "labels-contradict-path" in {v.code for v in res.violations}


def test_the_violation_explains_the_consequence_not_just_the_mismatch():
    res = _tamper(
        lambda m: m["entries"][0].__setitem__("labels", {"source": "wikipedia", "domain": "games"}),
        "labelmsg",
    )
    msg = next(str(v) for v in res.violations if v.code == "labels-contradict-path")
    assert "s2pdf-redacted" in msg and "wikipedia" in msg
    assert "mixture" in msg


# ---- the reason this had to ship before the payload ----

def test_labels_are_inside_the_hash_chain_so_they_cannot_be_backfilled():
    """Why this is a pre-publish blocker rather than a nice-to-have.

    ``to_dict`` emits ``labels``, and ``manifest_sha256`` hashes the canonical JSON of the
    manifest — so adding a label to a published dataset MOVES the hash the seal and every
    ``depends_on`` pin are written against. The sanctioned in-place backfill is
    descriptive-keys-only and asserts ``manifest_sha256`` is byte-identical, which this
    would violate. Adding labels after the fact therefore means republishing the payload.
    """
    from edullm_data.manifest import build_manifest, manifest_sha256

    common = {"sha256": "d" * 64, "bytes": 40, "count": {"unit": "tokens", "value": 10}}
    bare = ManifestEntry(path="tokens/wikipedia/train-00000.u32le.bin", **common)
    with_labels = ManifestEntry(
        path="tokens/wikipedia/train-00000.u32le.bin", labels={"source": "wikipedia"}, **common
    )
    assert "labels" not in bare.to_dict()
    assert with_labels.to_dict()["labels"] == {"source": "wikipedia"}
    a = manifest_sha256(build_manifest([bare], group_name="tokens"))
    b = manifest_sha256(build_manifest([with_labels], group_name="tokens"))
    assert a != b, "if these matched, labels could be backfilled and this would not be urgent"


# ---- label-segment-unsafe: reject what BREAKS, not what looks unusual ----


def test_the_hash_segment_loses_the_shard_name():
    """The measured failure, and the reason this check exists at all.

    A corpus with `tokens/stackv2-edu/C#/train-00000.u32le.bin` publishes clean and passes Gate A
    with zero violations — verified live. Then urlparse puts everything after the '#' into
    `fragment` and the shard name is gone from `path`.
    """
    from urllib.parse import urlparse

    from edullm_data.validate import _segment_breakage

    key = "tokens/stackv2-edu/C#/train-00000.u32le.bin"
    parsed = urlparse(f"s3://edullm-data/pretrain/x/v1/{key}")
    assert "train-00000" not in parsed.path, "the premise: the name really is lost"
    assert parsed.fragment == "/train-00000.u32le.bin"
    assert _segment_breakage("C#") is not None
    assert "SHARD NAME DISAPPEARS" in _segment_breakage("C#")


def test_a_bracket_segment_fails_to_match_its_own_glob():
    from fnmatch import fnmatch

    from edullm_data.validate import _segment_breakage

    key = "tokens/src/a[b]/train-00000.u32le.bin"
    assert not fnmatch(key, key), "the premise: a literal glob misses its own key"
    assert _segment_breakage("a[b]") is not None


def test_safe_but_unusual_segments_are_ACCEPTED():
    """The false positives that a lowercase-kebab rule would reject.

    `SAFE_SEGMENT_RE` is the right rule for a value this package GENERATES, and the wrong rule for
    a validator: it conflates style with danger. `tokens/stack-edu/Python/...` is what the existing
    label-selection fixtures use and it is entirely safe. Rejecting a legal corpus is the more
    expensive error — the bytes are frozen and the fix is a full re-copy.
    """
    from fnmatch import fnmatch
    from urllib.parse import urlparse

    from edullm_data.validate import _segment_breakage

    for seg in ("Python", "C++", "Jupyter Notebook", "MathOverflow", "3dprinting", "naïve"):
        assert _segment_breakage(seg) is None, f"{seg!r} is safe and must be accepted"
        key = f"tokens/src/{seg}/train-00000.u32le.bin"
        assert "train-00000" in urlparse(f"s3://b/p/{key}").path
        assert fnmatch(key, key)


def test_gate_a_rejects_a_published_hash_segment():
    """End to end: the corpus that used to pass with zero violations now fails."""
    import tempfile
    from pathlib import Path

    import numpy as np

    from edullm_data import publish as P
    from edullm_data import validate as V
    from edullm_data.s3 import FakeS3

    s3 = FakeS3()
    d = Path(tempfile.mkdtemp())
    g = d / "tokens" / "stackv2-edu" / "C#"
    g.mkdir(parents=True)
    rng = np.random.default_rng(1)
    for i, split in ((0, "train"), (1, "train"), (2, "val")):
        (g / f"{split}-{i:05d}.u32le.bin").write_bytes(
            rng.integers(1, 100278, size=40000, dtype=np.uint32).tobytes()
        )
    plan = P.publish(
        d, dataset_id="pretrain/unsafe-segment-gate",
        purpose="confirm Gate A now rejects an inherited domain value that breaks a consumer",
        profile="pretrain-tokens/v1", s3=s3, created_at="2026-08-01T00:00:00Z",
        group_meta={"tokens": {"tokenizer": {
            "repo_id": "allenai/dolma2-tokenizer", "revision": "a",
            "fingerprint_sha256": "c" * 64, "vocab_size": 100278, "eos_token_id": 100257}}},
        env={"EDULLM_CODE_SHA256": "a" * 64, "EDULLM_PACKAGES_LOCK_SHA256": "b" * 64},
    )
    res = V.validate_dataset(
        "edullm-landing", f"pretrain/unsafe-segment-gate/{plan.version}", s3,
        data_bucket="edullm-data")
    assert not res.ok
    codes = [v.code for v in res.violations]
    assert "label-segment-unsafe" in codes, codes

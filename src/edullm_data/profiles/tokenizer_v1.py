"""``tokenizer/v1`` — a published tokenizer as a first-class, owned artifact.

The tokenizer is the single most essential dependency of a token corpus: the shards are
meaningless integers without it. Pinning it by reference (repo_id + revision on HuggingFace)
is the exact dangling-dependency failure the standard exists to kill, pointed at HF instead
of ``/scratch`` — if the upstream repo moves or vanishes, every pretrain dataset becomes
undecodable. So the tokenizer is *published* into the airlock like any other dataset, and
pretrain/curriculum datasets ``depends_on`` it.

The payoff: ``vocab_size`` and ``eos_token_id`` stop being hand-typed guesses. They are
COMPUTED from the published ``tokenizer.json`` (rule §0.4, recompute-never-trust), and the
decode smoke test's ``id < vocab_size`` bound is finally real.

A tokenizer dataset is tiny (a few MB) and published once; every corpus references it.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from .base import GroupContext, Violation

NAME = "tokenizer/v1"

#: The group must contain a tokenizer.json (the HF-format tokenizer definition). Other files
#: (merges, special_tokens_map, config) are allowed but tokenizer.json is the one we derive
#: vocab from, so its presence is required and checked against bytes below.
REQUIRED_FIELDS: Mapping[str, Any] = {
    # No hand-typed vocab_size/eos here on purpose: those are DERIVED from tokenizer.json by
    # derive_vocab() and must not be asserted by a human (that would reintroduce the guess).
}

TOKENIZER_JSON = "tokenizer.json"


def _object_key(prefix: str, path: str) -> str:
    if not prefix:
        return path
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def derive_vocab(tokenizer_json_bytes: bytes) -> dict[str, Any]:
    """Compute ``{vocab_size, eos_token_id}`` from a HuggingFace ``tokenizer.json``.

    Pure parsing, no heavy library — tokenizer.json is JSON. ``vocab_size`` = base model
    vocab + added_tokens (added tokens extend the id space, so they count). ``eos_token_id``
    is resolved from ``added_tokens``/special tokens when discoverable, else left None (the
    corpus can still assert it explicitly, but it's derived when possible).
    """
    doc = json.loads(tokenizer_json_bytes)
    model = doc.get("model", {})
    vocab = model.get("vocab", {})
    base = len(vocab) if isinstance(vocab, (dict, list)) else 0
    added = doc.get("added_tokens", []) or []
    # added_tokens carry explicit ids; the true vocab size is max(id)+1 over base + added,
    # which handles gaps/padding between base vocab and added specials.
    max_id = base - 1
    eos_id = None
    for t in added:
        tid = t.get("id")
        if isinstance(tid, int):
            max_id = max(max_id, tid)
            content = t.get("content", "")
            if content in ("<|endoftext|>", "</s>", "<eos>", "<|eot_id|>") or "endoftext" in content:
                eos_id = tid
    vocab_size = max_id + 1 if max_id >= 0 else base
    return {"vocab_size": vocab_size, "eos_token_id": eos_id}


def check_tokenizer_json_present_and_valid(ctx: GroupContext) -> list[Violation]:
    """tokenizer.json must exist in the group and parse, and vocab must be derivable from it.
    This is the recompute: we read the actual file, not a metadata claim."""
    entries = ctx.manifest.get("entries", [])
    tj_path = None
    for raw in entries:
        p = raw.get("path") if isinstance(raw, Mapping) else None
        if p and p.rsplit("/", 1)[-1] == TOKENIZER_JSON:
            tj_path = p
            break
    if tj_path is None:
        return [Violation("tokenizer-json-missing", f"a {NAME} group must contain {TOKENIZER_JSON}", path=ctx.group.get("name"))]
    key = _object_key(ctx.prefix, tj_path)
    try:
        body = ctx.s3.get(ctx.landing_bucket, key)  # a tokenizer.json is a few MB — safe to read whole
    except Exception as e:  # noqa: BLE001
        return [Violation("tokenizer-json-unreadable", f"{tj_path}: {e}", path=tj_path)]
    try:
        derived = derive_vocab(body)
    except (ValueError, KeyError) as e:
        return [Violation("tokenizer-json-invalid", f"{tj_path}: not a parseable HF tokenizer.json ({e})", path=tj_path)]
    if not derived.get("vocab_size"):
        return [Violation("tokenizer-vocab-underivable", f"{tj_path}: could not derive a vocab_size", path=tj_path)]
    return []


CHECKS = [check_tokenizer_json_present_and_valid]


try:
    from . import registry as _registry

    _registry.register(sys.modules[__name__])
except Exception:  # noqa: BLE001
    pass

"""The reader refuses an object that is not an S3 adapter, at the boundary, by name.

**THIS IS THE TEST THAT WOULD HAVE CAUGHT FOUR FAILED PLATFORM RUNS ON 2026-08-01.**
OLMo-core's ``.edullm/train_on_corpus.py`` passed ``boto3.client("s3")`` where the reader
wanted ``Boto3S3``, and every one of those runs died with
``AttributeError: 'S3' object has no attribute 'head'`` from inside ``_require_validated``.

The reason nothing here caught it is worth stating, because it is not "the path had no test".
``_require_validated`` is exercised by ``test_read.py::test_refuses_unvalidated``, and it is
exercised against :class:`FakeS3`, which implements ``head`` correctly. **A faithful fake is
precisely what cannot stand in for a caller who passes something that is not an
implementation at all.** No test built on the package's own double can reach this bug, however
many of them there are, so the missing guard was never a unit test of the path. It is an
assertion at the boundary where an arbitrary object is accepted.

Nor could the type checker help. boto3 ships no type information, so ``boto3.client("s3")`` is
``Any``, and ``Any`` satisfies the ``s3: S3`` annotation. The annotation is correct and buys
nothing at that call site.
"""

from __future__ import annotations

import pytest

from edullm_data import read as R
from edullm_data.s3 import FakeS3, require_s3_adapter


#: The phrase only the boto3 branch produces.
#:
#: Not the bare word ``boto3``: the generic message quotes ``type(candidate).__module__``, which
#: for a resource is ``boto3.resources.factory``, so the word appears in both messages and an
#: assertion on it distinguishes nothing. This is the sentence that tells somebody which of the
#: two classes named ``S3`` they are holding, and it is the thing worth holding onto.
NAMES_BOTO3 = "was given a boto3 object"


def _boto3_client() -> object:
    """The real thing, not a stand-in.

    Constructing a client contacts nothing and needs no credentials, so this is a real
    ``boto3.client("s3")`` rather than an imitation of one. That matters more than usual here:
    the bug being guarded against is entirely about the true shape of that object, and a
    handmade double would be me asserting my own belief about boto3 and calling it evidence.

    It also keeps the test honest if boto3 changes. If a future version stops naming the
    generated class ``S3``, or moves ``meta``, this goes red and somebody rereads the guard,
    which is exactly what should happen.
    """
    import boto3

    return boto3.client("s3", region_name="us-east-1")


def _boto3_resource() -> object:
    """``boto3.resource("s3")``, the other object a person reaches for by that name.

    Different class (``s3.ServiceResource``), no ``head_object`` at all, and just as easy to
    pass here by mistake.
    """
    import boto3

    return boto3.resource("s3", region_name="us-east-1")


def test_the_class_name_that_caused_all_of_this_is_still_what_it_was() -> None:
    """The premise of the guard, asserted rather than asserted-in-a-docstring.

    ``type(boto3.client("s3")).__name__`` being literally ``S3``, the same name as the protocol
    in ``edullm_data.s3``, is the entire reason the 2026-08-01 failure was misread as a typo in
    ``read.py``. Every message below is written around that coincidence, so if it ever stops
    being true the messages become misleading and somebody should be told.
    """
    assert type(_boto3_client()).__name__ == "S3"


def test_a_boto3_client_is_refused_by_name_rather_than_dying_on_first_use() -> None:
    """Mutation: delete the ``looks_like_boto3`` branch and let the generic message serve.

    It does not serve. The generic message says an object is missing ``head()``, which is the
    same sentence the ``AttributeError`` already said and is the sentence that sent the last
    reader looking for a typo in ``read.py``. This branch has to say **boto3** out loud,
    because the one fact a person cannot recover from the traceback is which of the two
    classes called ``S3`` they are holding.
    """
    with pytest.raises(TypeError) as caught:
        R.dataset_paths("pretrain/x", "v1", s3=_boto3_client())

    message = str(caught.value)
    assert NAMES_BOTO3 in message
    assert "Boto3S3.default()" in message, "the message has to name the thing to pass instead"
    assert "dataset_paths()" in message, "and where the wrong object went in"


def test_a_boto3_resource_gets_the_same_message_despite_having_no_head_object() -> None:
    """Mutation: discriminate on ``head_object`` as well as ``meta``.

    **This is the mutation that survived the first version of this file**, and it survived
    because the only non-adapter the file tried was a client. ``boto3.resource("s3")`` has no
    ``head_object``, so the tighter-looking conjunction dropped it into the generic message and
    left somebody holding the wrong object with no idea which one it is. A resource is at least
    as easy to pass here as a client. The check is on ``type(candidate.meta).__module__``
    instead, which both carry.

    Asserted on :data:`NAMES_BOTO3` rather than on the substring ``"boto3"``, and that is a
    second survived mutation behind one line. A resource's ``type(...).__module__`` is
    ``boto3.resources.factory``, so the *generic* message quotes it and contains ``"boto3"``
    too; a test looking only for that word passes whichever message it gets and holds nothing.
    """
    with pytest.raises(TypeError) as caught:
        R.dataset_paths("pretrain/x", "v1", s3=_boto3_resource())

    message = str(caught.value)
    assert NAMES_BOTO3 in message
    assert "Boto3S3.default()" in message


def test_the_refusal_happens_before_anything_is_called_on_the_object() -> None:
    """Mutation: move the guard below ``_require_validated``.

    Then it never runs, because ``_require_validated`` reaches ``s3.head`` first and raises the
    ``AttributeError`` this whole file exists to replace. Asserted with
    ``require_validated=False`` so that nothing else in ``dataset_paths`` could plausibly be
    what refused: with the seal check skipped, a ``TypeError`` here can only be the guard.
    """
    with pytest.raises(TypeError):
        R.dataset_paths("pretrain/x", "v1", s3=_boto3_client(), require_validated=False)


@pytest.mark.parametrize(
    ("call", "name"),
    [
        (lambda bad: R.dataset_paths("pretrain/x", "v1", s3=bad), "dataset_paths()"),
        (lambda bad: R.resolve_latest("pretrain/x", s3=bad), "resolve_latest()"),
        (lambda bad: R.verify_seal("pretrain/x", "v1", s3=bad), "verify_seal()"),
        (
            lambda bad: R.build_mixture("pretrain/x", "v1", sources=[], total=1, seed=1, s3=bad),
            "build_mixture()",
        ),
    ],
)
def test_every_public_reader_guards_its_own_boundary(call, name: str) -> None:
    """Mutation: guard only ``dataset_paths`` and trust the other three.

    All four take an ``s3`` from a caller and all four are the first thing a research
    entrypoint touches, so a guard on one of them is a guard on whichever one that repository
    happened to call. ``build_mixture`` is checked with an empty ``sources``, which normally
    raises ``ReadError`` on the line after: the guard has to come first, or the caller is told
    about their mixture while holding the wrong client.
    """
    with pytest.raises(TypeError) as caught:
        call(_boto3_client())
    assert name in str(caught.value)


def test_a_real_adapter_passes_untouched() -> None:
    """Mutation: tighten the check to the whole protocol with ``isinstance(x, S3)``.

    :class:`FakeS3` would still pass, so this test would stay green and the mutation would look
    safe. It is not: the whole protocol includes ``put``, ``put_file``, ``copy`` and ``delete``,
    which no reader calls, so a read-only adapter would start being refused for failing to
    implement writes it will never be asked to perform. The check is scoped to the four methods
    the readers actually use, and the next test is the one that holds that.
    """
    require_s3_adapter(FakeS3(), called_from="test")


def test_a_read_only_adapter_is_accepted_because_the_readers_never_write() -> None:
    """Mutation: widen ``_READER_METHODS`` to the full protocol.

    Red here. This object can serve every call ``read.py`` makes and cannot write, which is a
    reasonable thing to hand a reader and arguably the safest thing to hand one. Refusing it
    would be the guard inventing a requirement the code does not have, which is the failure
    mode of every check that grows past what it was added for.
    """

    class ReadOnly:
        def get(self, bucket: str, key: str) -> bytes: ...
        def get_range(self, bucket: str, key: str, start: int, length: int) -> bytes: ...
        def head(self, bucket: str, key: str) -> dict: ...
        def list(self, bucket: str, prefix: str) -> list[dict]: ...

    require_s3_adapter(ReadOnly(), called_from="test")


def test_an_object_missing_methods_is_told_which_ones() -> None:
    """Mutation: drop ``missing`` from the generic message.

    Somebody writing their own adapter gets "this is not an S3" and has to diff two files to
    find out what they left out, when the check already knows.
    """

    class HalfAnAdapter:
        def get(self, bucket: str, key: str) -> bytes: ...
        def head(self, bucket: str, key: str) -> dict: ...

    with pytest.raises(TypeError) as caught:
        require_s3_adapter(HalfAnAdapter(), called_from="test")

    message = str(caught.value)
    assert "get_range()" in message
    assert "list()" in message
    assert "get()" not in message.split("missing", 1)[1], "do not list what was provided"


def test_none_is_refused_without_being_blamed_on_boto3() -> None:
    """A default that was never filled in is the other way a wrong ``s3`` arrives.

    Telling somebody who passed ``None`` that they passed a boto3 object sends them to fix code
    that is already correct.
    """
    with pytest.raises(TypeError) as caught:
        require_s3_adapter(None, called_from="test")
    assert NAMES_BOTO3 not in str(caught.value)


def test_an_unrelated_object_with_a_meta_attribute_is_not_called_boto3() -> None:
    """Mutation: widen the boto3 branch to ``hasattr(candidate, "meta")``.

    ``meta`` is a common enough attribute name that presence alone is not evidence of boto3,
    and a message confidently naming the wrong library is worse than the generic one, because
    the generic one at least says what is missing. The module the ``meta`` object's type comes
    from is the part that actually identifies boto3.
    """

    class HasAMetaOfItsOwn:
        meta = {"written_by": "somebody else entirely"}

    with pytest.raises(TypeError) as caught:
        require_s3_adapter(HasAMetaOfItsOwn(), called_from="test")

    message = str(caught.value)
    assert NAMES_BOTO3 not in message
    assert "get()" in message, "it should fall through to the message that says what is missing"

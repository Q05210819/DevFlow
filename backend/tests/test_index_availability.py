"""Pure availability evaluation used by read paths and operational checks."""

from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from devflow.errors import AppError
from devflow.knowledge.indexing import (
    IndexAvailability,
    evaluate_index_availability,
)
from devflow.knowledge.milvus import (
    ANALYZER_VERSION,
    SCHEMA_VERSION,
    IndexFingerprint,
)

FINGERPRINT = IndexFingerprint(
    embedding="bge-m3@revision",
    tokenizer="bge-m3-tokenizer-v1",
    analyzer=ANALYZER_VERSION,
    chunking="chunking-v1",
    schema=SCHEMA_VERSION,
)
GENERATION = 3
SHA = "a" * 40


def corpus(**overrides):
    values = {
        "target_generation": GENERATION,
        "target_sha": SHA,
        "active_index_version_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def version(**overrides):
    values = {
        "id": uuid4(),
        "status": "ready",
        "queryable": True,
        "purged_at": None,
        "target_generation": GENERATION,
        "fingerprint": asdict(FINGERPRINT),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_without_a_target_nothing_is_indexed():
    assert evaluate_index_availability(None, None, None) == IndexAvailability(
        "not_indexed", "no_target"
    )
    assert evaluate_index_availability(
        corpus(target_generation=0), None, None
    ) == IndexAvailability("not_indexed", "no_target")
    assert evaluate_index_availability(
        corpus(target_sha=""), None, None
    ) == IndexAvailability("not_indexed", "no_target")


def test_current_published_version_is_ready():
    active = version()
    assert evaluate_index_availability(corpus(), active, None) == IndexAvailability(
        "ready", None, active.id
    )
    assert evaluate_index_availability(
        corpus(), active, None, fingerprint=FINGERPRINT
    ) == IndexAvailability("ready", None, active.id)


def test_runtime_fingerprint_mismatch_is_stale():
    active = version()
    other = IndexFingerprint(
        embedding="bge-m3@other",
        tokenizer=FINGERPRINT.tokenizer,
        analyzer=ANALYZER_VERSION,
        chunking=FINGERPRINT.chunking,
        schema=SCHEMA_VERSION,
    )
    assert evaluate_index_availability(
        corpus(), active, None, fingerprint=other
    ) == IndexAvailability("stale", "fingerprint_mismatch", active.id)

    malformed = version(fingerprint={"embedding": "incomplete"})
    assert evaluate_index_availability(
        corpus(), malformed, None, fingerprint=FINGERPRINT
    ) == IndexAvailability("stale", "fingerprint_mismatch", malformed.id)


def test_unfinished_target_is_building():
    building = version(status="queued", queryable=False)
    assert evaluate_index_availability(corpus(), None, building) == IndexAvailability(
        "building", "target_building", building.id
    )

    active = version(target_generation=GENERATION - 1)
    assert evaluate_index_availability(corpus(), active, building) == IndexAvailability(
        "building", "target_building", building.id
    )


def test_active_older_target_without_pending_build_is_stale():
    active = version(target_generation=GENERATION - 1)
    assert evaluate_index_availability(corpus(), active, None) == IndexAvailability(
        "stale", "target_pending", active.id
    )


def test_failed_target_reports_an_explicit_reason():
    failed = version(status="failed", queryable=False)
    assert evaluate_index_availability(corpus(), None, failed) == IndexAvailability(
        "stale", "target_failed", failed.id
    )

    # An older active version still addressable by SHA must not hide that the
    # current target build is terminal.
    active = version(target_generation=GENERATION - 1)
    assert evaluate_index_availability(corpus(), active, failed) == IndexAvailability(
        "stale", "target_failed", failed.id
    )


def test_reclaimed_or_unavailable_active_has_an_explicit_reason():
    purged = version(purged_at=object())
    assert evaluate_index_availability(corpus(), purged, None) == IndexAvailability(
        "stale", "vectors_reclaimed", purged.id
    )

    failed = version(status="failed")
    assert evaluate_index_availability(corpus(), failed, None) == IndexAvailability(
        "stale", "active_unavailable", failed.id
    )

    unqueryable = version(queryable=False)
    assert evaluate_index_availability(
        corpus(), unqueryable, None
    ) == IndexAvailability("stale", "active_unavailable", unqueryable.id)


def test_finished_target_without_active_pointer_is_not_indexed():
    done = version(status="ready")
    assert evaluate_index_availability(corpus(), None, done) == IndexAvailability(
        "not_indexed", "target_not_indexed"
    )


def test_require_raises_a_typed_not_ready_error():
    unavailable = IndexAvailability("stale", "vectors_missing")
    with pytest.raises(AppError) as raised:
        unavailable.require()
    assert raised.value.code == "index_not_ready"
    assert raised.value.status == 409
    IndexAvailability("ready").require()

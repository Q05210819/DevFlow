from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError


@pytest.mark.parametrize("key", ["", " ", "two words", "x\n", "\t", "é", "x" * 129, 12])
def test_idempotency_key_rejects_invalid_values(key):
    from devflow.jobs.schemas import IdempotencyKey

    with pytest.raises(ValidationError):
        TypeAdapter(IdempotencyKey).validate_python(key)


@pytest.mark.parametrize("key", ["a", "!~", "x" * 128])
def test_idempotency_key_preserves_visible_ascii(key):
    from devflow.jobs.schemas import IdempotencyKey

    assert TypeAdapter(IdempotencyKey).validate_python(key) == key


def test_event_payload_rejects_arbitrary_errors_and_metadata():
    from devflow.jobs.schemas import EventPayload

    payload = EventPayload(status="queued", attempt=0)
    assert payload.model_dump() == {
        "status": "queued",
        "attempt": 0,
        "error_code": None,
    }
    for extra in [
        {"token": "secret"},
        {"error_code": "raw provider secret"},
        {"status": "unknown"},
    ]:
        with pytest.raises(ValidationError):
            EventPayload.model_validate({**payload.model_dump(), **extra})


def test_public_job_serialization_contains_only_frozen_contract():
    from devflow.jobs.schemas import JobView

    now = datetime(2026, 1, 1, tzinfo=UTC)
    view = JobView(
        id=uuid4(),
        repository_id=uuid4(),
        requested_by=uuid4(),
        status="queued",
        attempt=0,
        max_attempts=3,
        result=None,
        error_code=None,
        last_sequence=1,
        created_at=now,
        updated_at=now,
    )
    assert set(view.model_dump()) == {
        "id",
        "repository_id",
        "requested_by",
        "status",
        "attempt",
        "max_attempts",
        "result",
        "error_code",
        "last_sequence",
        "created_at",
        "updated_at",
    }


def test_attempt_configuration_cannot_exceed_three(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(job_max_attempts=4)

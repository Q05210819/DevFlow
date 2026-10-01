from uuid import uuid4

import pytest


@pytest.mark.parametrize(
    "source,target", [("users", "audit"), ("repository-discovery", "repository-list")]
)
def test_cursor_rejects_other_actor_or_purpose(source, target):
    from devflow.errors import AppError
    from devflow.pagination import CursorCodec

    codec = CursorCodec(b"a" * 32)
    actor = uuid4()
    position = {"created_at": "2026-10-01T00:00:00+00:00", "id": str(uuid4())}
    token = codec.encode_cursor(actor, source, position, 20)
    assert codec.decode_cursor(token, actor, source, 20) == position
    for candidate, owner, purpose, limit in [
        (token, uuid4(), source, 20),
        (token, actor, target, 20),
        (token, actor, source, 100),
        ("A" + token[1:], actor, source, 20),
        ("invalid", actor, source, 20),
    ]:
        with pytest.raises(AppError) as error:
            codec.decode_cursor(candidate, owner, purpose, limit)
        assert error.value.status == 400
    with pytest.raises(AppError):
        CursorCodec(b"b" * 32).decode_cursor(token, actor, source, 20)


@pytest.mark.parametrize("limit", [0, 101, True])
def test_cursor_rejects_invalid_limit(limit):
    from devflow.errors import AppError
    from devflow.pagination import CursorCodec

    with pytest.raises(AppError):
        CursorCodec(b"a" * 32).encode_cursor(uuid4(), "users", {}, limit)

import gzip
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from devflow.errors import AppError
from devflow.github.client import GitHubClient

pytestmark = pytest.mark.asyncio
TOKEN = SecretStr("installation-sentinel")


class LargeStream(httpx.AsyncByteStream):
    def __init__(self):
        self.chunks_read = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(20):
            self.chunks_read += 1
            yield b" " * (1024 * 1024)

    async def aclose(self):
        self.closed = True


async def test_stream_cap_applies_without_content_length(settings_factory):
    from devflow.github.data import GitHubDataClient

    stream = LargeStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubDataClient(GitHubClient(http, settings_factory())).read(
                TOKEN, "team/private", "issues"
            )
    assert caught.value.code == "github_response_too_large"
    assert "sentinel" not in str(caught.value)
    assert stream.chunks_read == 17
    assert stream.closed


@pytest.mark.parametrize(
    "link",
    [
        '<https://attacker.invalid/repos/team/private/issues?page=2>; rel="next"',
        '<https://api.github.com/repos/team/private/pulls?page=2>; rel="next"',
        '<https://api.github.com/repos/team/private/issues?page=1>; rel="next"',
        '<https://api.github.com/repos/team/private/issues?page=2&page=3>; rel="next"',
        '<https://api.github.com/repos/team/private/issues?page=0>; rel="next"',
        '<https://api.github.com/repos/team/private/issues>; rel="next"',
        '<https://user:secret@api.github.com/repos/team/private/issues?page=2>; rel="next"',
        '<https://api.github.com/repos/team/private/issues?page=2#fragment>; rel="next"',
        (
            '<https://api.github.com/repos/team/private/issues?page=2>; rel="next", '
            '<https://api.github.com/repos/team/private/issues?page=3>; rel="next"'
        ),
        'not-a-url; rel="next"',
        '<https://api.github.com/repos/team/private/issues?page=2>; rel="prev next"',
        '<https://api.github.com/repos/team/private/issues?page=2>; rel="next',
    ],
)
async def test_malformed_next_link_is_not_completion(settings_factory, link):
    from devflow.github.data import GitHubDataClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[], headers={"link": link})
        )
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubDataClient(GitHubClient(http, settings_factory())).read(
                TOKEN, "team/private", "issues"
            )
    assert caught.value.code == "github_unavailable"


@pytest.mark.parametrize("relation", ["NEXT", "Next"])
async def test_registered_next_relation_is_case_insensitive(settings_factory, relation):
    from devflow.github.data import GitHubDataClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json=[],
                headers={
                    "link": f'<https://api.github.com/repos/team/private/issues?page=2>; rel="{relation}"'
                },
            )
        )
    ) as http:
        result = await GitHubDataClient(GitHubClient(http, settings_factory())).read(
            TOKEN, "team/private", "issues"
        )
    assert result.next_page == 2


@pytest.mark.parametrize(
    "link",
    [
        '<https://attacker.invalid/repos/team/private/issues?page=2>; rel="Next"',
        '<https://api.github.com/repos/team/private/issues?page=2>; rel="next", <https://api.github.com/repos/team/private/issues?page=3>; rel="NEXT"',
        '<https://api.github.com/repos/team/private/issues?page=2>; rel="PreV NeXt"',
    ],
)
async def test_next_relation_case_does_not_bypass_validation(settings_factory, link):
    from devflow.github.data import GitHubDataClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[], headers={"link": link})
        )
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubDataClient(GitHubClient(http, settings_factory())).read(
                TOKEN, "team/private", "issues"
            )
    assert caught.value.code == "github_unavailable"


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({}, "permission_missing"),
        (
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4102444800"},
            "github_rate_limited",
        ),
    ],
)
async def test_error_body_recursion_preserves_header_classification(
    settings_factory, headers, code
):
    from devflow.github.data import GitHubDataClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                403, headers=headers, content=b"[" * 50000 + b"]" * 50000
            )
        )
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubDataClient(GitHubClient(http, settings_factory())).read(
                TOKEN, "team/private", "issues"
            )
    assert caught.value.code == code
    if code == "github_rate_limited":
        assert caught.value.retry_at == datetime(2100, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("endpoint", "target", "attempt", "path", "payload", "items"),
    [
        ("issues", None, None, "/issues", [{"number": 1}], [{"number": 1}]),
        ("issue", 7, None, "/issues/7", {"number": 7}, None),
        ("issue_comments", 7, None, "/issues/7/comments", [{"id": 8}], [{"id": 8}]),
        ("pulls", None, None, "/pulls", [{"number": 7}], [{"number": 7}]),
        ("pull", 7, None, "/pulls/7", {"number": 7}, None),
        ("pull_files", 7, None, "/pulls/7/files", [{"sha": "abc"}], [{"sha": "abc"}]),
        ("reviews", 7, None, "/pulls/7/reviews", [{"id": 8}], [{"id": 8}]),
        ("review_comments", 7, None, "/pulls/7/comments", [{"id": 8}], [{"id": 8}]),
        (
            "runs",
            None,
            None,
            "/actions/runs",
            {"workflow_runs": [{"id": 9}], "total_count": 3},
            [{"id": 9}],
        ),
        ("run", 9, None, "/actions/runs/9", {"id": 9}, None),
        ("attempt", 9, 2, "/actions/runs/9/attempts/2", {"id": 9}, None),
        (
            "jobs",
            9,
            2,
            "/actions/runs/9/attempts/2/jobs",
            {"jobs": [{"id": 10}], "total_count": 1},
            [{"id": 10}],
        ),
        (
            "jobs",
            9,
            None,
            "/actions/runs/9/jobs",
            {"jobs": [{"id": 10}], "total_count": 1},
            [{"id": 10}],
        ),
        ("repository", None, None, "", {"id": 81, "full_name": "team/private"}, None),
    ],
)
async def test_fixed_endpoints_and_response_shapes(
    settings_factory, endpoint, target, attempt, path, payload, items
):
    from devflow.github.data import GitHubDataClient

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await GitHubDataClient(GitHubClient(http, settings_factory())).read(
            TOKEN, "team/private", endpoint, target=target, attempt=attempt
        )
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/repos/team/private" + path
    assert requests[0].headers["authorization"] == "Bearer installation-sentinel"
    assert result.items == items
    assert result.item == (payload if items is None else None)
    assert result.total_count == (
        payload.get("total_count")
        if isinstance(payload, dict) and items is not None
        else None
    )
    assert result.next_page is None
    if items is not None:
        assert dict(requests[0].url.params) == {"page": "1", "per_page": "100"}


async def test_valid_next_page_and_filter_allowlist(settings_factory):
    from devflow.github.data import GitHubDataClient

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=[{"number": 1}],
            headers={
                "link": '<https://api.github.com/repos/team/private/issues?state=all&page=3&per_page=100>; rel="next", <https://api.github.com/repos/team/private/issues?page=10>; rel="last"'
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await GitHubDataClient(GitHubClient(http, settings_factory())).read(
            TOKEN,
            "team/private",
            "issues",
            page=2,
            filters={
                "state": "all",
                "since": "2026-10-01T00:00:00Z",
                "sort": "updated",
                "direction": "asc",
            },
        )
    assert result.next_page == 3
    assert dict(requests[0].url.params) == {
        "page": "2",
        "per_page": "100",
        "state": "all",
        "since": "2026-10-01T00:00:00Z",
        "sort": "updated",
        "direction": "asc",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"full_name": "team/private/extra"},
        {"full_name": "../private"},
        {"full_name": "team/%2e%2e"},
        {"full_name": "team/private?x=y"},
        {"full_name": "https://attacker.invalid"},
        {"full_name": "team/.."},
        {"endpoint": "https://attacker.invalid"},
        {"endpoint": "issue", "target": True},
        {"endpoint": "issue", "target": 0},
        {"endpoint": "issue"},
        {"page": 0},
        {"page": True},
        {"target": 7},
        {"attempt": 2},
        {"filters": {"per_page": 1000}},
        {"filters": {"url": "https://attacker.invalid"}},
        {"filters": {"state": "invalid"}},
        {"filters": {"since": ["one", "two"]}},
        {"endpoint": "repository", "filters": {"state": "all"}},
    ],
)
async def test_invalid_inputs_do_not_make_http_requests(settings_factory, changes):
    from devflow.github.data import GitHubDataClient

    requests = []
    args = {"full_name": "team/private", "endpoint": "issues", **changes}

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(AppError) as caught:
            await GitHubDataClient(GitHubClient(http, settings_factory())).read(
                TOKEN, **args
            )
    assert caught.value.code == "validation_error"
    assert requests == []


@pytest.mark.parametrize(
    ("status", "headers", "body", "code"),
    [
        (401, {}, {}, "github_installation_unavailable"),
        (403, {}, {"message": "Resource not accessible"}, "permission_missing"),
        (404, {}, {}, "resource_not_found"),
        (
            403,
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4102444800"},
            {},
            "github_rate_limited",
        ),
        (302, {"location": "https://attacker.invalid"}, {}, "github_unavailable"),
    ],
)
async def test_installation_access_errors_never_request_user_login(
    settings_factory, status, headers, body, code
):
    from devflow.github.data import GitHubDataClient

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubDataClient(GitHubClient(http, settings_factory())).read(
                TOKEN, "team/private", "issues"
            )
    assert caught.value.code == code
    assert len(requests) == 1
    if code == "github_rate_limited":
        assert caught.value.retry_at == datetime(2100, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        ("issues", {}),
        ("issues", [1]),
        ("issue", []),
        ("runs", {"workflow_runs": [], "total_count": True}),
        ("jobs", {"jobs": [], "total_count": -1}),
        ("runs", {"workflow_runs": []}),
    ],
)
async def test_malformed_payload_is_safe(settings_factory, endpoint, payload):
    from devflow.github.data import GitHubDataClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubDataClient(GitHubClient(http, settings_factory())).read(
                TOKEN,
                "team/private",
                endpoint,
                target=7 if endpoint in {"issue", "jobs"} else None,
            )
    assert caught.value.code == "github_unavailable"


@pytest.mark.parametrize("size", [16 * 1024 * 1024, 16 * 1024 * 1024 + 1])
async def test_decoded_size_limit_and_compressed_json(settings_factory, size):
    from devflow.github.data import GitHubDataClient

    compressed = gzip.compress(b"[]" + b" " * (size - 2))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={
                    "content-encoding": "gzip",
                    "content-length": str(len(compressed)),
                },
                stream=httpx.ByteStream(compressed),
            )
        )
    ) as http:
        data = GitHubDataClient(GitHubClient(http, settings_factory()))
        if size == 16 * 1024 * 1024:
            assert (await data.read(TOKEN, "team/private", "issues")).items == []
        else:
            with pytest.raises(AppError) as caught:
                await data.read(TOKEN, "team/private", "issues")
            assert caught.value.code == "github_response_too_large"


async def test_json_recursion_failure_is_controlled(settings_factory):
    from devflow.github.data import GitHubDataClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"[" * 50000 + b"]" * 50000)
        )
    ) as http:
        with pytest.raises(AppError) as caught:
            await GitHubDataClient(GitHubClient(http, settings_factory())).read(
                TOKEN, "team/private", "issues"
            )
    assert caught.value.code == "github_unavailable"


async def test_response_page_cannot_exceed_requested_size(settings_factory):
    from devflow.github.data import GitHubDataClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[{}] * 101))
    ) as http:
        with pytest.raises(AppError) as error:
            await GitHubDataClient(GitHubClient(http, settings_factory())).read(
                TOKEN, "team/private", "issues"
            )
    assert error.value.code == "github_unavailable"

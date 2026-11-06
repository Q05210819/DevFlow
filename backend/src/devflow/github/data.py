"""One bounded read of a fixed GitHub repository data endpoint."""

import re
from enum import StrEnum
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from devflow.errors import AppError
from devflow.github.client import REST_URL, GitHubClient, _positive_number, unavailable


class DataEndpoint(StrEnum):
    ISSUES = "issues"
    ISSUE = "issue"
    ISSUE_COMMENTS = "issue_comments"
    PULLS = "pulls"
    PULL = "pull"
    PULL_FILES = "pull_files"
    REVIEWS = "reviews"
    REVIEW_COMMENTS = "review_comments"
    RUNS = "runs"
    RUN = "run"
    ATTEMPT = "attempt"
    JOBS = "jobs"
    REPOSITORY = "repository"
    REF = "ref"
    TAG = "tag"
    COMMIT = "commit"


class DataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    items: list[dict] | None = None
    item: dict | None = None
    next_page: int | None = None
    total_count: int | None = None


# Path template, list response kind (None means detail), allowed query keys.
_ENDPOINTS = {
    DataEndpoint.ISSUES: ("/issues", "list", {"state", "since", "sort", "direction"}),
    DataEndpoint.ISSUE: ("/issues/{target}", None, set()),
    DataEndpoint.ISSUE_COMMENTS: ("/issues/{target}/comments", "list", {"since"}),
    DataEndpoint.PULLS: (
        "/pulls",
        "list",
        {"state", "sort", "direction", "base", "head"},
    ),
    DataEndpoint.PULL: ("/pulls/{target}", None, set()),
    DataEndpoint.PULL_FILES: ("/pulls/{target}/files", "list", set()),
    DataEndpoint.REVIEWS: ("/pulls/{target}/reviews", "list", set()),
    DataEndpoint.REVIEW_COMMENTS: ("/pulls/{target}/comments", "list", {"since"}),
    DataEndpoint.RUNS: (
        "/actions/runs",
        "workflow_runs",
        {"created", "status", "branch", "event", "head_sha"},
    ),
    DataEndpoint.RUN: ("/actions/runs/{target}", None, set()),
    DataEndpoint.ATTEMPT: ("/actions/runs/{target}/attempts/{attempt}", None, set()),
    DataEndpoint.JOBS: ("/actions/runs/{target}/jobs", "jobs", {"filter"}),
    DataEndpoint.REPOSITORY: ("", None, set()),
    DataEndpoint.REF: ("/git/ref/{ref}", None, set()),
    DataEndpoint.TAG: ("/git/tags/{oid}", None, set()),
    DataEndpoint.COMMIT: ("/git/commits/{oid}", None, set()),
}


def _invalid() -> AppError:
    return AppError("validation_error", 400, "Invalid GitHub data request")


def _next_page(response: httpx.Response, page: int) -> int | None:
    """Fail closed on an advertised next link; never mistake it for completion."""
    next_links = []
    for header in response.headers.get_list("link"):
        for part in re.split(r",\s*(?=<)", header):
            match = re.fullmatch(
                r'\s*<([^<>]+)>\s*;\s*rel\s*=\s*(?:"([^"]+)"|([A-Za-z]+))\s*',
                part,
                re.IGNORECASE,
            )
            if match is None:
                raise unavailable()
            relation = (match[2] or match[3]).lower().split()
            if "next" not in relation:
                continue
            if relation != ["next"]:
                raise unavailable()
            next_links.append(match[1])
    if not next_links:
        return None
    if len(next_links) != 1:
        raise unavailable()
    try:
        url = urlsplit(next_links[0])
        if (
            url.scheme != "https"
            or url.hostname != "api.github.com"
            or url.port not in (None, 443)
            or url.username is not None
            or url.password is not None
            or url.fragment
            or url.path != response.request.url.path
        ):
            raise ValueError
        values = parse_qs(url.query, keep_blank_values=True).get("page", [])
        if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
            raise ValueError
        value = int(values[0])
        if value <= page:
            raise ValueError
        return value
    except ValueError:
        raise unavailable() from None


class GitHubDataClient:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def read(
        self,
        token: SecretStr,
        full_name: str,
        endpoint: DataEndpoint,
        *,
        target: int | None = None,
        attempt: int | None = None,
        page: int = 1,
        filters: dict | None = None,
        ref: str | None = None,
        oid: str | None = None,
        installation: bool = True,
    ) -> DataResponse:
        if (
            not isinstance(full_name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", full_name)
            is None
            or full_name.split("/")[1] in {".", ".."}
        ):
            raise _invalid()
        try:
            endpoint = DataEndpoint(endpoint)
        except (ValueError, TypeError):
            raise _invalid() from None
        path, kind, allowed_filters = _ENDPOINTS[endpoint]
        if "{ref}" in path:
            from devflow.workspace.git import valid_ref

            if (
                not isinstance(ref, str)
                or not ref.startswith(("heads/", "tags/"))
                or not valid_ref("refs/" + ref)
            ):
                raise _invalid()
        elif ref is not None:
            raise _invalid()
        if "{oid}" in path:
            if not isinstance(oid, str) or re.fullmatch(r"[0-9a-f]{40}", oid) is None:
                raise _invalid()
        elif oid is not None:
            raise _invalid()
        if type(installation) is not bool:
            raise _invalid()
        _positive_number(page)
        if "{target}" in path:
            _positive_number(target)
        elif target is not None:
            raise _invalid()
        if endpoint == DataEndpoint.JOBS and attempt is not None:
            path = "/actions/runs/{target}/attempts/{attempt}/jobs"
            allowed_filters = set()
        if "{attempt}" in path:
            _positive_number(attempt)
        elif attempt is not None:
            raise _invalid()
        if filters is None:
            filters = {}
        if not isinstance(filters, dict) or any(
            key not in allowed_filters or not isinstance(value, str) or not value
            for key, value in filters.items()
        ):
            raise _invalid()
        enums = {
            "state": {"open", "closed", "all"},
            "direction": {"asc", "desc"},
            "filter": {"latest", "all"},
            "sort": {"created", "updated", "comments"}
            if endpoint == DataEndpoint.ISSUES
            else {"created", "updated", "popularity", "long-running"},
        }
        if any(
            key in enums and value not in enums[key] for key, value in filters.items()
        ):
            raise _invalid()
        params = (
            {"page": page, "per_page": 100, **filters} if kind is not None else None
        )
        response = await self._client._request(
            "GET",
            f"{REST_URL}/repos/{full_name}{path.format(target=target, attempt=attempt, ref=quote(ref or '', safe='/'), oid=oid)}",
            token=token,
            params=params,
            installation=installation,
        )
        try:
            payload = response.json()
        except (ValueError, UnicodeError, RecursionError):
            raise unavailable() from None
        if kind is None:
            if not isinstance(payload, dict):
                raise unavailable()
            return DataResponse(item=payload)
        total_count = None
        if kind == "list":
            items = payload
        else:
            if not isinstance(payload, dict):
                raise unavailable()
            items = payload.get(kind)
            total_count = payload.get("total_count")
            if type(total_count) is not int or total_count < 0:
                raise unavailable()
        if (
            not isinstance(items, list)
            or len(items) > 100
            or any(not isinstance(item, dict) for item in items)
        ):
            raise unavailable()
        return DataResponse(
            items=items, next_page=_next_page(response, page), total_count=total_count
        )

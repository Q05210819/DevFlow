"""Finite provider facts; a commit hash alone never creates a workspace source."""

from pydantic import TypeAdapter, ValidationError

from devflow.errors import AppError
from devflow.facts.schemas import SHA, GitHubID
from devflow.workspace.git import valid_ref
from devflow.workspace.schemas import WorkspaceSource

_SHA = TypeAdapter(SHA)
_ID = TypeAdapter(GitHubID)


def unavailable():
    return AppError("version_unavailable", 409, "Selected code version is unavailable")


async def resolve(read, scope_for, base, selection):
    """read/scope_for are private credential-aware closures, never request fields."""
    try:
        return await _resolve(read, scope_for, base, selection)
    except (KeyError, TypeError, ValueError, ValidationError):
        raise unavailable() from None
    except AppError as error:
        if error.code == "resource_not_found":
            raise unavailable() from None
        raise


async def _ref(read, base, ref):
    if not valid_ref(ref):
        raise unavailable()
    item = await read(base, "ref", ref=ref.removeprefix("refs/"))
    if item["ref"] != ref:
        raise unavailable()
    obj = item["object"]
    seen = set()
    for depth in range(9):
        sha = _SHA.validate_python(obj["sha"])
        if obj["type"] == "commit":
            return sha
        if (
            not ref.startswith("refs/tags/")
            or obj["type"] != "tag"
            or depth == 8
            or sha in seen
        ):
            raise unavailable()
        seen.add(sha)
        tag = await read(base, "tag", oid=sha)
        if tag["sha"] != sha:
            raise unavailable()
        obj = tag["object"]
    raise unavailable()


async def _pull(read, base, number):
    item = await read(base, "pull", target=number)
    if (
        _ID.validate_python(item["number"]) != number
        or _ID.validate_python(item["base"]["repo"]["id"])
        != base["github_repository_id"]
    ):
        raise unavailable()
    _SHA.validate_python(item["base"]["sha"])
    _SHA.validate_python(item["head"]["sha"])
    return item


async def _resolve(read, scope_for, base, selection):
    base_id = base["github_repository_id"]
    source_id = base_id
    base_sha = head_sha = None
    if selection.kind in {"branch", "tag"}:
        ref = (
            f"refs/{'heads' if selection.kind == 'branch' else 'tags'}/{selection.name}"
        )
        sha = await _ref(read, base, ref)
    elif selection.kind == "pull":
        pull = await _pull(read, base, selection.number)
        base_sha, head_sha = pull["base"]["sha"], pull["head"]["sha"]
        if selection.side == "base":
            sha, ref = base_sha, "refs/heads/" + pull["base"]["ref"]
        else:
            head_repository = pull["head"]["repo"]
            if head_repository is None:
                raise AppError(
                    "fork_source_unavailable", 404, "Fork code source unavailable"
                )
            source_id = _ID.validate_python(head_repository["id"])
            sha, ref = head_sha, f"refs/pull/{selection.number}/head"
    else:
        run = await read(
            base, "attempt", target=selection.run_id, attempt=selection.attempt
        )
        if (
            _ID.validate_python(run["id"]) != selection.run_id
            or _ID.validate_python(run["run_attempt"]) != selection.attempt
            or _ID.validate_python(run["repository"]["id"]) != base_id
        ):
            raise unavailable()
        sha = _SHA.validate_python(run["head_sha"])
        source_id = _ID.validate_python(run["head_repository"]["id"])
        pulls = run["pull_requests"]
        if not isinstance(pulls, list):
            raise unavailable()
        if pulls:
            if len(pulls) != 1:
                raise unavailable()
            number = _ID.validate_python(pulls[0]["number"])
            pull = await _pull(read, base, number)
            if (
                pull["head"]["sha"] != sha
                or _ID.validate_python(pull["head"]["repo"]["id"]) != source_id
            ):
                raise unavailable()
            base_sha, head_sha = pull["base"]["sha"], sha
            ref = f"refs/pull/{number}/head"
        else:
            if source_id != base_id or not isinstance(run.get("head_branch"), str):
                raise unavailable()
            ref = None
            for namespace in ("heads", "tags"):
                candidate = f"refs/{namespace}/{run['head_branch']}"
                try:
                    candidate_sha = await _ref(read, base, candidate)
                except AppError as error:
                    if error.code not in {"resource_not_found", "version_unavailable"}:
                        raise
                    continue
                if candidate_sha == sha:
                    ref = candidate
                    break
            if ref is None:
                raise unavailable()
    if not valid_ref(ref):
        raise unavailable()
    source = await scope_for(source_id)
    tree = None
    for scope in [base] if source_id == base_id else [base, source]:
        commit = await read(scope, "commit", oid=sha)
        if _SHA.validate_python(commit["sha"]) != sha:
            raise unavailable()
        current_tree = _SHA.validate_python(commit["tree"]["sha"])
        if tree is not None and tree != current_tree:
            raise unavailable()
        tree = current_tree
    proof = WorkspaceSource(
        selection=selection,
        ref=ref,
        source_repository_id=source_id,
        transfer_repository_id=base_id,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    return sha, tree, proof, source

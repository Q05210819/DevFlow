"""Convert upstream objects to a bounded whitelist; context owns relationships."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from devflow.facts.schemas import (
    FactInput,
    ParentKind,
    pr_file_version_key,
    validate_fact_payload,
)


@dataclass(frozen=True, slots=True)
class FactContext:
    repository_id: UUID
    observed_at: datetime
    full_name: str
    parent_kind: ParentKind | None = None
    parent_source_id: int | None = None
    parent_number: int | None = None
    issue_id: int | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    attempt: int | None = None


def normalize_fact(kind: str, payload: dict, context: FactContext) -> FactInput:
    p, c = payload, context
    reasons = set()

    def text(value, limit=65536):
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("Invalid source text")
        if len(value) > limit:
            reasons.add("content_truncated")
        return value[:limit]

    def user(value):
        if value is None:
            reasons.add("field_unavailable")
            return None
        return {"id": value["id"], "login": text(value.get("login"), 256)}

    def bounded(values, limit=100):
        if not isinstance(values, list):
            raise TypeError("Invalid source list")
        if len(values) > limit:
            reasons.add("content_truncated")
        return values[:limit]

    data = {}
    if kind in {"issue", "pull"}:
        data = {
            "title": text(p["title"], 4096),
            "body": text(p.get("body")),
            "state": p["state"],
            "labels": [
                text(v["name"] if isinstance(v, dict) else v, 256)
                for v in bounded(p.get("labels", []))
            ],
            "assignees": [user(v) for v in bounded(p.get("assignees", []))],
            "author": user(p.get("user")),
            "created_at": p.get("created_at"),
            "closed_at": p.get("closed_at"),
        }
        if kind == "pull":
            data.update(
                issue_id=c.issue_id,
                base_sha=c.base_sha or p["base"]["sha"],
                head_sha=c.head_sha or p["head"]["sha"],
                base_repository_id=(p["base"].get("repo") or {}).get("id"),
                head_repository_id=(p["head"].get("repo") or {}).get("id"),
                base_ref=text(p["base"].get("ref"), 4096),
                head_ref=text(p["head"].get("ref"), 4096),
                draft=p["draft"],
                merged=p["merged"],
                merge_commit_sha=p.get("merge_commit_sha"),
                merged_at=p.get("merged_at"),
            )
    elif kind in {"comment", "review", "review_comment"}:
        data = {
            "body": text(p.get("body")),
            "author": user(p.get("user")),
            "created_at": p.get("created_at"),
        }
        if kind == "review":
            data.update(
                state=p["state"],
                commit_id=p.get("commit_id"),
                submitted_at=p.get("submitted_at"),
            )
        elif kind == "review_comment":
            data.update(
                {
                    k: p.get(k)
                    for k in (
                        "commit_id",
                        "original_commit_id",
                        "side",
                        "line",
                        "original_line",
                        "start_line",
                        "original_start_line",
                        "in_reply_to_id",
                    )
                }
            )
            data.update(
                path=text(p.get("path"), 4096),
                review_id=p.get("pull_request_review_id"),
            )
    elif kind == "pr_file":
        patch = text(p.get("patch"))
        if patch is None:
            reasons.add("field_unavailable")
        data = {
            k: p.get(k)
            for k in (
                "filename",
                "previous_filename",
                "sha",
                "status",
                "additions",
                "deletions",
                "changes",
            )
        }
        data.update(
            base_sha=c.base_sha,
            head_sha=c.head_sha,
            patch=patch,
            patch_status="missing"
            if patch is None
            else "truncated"
            if patch != p["patch"]
            else "available",
        )
    elif kind in {"ci_run", "ci_job"}:
        data = {
            "status": p["status"],
            "conclusion": p.get("conclusion"),
            "attempt": c.attempt,
            "head_sha": c.head_sha,
        }
        if kind == "ci_run":
            data.update(
                {
                    k: p.get(k)
                    for k in (
                        "workflow_id",
                        "created_at",
                        "updated_at",
                        "run_started_at",
                    )
                }
            )
            data.update(
                event=text(p["event"], 256),
                workflow_path=text(p.get("path"), 4096),
                name=text(p.get("name"), 4096),
                branch=text(p.get("head_branch"), 4096),
            )
        else:
            data.update(
                run_id=c.parent_source_id,
                name=text(p["name"], 4096),
                started_at=p.get("started_at"),
                completed_at=p.get("completed_at"),
                steps=[
                    {
                        **{
                            k: s.get(k)
                            for k in (
                                "number",
                                "status",
                                "conclusion",
                                "started_at",
                                "completed_at",
                            )
                        },
                        "name": text(s["name"], 4096),
                    }
                    for s in bounded(p.get("steps", []))
                ],
            )
    else:
        raise ValueError("Unsupported fact kind")
    normalized = validate_fact_payload(kind, data)
    if any(
        v == "unknown"
        for k, v in normalized.model_dump().items()
        if k in {"state", "status", "conclusion", "side"}
    ):
        reasons.add("unknown_value")
    if kind == "ci_job" and any(
        s.status == "unknown" or s.conclusion == "unknown" for s in normalized.steps
    ):
        reasons.add("unknown_value")
    data["normalization_reasons"] = sorted(reasons)
    version = (
        pr_file_version_key(
            base_sha=c.base_sha, head_sha=c.head_sha, filename=data["filename"]
        )
        if kind == "pr_file"
        else str(c.attempt)
        if kind == "ci_run"
        else ""
    )
    source_id = c.parent_source_id if kind == "pr_file" else p["id"]
    if kind == "issue":
        path = f"issues/{p['number']}"
    elif kind == "pull":
        path = f"pull/{p['number']}"
    elif kind == "ci_run":
        path = f"actions/runs/{p['id']}/attempts/{c.attempt}"
    elif kind == "ci_job":
        path = f"actions/runs/{c.parent_source_id}/job/{p['id']}"
    elif kind == "pr_file":
        path = f"pull/{c.parent_number}/files"
    else:
        parent = "pull" if c.parent_kind == "pull" else "issues"
        anchor = {
            "comment": "issuecomment",
            "review": "pullrequestreview",
            "review_comment": "discussion_r",
        }[kind]
        path = f"{parent}/{c.parent_number}#{anchor}{'' if kind == 'review_comment' else '-'}{p['id']}"
    source_url = p.get("html_url") or f"https://github.com/{c.full_name}/{path}"
    if (
        c.parent_kind in {"issue", "pull"}
        and c.parent_number is None
        and not p.get("html_url")
    ):
        reasons.add("field_unavailable")
        source_url = f"https://github.com/{c.full_name}"
    data["normalization_reasons"] = sorted(reasons)
    return FactInput(
        repository_id=c.repository_id,
        kind=kind,
        source_id=source_id,
        version_key=version,
        number=p.get("number") if kind in {"issue", "pull"} else None,
        parent_kind=c.parent_kind,
        parent_source_id=c.parent_source_id,
        state=getattr(normalized, "state", getattr(normalized, "status", None)),
        source_updated_at=p.get("updated_at"),
        observed_at=c.observed_at,
        source_url=source_url,
        payload=data,
    )

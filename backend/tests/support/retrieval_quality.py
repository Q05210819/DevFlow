"""Fixed-set retrieval metrics with immutable, source-level gold spans.

Gold never contains chunk identifiers produced by the implementation. A match
is decided only by repository, source SHA, path and the original inclusive line
interval. Overlapping chunks that cover an already matched gold span add no
recall or nDCG score, so repeated windows cannot inflate a result.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

K = 6
REQUIRED_GROUPS = (
    "zh",
    "en",
    "mixed",
    "code",
    "path",
    "no_answer",
    "old_sha",
    "isolated",
)
MINIMUM_QUESTIONS = 30
GROUP_MINIMUMS = {
    "zh": 5,
    "en": 5,
    "mixed": 4,
    "code": 4,
    "path": 4,
    "no_answer": 4,
    "old_sha": 4,
    "isolated": 3,
}
SHA = re.compile(r"^[0-9a-f]{40}$")
PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$")
ANSWER_FIELDS = frozenset({"repository", "sha", "path", "start_line", "end_line"})


@dataclass(frozen=True, slots=True)
class GoldSpan:
    """One immutable answer interval in an original source document."""

    repository: str
    sha: str
    path: str
    start_line: int
    end_line: int

    def __post_init__(self):
        if not self.repository or not SHA.fullmatch(self.sha):
            raise ValueError("Gold span requires a repository and full SHA")
        if not PATH.fullmatch(self.path):
            raise ValueError("Gold span path is not a safe repository path")
        if not 1 <= self.start_line <= self.end_line:
            raise ValueError("Gold span lines must be ordered and positive")


@dataclass(frozen=True, slots=True)
class RetrievalMatch:
    """One returned evidence item projected onto its original source."""

    repository: str
    sha: str | None
    path: str | None
    start_line: int
    end_line: int
    text: str = ""

    def overlaps(self, gold: GoldSpan) -> bool:
        return (
            self.repository == gold.repository
            and (self.sha or "") == gold.sha
            and (self.path or "") == gold.path
            and self.start_line <= gold.end_line
            and gold.start_line <= self.end_line
        )


@dataclass(frozen=True, slots=True)
class ScoredQuestion:
    id: str
    group: str
    answerable: bool
    spans: int
    matched_spans: int
    recall: float | None
    ndcg: float | None
    returned: int
    false_hit: bool
    abstained: bool
    leaked: bool


@dataclass(frozen=True, slots=True)
class GroupReport:
    questions: int
    answerable: int
    macro_recall: float | None
    macro_ndcg: float | None
    false_hits: int
    abstentions: int
    leaks: int


@dataclass(frozen=True, slots=True)
class QualityReport:
    k: int
    questions: tuple[ScoredQuestion, ...]
    groups: Mapping[str, GroupReport]

    @property
    def answerable(self) -> int:
        return sum(1 for item in self.questions if item.answerable)

    @property
    def no_answer(self) -> int:
        return sum(1 for item in self.questions if not item.answerable)

    @property
    def macro_recall_at_6(self) -> float | None:
        values = [item.recall for item in self.questions if item.recall is not None]
        return sum(values) / len(values) if values else None

    @property
    def macro_ndcg_at_6(self) -> float | None:
        values = [item.ndcg for item in self.questions if item.ndcg is not None]
        return sum(values) / len(values) if values else None

    @property
    def false_hits(self) -> int:
        return sum(1 for item in self.questions if item.false_hit)

    @property
    def abstentions(self) -> int:
        return sum(1 for item in self.questions if item.abstained)

    @property
    def leaks(self) -> int:
        return sum(1 for item in self.questions if item.leaked)

    def as_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "answerable": self.answerable,
            "no_answer": self.no_answer,
            "macro_recall_at_6": self.macro_recall_at_6,
            "macro_ndcg_at_6": self.macro_ndcg_at_6,
            "false_hits": self.false_hits,
            "abstentions": self.abstentions,
            "leaks": self.leaks,
            "groups": {
                name: {
                    "questions": group.questions,
                    "answerable": group.answerable,
                    "macro_recall_at_6": group.macro_recall,
                    "macro_ndcg_at_6": group.macro_ndcg,
                    "false_hits": group.false_hits,
                    "abstentions": group.abstentions,
                    "leaks": group.leaks,
                }
                for name, group in sorted(self.groups.items())
            },
            "questions": [
                {
                    "id": item.id,
                    "group": item.group,
                    "answerable": item.answerable,
                    "spans": item.spans,
                    "matched_spans": item.matched_spans,
                    "recall_at_6": item.recall,
                    "ndcg_at_6": item.ndcg,
                    "returned": item.returned,
                    "false_hit": item.false_hit,
                    "abstained": item.abstained,
                    "leaked": item.leaked,
                }
                for item in self.questions
            ],
        }


def _span(value: Mapping[str, Any]) -> GoldSpan:
    if set(value) != ANSWER_FIELDS:
        raise ValueError("Gold answer must use only immutable source fields")
    return GoldSpan(
        repository=str(value["repository"]),
        sha=str(value["sha"]),
        path=str(value["path"]),
        start_line=int(value["start_line"]),
        end_line=int(value["end_line"]),
    )


def _ndcg(matches: Sequence[RetrievalMatch], spans: Sequence[GoldSpan]) -> float:
    matched: set[int] = set()
    dcg = 0.0
    for rank, match in enumerate(matches, start=1):
        fresh = {
            index
            for index, span in enumerate(spans)
            if index not in matched and match.overlaps(span)
        }
        if fresh:
            matched.update(fresh)
            dcg += 1.0 / math.log2(rank + 1)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(spans), K) + 1))
    return dcg / ideal if ideal else 0.0


def score_questions(
    gold: Mapping[str, Any],
    results: Mapping[str, Sequence[RetrievalMatch]],
    *,
    k: int = K,
) -> QualityReport:
    """Score one fixed set; answerable questions alone form the macro means."""

    if k < 1:
        raise ValueError("k must be positive")
    rows: list[ScoredQuestion] = []
    for question in gold.get("questions", ()):
        identifier = str(question["id"])
        group = str(question["group"])
        spans = tuple(_span(item) for item in question.get("answers", ()))
        matches = tuple(results.get(identifier, ()))[:k]
        matched = {
            index
            for index, span in enumerate(spans)
            if any(match.overlaps(span) for match in matches)
        }
        answerable = bool(spans)
        forbidden_repository = question.get("forbidden_repository")
        markers = tuple(str(item) for item in question.get("forbidden_markers", ()))
        leaked = any(
            (
                forbidden_repository is not None
                and match.repository == forbidden_repository
            )
            or any(
                marker in (match.text or "") or marker in (match.path or "")
                for marker in markers
            )
            for match in matches
        )
        rows.append(
            ScoredQuestion(
                id=identifier,
                group=group,
                answerable=answerable,
                spans=len(spans),
                matched_spans=len(matched),
                recall=(len(matched) / len(spans)) if answerable else None,
                ndcg=_ndcg(matches, spans) if answerable else None,
                returned=len(matches),
                false_hit=not answerable and bool(matches),
                abstained=not answerable and not matches,
                leaked=leaked,
            )
        )
    groups: dict[str, GroupReport] = {}
    for name in sorted({item.group for item in rows}):
        members = [item for item in rows if item.group == name]
        answerable = [item for item in members if item.answerable]
        groups[name] = GroupReport(
            questions=len(members),
            answerable=len(answerable),
            macro_recall=(
                sum(item.recall for item in answerable) / len(answerable)
                if answerable
                else None
            ),
            macro_ndcg=(
                sum(item.ndcg for item in answerable) / len(answerable)
                if answerable
                else None
            ),
            false_hits=sum(1 for item in members if item.false_hit),
            abstentions=sum(1 for item in members if item.abstained),
            leaks=sum(1 for item in members if item.leaked),
        )
    return QualityReport(k=k, questions=tuple(rows), groups=groups)


def validate_quality(gold: Mapping[str, Any], *, root: Path) -> None:
    """Reject a fixture that cannot be an immutable, group-complete gold set."""

    if not isinstance(gold.get("version"), str) or not gold["version"]:
        raise ValueError("Quality fixture requires a version")
    repositories = gold.get("repositories")
    if not isinstance(repositories, Mapping) or not repositories:
        raise ValueError("Quality fixture requires repositories")
    for name, repository in repositories.items():
        if not isinstance(repository, Mapping):
            raise TypeError(f"Repository {name} must be an object")
        if not SHA.fullmatch(str(repository.get("sha", ""))):
            raise ValueError(f"Repository {name} requires a full SHA")
        if repository.get("sha") is None:
            raise ValueError(f"Repository {name} requires a source SHA")
        target = repository.get("target_sha")
        if target is not None and not SHA.fullmatch(str(target)):
            raise ValueError(f"Repository {name} target SHA is invalid")
        path = Path(str(repository.get("root", "")))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError(f"Repository {name} root must stay inside fixtures")
        if not (root / path).is_dir():
            raise ValueError(f"Repository {name} root does not exist")
    questions = gold.get("questions")
    if not isinstance(questions, list) or len(questions) < MINIMUM_QUESTIONS:
        raise ValueError(
            f"Quality fixture requires at least {MINIMUM_QUESTIONS} questions"
        )
    identifiers = [str(question.get("id")) for question in questions]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Question identifiers must be unique")
    counts = {name: 0 for name in REQUIRED_GROUPS}
    for question in questions:
        group = str(question.get("group"))
        if group not in counts:
            raise ValueError(f"Unknown question group {group}")
        counts[group] += 1
        selected = question.get("selected_repositories")
        if not isinstance(selected, list) or not selected:
            raise ValueError(f"Question {question['id']} requires a selected scope")
        if any(name not in repositories for name in selected):
            raise ValueError(f"Question {question['id']} selects an unknown repository")
        indexes = question.get("indexes")
        if not isinstance(indexes, list) or not indexes:
            raise ValueError(f"Question {question['id']} requires an explicit index")
        for index in indexes:
            if index.get("repository") not in selected:
                raise ValueError(
                    f"Question {question['id']} indexes an unselected repository"
                )
            if index.get("target") not in {"target", "active"}:
                raise ValueError(f"Question {question['id']} has an invalid target")
            if not SHA.fullmatch(str(index.get("sha", ""))):
                raise ValueError(f"Question {question['id']} has an invalid index SHA")
        answers = question.get("answers")
        if not isinstance(answers, list):
            raise TypeError(f"Question {question['id']} answers must be a list")
        spans = [_span(answer) for answer in answers]
        if len(set(spans)) != len(spans):
            raise ValueError(f"Question {question['id']} repeats a gold span")
        for span in spans:
            if span.repository not in selected:
                raise ValueError(
                    f"Question {question['id']} answers outside its selected scope"
                )
            if not (
                root / Path(repositories[span.repository]["root"]) / span.path
            ).is_file():
                raise ValueError(f"Question {question['id']} gold path does not exist")
        if group in {"no_answer", "isolated"} and spans:
            raise ValueError(f"Question {question['id']} must have no in-scope answer")
        if group == "isolated" and not question.get("forbidden_repository"):
            raise ValueError(
                f"Question {question['id']} requires a forbidden repository"
            )
        if group == "old_sha":
            for index in indexes:
                if index["target"] != "active":
                    raise ValueError("Old-SHA questions must select an active index")
    for name, minimum in GROUP_MINIMUMS.items():
        if counts[name] < minimum:
            raise ValueError(
                f"Group {name} requires at least {minimum} questions, got {counts[name]}"
            )

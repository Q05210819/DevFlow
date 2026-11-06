"""Fixed-set metrics: macro recall/nDCG, interval dedupe and honest abstention."""

import json
import math
from pathlib import Path

import pytest
from support.retrieval_quality import (
    GROUP_MINIMUMS,
    MINIMUM_QUESTIONS,
    REQUIRED_GROUPS,
    RetrievalMatch,
    score_questions,
    validate_quality,
)

SHA = "b" * 40
OTHER_SHA = "c" * 40
FIXTURES = Path(__file__).parent / "fixtures" / "knowledge"


def span(path, start, end, *, sha=SHA):
    return {
        "repository": "base",
        "sha": sha,
        "path": path,
        "start_line": start,
        "end_line": end,
    }


def match(path, start, end, *, sha=SHA, text="", repository="base"):
    return RetrievalMatch(
        repository=repository,
        sha=sha,
        path=path,
        start_line=start,
        end_line=end,
        text=text,
    )


def test_macro_recall_and_ndcg_deduplicate_intervals():
    gold = {
        "version": "inline-test",
        "questions": [
            {
                "id": "q1",
                "group": "zh",
                "answers": [span("docs/a.md", 10, 12), span("docs/a.md", 30, 32)],
            },
            {"id": "q2", "group": "en", "answers": [span("docs/b.md", 5, 5)]},
            {"id": "q3", "group": "no_answer", "answers": []},
            {"id": "q4", "group": "no_answer", "answers": []},
        ],
    }
    results = {
        # Both chunks overlap the first gold interval only. The second chunk is
        # a duplicate and must not add recall or nDCG.
        "q1": [match("docs/a.md", 8, 13), match("docs/a.md", 9, 12)],
        "q2": [match("docs/b.md", 5, 5)],
        "q3": [],
        "q4": [match("docs/c.md", 1, 2)],
    }

    report = score_questions(gold, results)
    by_id = {item.id: item for item in report.questions}

    assert by_id["q1"].matched_spans == 1
    assert by_id["q1"].recall == 0.5
    assert by_id["q1"].ndcg == pytest.approx(1 / (1 + 1 / math.log2(3)))
    assert by_id["q2"].recall == 1.0
    assert by_id["q2"].ndcg == 1.0
    assert report.macro_recall_at_6 == pytest.approx(0.75)
    assert report.macro_ndcg_at_6 == pytest.approx((1 / (1 + 1 / math.log2(3)) + 1) / 2)
    assert report.answerable == 2
    assert report.no_answer == 2
    # No-answer questions never enter the recall denominator, but their
    # returned evidence and abstentions are still counted honestly.
    assert report.false_hits == 1
    assert report.abstentions == 1
    assert by_id["q3"].recall is None
    assert by_id["q3"].abstained
    assert by_id["q4"].false_hit
    assert report.groups["zh"].macro_recall == 0.5
    assert report.groups["no_answer"].false_hits == 1
    assert report.groups["no_answer"].abstentions == 1


def test_wrong_sha_and_path_never_match():
    gold = {
        "version": "inline-test",
        "questions": [
            {"id": "q1", "group": "en", "answers": [span("docs/a.md", 10, 12)]}
        ],
    }
    results = {"q1": [match("docs/a.md", 10, 12, sha=OTHER_SHA)]}
    report = score_questions(gold, results)
    assert report.macro_recall_at_6 == 0.0
    assert report.questions[0].matched_spans == 0


def test_forbidden_repository_and_marker_are_leaks():
    gold = {
        "version": "inline-test",
        "questions": [
            {
                "id": "q1",
                "group": "isolated",
                "answers": [],
                "forbidden_repository": "isolated",
                "forbidden_markers": ["ONLY-MARKER"],
            }
        ],
    }
    assert score_questions(gold, {"q1": [match("docs/a.md", 1, 2)]}).leaks == 0
    assert (
        score_questions(
            gold,
            {"q1": [match("docs/a.md", 1, 2, text="ONLY-MARKER")]},
        ).leaks
        == 1
    )
    assert (
        score_questions(
            gold,
            {"q1": [match("docs/a.md", 1, 2, repository="isolated")]},
        ).leaks
        == 1
    )


def test_quality_fixture_is_frozen_and_covers_required_groups():
    gold = json.loads((FIXTURES / "quality.json").read_text(encoding="utf-8"))
    validate_quality(gold, root=FIXTURES)

    questions = gold["questions"]
    assert len(questions) >= MINIMUM_QUESTIONS
    counts = {name: 0 for name in REQUIRED_GROUPS}
    for question in questions:
        counts[question["group"]] += 1
    for name, minimum in GROUP_MINIMUMS.items():
        assert counts[name] >= minimum
    assert set(counts) == set(REQUIRED_GROUPS)

    repositories = gold["repositories"]
    for question in questions:
        if question["group"] == "old_sha":
            for index in question["indexes"]:
                owner = repositories[index["repository"]]
                assert index["target"] == "active"
                assert index["sha"] == owner["sha"]
                assert index["sha"] != owner["target_sha"]
        if question["group"] == "isolated":
            assert question["answers"] == []
            assert question["forbidden_repository"] == "isolated"
            assert "ISOLATED-ONLY-MARKER-7f3a" in question["forbidden_markers"]


def test_fixture_gold_does_not_name_implementation_chunks():
    gold = json.loads((FIXTURES / "quality.json").read_text(encoding="utf-8"))
    for question in gold["questions"]:
        for answer in question["answers"]:
            assert set(answer) == {
                "repository",
                "sha",
                "path",
                "start_line",
                "end_line",
            }
            assert "chunk" not in json.dumps(answer).lower()
            assert "index_version" not in json.dumps(answer).lower()

"""Pure ranking rules: RRF k=60, stable ties, top 20 and final evidence caps."""

from uuid import UUID, uuid4

import pytest

from devflow.knowledge.milvus import RankedHit
from devflow.knowledge.retrieval import RankedChunk, fuse, select_evidence
from devflow.knowledge.schemas import ChunkLocation, SourceIdentity


def hit(index_id, chunk_id, rank):
    return RankedHit(chunk_id=chunk_id, index_version_id=index_id, rank=rank, score=0.0)


def test_rrf_k60_dedup_then_top20():
    index_id = uuid4()
    chunks = [uuid4() for _ in range(25)]
    bm25 = [hit(index_id, chunks[number], number + 1) for number in range(25)]
    # The first two chunks also appear in the dense route; identity is
    # (index version, chunk), so each is one fused candidate with both ranks.
    dense = [hit(index_id, chunks[0], 1), hit(index_id, chunks[1], 2)]

    fused = fuse(bm25, dense)
    assert len(fused) == 25
    by_chunk = {candidate.chunk_id: candidate for candidate in fused}
    assert by_chunk[chunks[0]].score == pytest.approx(1 / 61 + 1 / 61)
    assert by_chunk[chunks[1]].score == pytest.approx(1 / 62 + 1 / 62)
    assert by_chunk[chunks[2]].score == pytest.approx(1 / 63)
    assert by_chunk[chunks[0]].bm25_rank == 1
    assert by_chunk[chunks[0]].dense_rank == 1
    assert [candidate.chunk_id for candidate in fused[:20]] == chunks[:20]
    assert len(fused[:20]) == 20


def test_rrf_uses_chunk_id_as_stable_tie_break():
    index_id = uuid4()
    first = UUID(int=1)
    second = UUID(int=2)
    # Both appear in exactly one route at the same rank, so they tie exactly.
    fused = fuse(
        [hit(index_id, second, 1)],
        [hit(index_id, first, 1)],
    )
    assert [candidate.chunk_id for candidate in fused] == [first, second]
    assert fused[0].score == pytest.approx(fused[1].score)


def identity(*, document_id, revision_hash, repository_id):
    return SourceIdentity(
        repository_id=repository_id,
        source_repository_id=repository_id,
        dependency_repository_ids=(repository_id,),
        document_id=document_id,
        revision_hash=revision_hash,
        workspace_id=None,
        sha=None,
        path=None,
    )


def ranked(
    index_id,
    *,
    document_id,
    revision_hash,
    repository_id,
    start_byte,
    end_byte,
    chunk_id=None,
    score=0.0,
):
    return RankedChunk(
        chunk_id=chunk_id or uuid4(),
        index_version_id=index_id,
        identity=identity(
            document_id=document_id,
            revision_hash=revision_hash,
            repository_id=repository_id,
        ),
        location=ChunkLocation(
            start_line=1,
            end_line=1,
            start_byte=start_byte,
            end_byte=end_byte,
        ),
        text="passage",
        content_hash="a" * 64,
        score=score,
        document_id=document_id,
        revision_hash=revision_hash,
    )


def test_final_selection_caps_six_two_per_file_and_drops_overlap():
    index_id = uuid4()
    repository_id = uuid4()
    file_one = uuid4()
    file_two = uuid4()
    file_three = uuid4()
    file_four = uuid4()
    revision = "a" * 64

    first = ranked(
        index_id,
        document_id=file_one,
        revision_hash=revision,
        repository_id=repository_id,
        start_byte=0,
        end_byte=10,
    )
    candidates = [
        first,
        # Same (index, chunk) identity again: must be fused or skipped once.
        first,
        ranked(
            index_id,
            document_id=file_one,
            revision_hash=revision,
            repository_id=repository_id,
            start_byte=5,
            end_byte=15,
        ),
        ranked(
            index_id,
            document_id=file_one,
            revision_hash=revision,
            repository_id=repository_id,
            start_byte=10,
            end_byte=20,
        ),
        ranked(
            index_id,
            document_id=file_one,
            revision_hash=revision,
            repository_id=repository_id,
            start_byte=20,
            end_byte=30,
        ),
        ranked(
            index_id,
            document_id=file_two,
            revision_hash=revision,
            repository_id=repository_id,
            start_byte=0,
            end_byte=10,
        ),
        ranked(
            index_id,
            document_id=file_two,
            revision_hash=revision,
            repository_id=repository_id,
            start_byte=10,
            end_byte=20,
        ),
        ranked(
            index_id,
            document_id=file_two,
            revision_hash=revision,
            repository_id=repository_id,
            start_byte=20,
            end_byte=30,
        ),
        ranked(
            index_id,
            document_id=file_three,
            revision_hash=revision,
            repository_id=repository_id,
            start_byte=0,
            end_byte=10,
        ),
        ranked(
            index_id,
            document_id=file_four,
            revision_hash=revision,
            repository_id=repository_id,
            start_byte=0,
            end_byte=10,
        ),
    ]

    selected = select_evidence(candidates)
    assert len(selected) == 6
    assert len({item.chunk_id for item in selected}) == len(selected)
    per_file = {}
    for item in selected:
        per_file[item.document_id] = per_file.get(item.document_id, 0) + 1
    assert all(count <= 2 for count in per_file.values())
    for left in selected:
        for right in selected:
            if left is right or left.document_id != right.document_id:
                continue
            assert not (
                left.location.start_byte < right.location.end_byte
                and right.location.start_byte < left.location.end_byte
            )
    # The first-ranked chunk is always kept.
    assert selected[0].chunk_id == candidates[0].chunk_id
    # file_one's second non-overlapping chunk is kept, but its overlapping
    # third and its fourth (over the two-per-file cap) are not.
    kept_file_one = [item for item in selected if item.document_id == file_one]
    assert len(kept_file_one) == 2
    assert [item.location.start_byte for item in kept_file_one] == [0, 10]

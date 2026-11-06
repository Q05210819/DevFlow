"""Synthetic ranking fixture for retrieval quality checks.

This is prose-shaped code, not a copy of the product implementation.
"""

RRF_K = 60
RERANK_LIMIT = 20
EVIDENCE_LIMIT = 6
PER_FILE_LIMIT = 2


def fuse_ranked_lists(bm25_hits, dense_hits):
    """Merge two ranked lists by reciprocal rank with a stable tie break."""
    scores = {}
    for hits in (bm25_hits, dense_hits):
        for rank, chunk in enumerate(hits, start=1):
            key = (chunk.index_version_id, chunk.chunk_id)
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))


def select_evidence(candidates):
    """Keep at most six items, two per file, and reject overlapping bytes."""
    selected = []
    per_file = {}
    intervals = {}
    for candidate in candidates:
        file_key = candidate.document_id
        if per_file.get(file_key, 0) >= PER_FILE_LIMIT:
            continue
        if overlaps(intervals.get(file_key, []), candidate):
            continue
        per_file[file_key] = per_file.get(file_key, 0) + 1
        intervals.setdefault(file_key, []).append(candidate)
        selected.append(candidate)
        if len(selected) == EVIDENCE_LIMIT:
            break
    return selected


def overlaps(existing, candidate):
    """Return true when two half-open byte intervals intersect."""
    return any(
        candidate.start_byte < other.end_byte and other.start_byte < candidate.end_byte
        for other in existing
    )

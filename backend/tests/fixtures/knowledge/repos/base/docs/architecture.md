# Architecture Notes

This document is synthetic fixture content for retrieval quality checks.

## Immutable revisions

A knowledge import writes immutable document revisions.
Each revision records the workspace identifier, the source SHA, and a content hash.
Later imports append new revisions; they never rewrite an existing revision.

## Index publication

An index version becomes queryable only after every member chunk is written and verified.
Publication requires a member digest that matches the exact set of indexed chunks.
A stale executor cannot publish because its lease generation no longer matches the job row.

## Query scope

The query scope is frozen in PostgreSQL before any vector search.
Every selected repository and index version is pinned to a generation and a SHA.
Vectors that are not part of the frozen scope are filtered out by the vector store query.

## Fusion and reranking

Reciprocal-rank fusion uses the constant RRF_K equal to sixty.
The fusion deduplicates identical chunk identities before reranking.
The reranker is local, bounded, and never calls a cloud model.

## Evidence caps

At most six evidence items are returned for one query.
At most two items may come from the same document revision.
Overlapping byte intervals from one revision are rejected before the final result is built.

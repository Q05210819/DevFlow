# Deprecated Configuration

This synthetic fixture records configuration that exists only in the old SHA.

## Retrieval mode

The old configuration key `retrieval.mode` accepted the value `bm25_only`.
The newer target SHA removes that key entirely.
Using `retrieval.mode=bm25_only` on the new target is rejected as unknown configuration.

## Vector dimension

The old index used a dimension of 768.
The new index requires a dimension of 1024.
A query bound to the old active SHA must use the old fingerprint and must never mix dimensions.

## Residency

The old configuration allowed `residency=remote`.
The new target requires `residency=local` and rejects remote inference.
The active old index may still be queried until it is retired, but only with an explicit old SHA choice.

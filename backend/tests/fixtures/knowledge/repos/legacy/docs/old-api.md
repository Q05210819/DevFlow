# Legacy Search API

This synthetic document describes the older active index generation.
The repository target has moved to a newer SHA, but the query selects the old active SHA explicitly.

## Endpoint

The old endpoint is `/v1/search`.
It accepts a required `query` field and an optional `top_k` field.
The default `top_k` was ten, and the maximum accepted value was fifty.

## Response

The old response returned a flat `hits` array without citation identifiers.
Each hit carried only `path`, `start_line`, `end_line`, and a numeric `score`.
Clients could not verify which immutable revision produced a hit.

## Removal

The `/v1/search` endpoint was removed from the newer target SHA.
The replacement endpoint is `/v2/retrieval`, which requires an explicit selection.
Calling `/v1/search` against the new target returns a not-found response.

## Compatibility

Historical results remain readable while the old active index is still published.
Once the old index is retired, previews for its citations report source_expired.

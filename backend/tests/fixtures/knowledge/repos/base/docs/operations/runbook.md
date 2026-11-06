# On-call Runbook

This synthetic runbook describes the operating rules for the example service.
It is fixture text for retrieval quality measurement only.

## Alerting

The primary paging alert is `retrieval_latency_p99`.
A page fires when the ninety-ninth percentile latency stays above two seconds for ten minutes.
Secondary alerts such as `queue_backlog` are tickets, not pages.

## Escalation

Page the platform-oncall first, then the retrieval-oncall if the incident lasts longer than fifteen minutes.
Do not page the database team directly; the platform-oncall owns that escalation.
Record the incident identifier in the shared timeline before making changes.

## Draining

Before restarting a model worker, drain the queue instead of stopping the world.
Draining lets the current lease finish and prevents a stale executor from publishing results.
A worker restart must never cancel a running browser request as a substitute for job cancellation.

## Backup verification

Restore drills run every month against an isolated database.
Verify the restored row counts and the latest audit event before declaring the drill successful.
Never point a restore drill at the production database.

## Post-incident

Write a short review within two business days.
Include the failing component, the detection gap, and one concrete follow-up with an owner.

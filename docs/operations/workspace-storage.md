# Workspace storage and recovery

Workspace stores service-owned bare repositories by numeric GitHub repository ID. API mounts are read-only; only Worker maintenance/fetch operations may write. Never point the cache root at a checkout, another application's volume, or a network filesystem with unverified lock semantics.

## Capacity and retention

Defaults are 2 GiB per repository, 20 GiB for the cache root, seven unused days before an eligible version can be evicted, and 600 seconds of grace for failed-attempt orphans. Configuration keys are `WORKSPACE_REPOSITORY_BYTES`, `WORKSPACE_TOTAL_BYTES`, `WORKSPACE_UNUSED_DAYS`, and `WORKSPACE_ORPHAN_GRACE_SECONDS`. Bytes include packs, temporary files, unpublished attempts, unknown directories and residual data. A failed or expired Job does not make its bytes disappear.

Fetch admission and the supervisor monitor apply soft limits. The monitor checks during Git execution, ordinarily every 200 ms; directory traversal and filesystem operations can take longer. Scans are bounded to 100,000 filesystem entries and the operation deadline. Unreadable or unsupported storage fails closed. These limits can overshoot and are not a filesystem quota. Production requires a separately constrained filesystem/volume with a real deployment quota and headroom for temporary pack/repack files. An ordinary Compose named volume is not a hard quota.

Worker maintenance runs at most once per minute per Worker, with a ten-second sweep deadline and bounded repository/candidate batches. It shares the single global writer/GC slot and each repository's exclusive OS guard. Four unresolved reader operations globally are the maximum; a retained unknown guard occupies its slot even when expired. Busy repositories defer maintenance rather than delete evidence under a reader.

Current default-branch and open-PR requirements, recently admitted readers, and publishable attempts remain protected. Open-PR protection is independent of the Health display cap. A completed Git process can still have a pending database publication: its exact pin remains protected while the matching Job attempt is running, including after lease expiry until durable queue recovery revokes that attempt. Orphan deletion needs both positive physical drain and grace after revocation.

An eligible published version becomes `unavailable` before ref deletion, retaining its pin as a resumable cleanup marker. A later sweep can finish interrupted cleanup. Provenance, SHA and source identity remain permanently recorded. No remote ref is promised to remain recoverable after eviction. Re-preparation uses a fresh unique attempt; an orphan ref is never adopted as public authority.

Revoked GitHub access does not prevent internal cleanup of demonstrably service-owned bytes. It also does not permit deleting protected versions. Maintenance validates the persistent WorkspaceVersion/Repository numeric identity separately from read/fetch authorization. Missing ownership anchors, conflicting identities, unverified layouts and unknown runtime guards remain unavailable and counted toward capacity.

## Health availability

Health never fetches code or makes provider calls to fill availability. Its preflight shares one three-second deadline, also subject to the existing Health attempt's earlier cancellation. It tries at most 16 distinct physical pins without waiting for a busy global GC slot. Selection order is default branch, visible PR number/base/head, then frozen CI order; at most 401 logical candidates derive from the already capped Health evidence.

`manifest.workspace_versions` records `available`, `unprepared`, `unavailable` or `unknown`, fixed identity, safe reason and a check time where known. A successful check establishes only local pin/commit/root-tree identity at observation time. It does not establish full blob completeness, the latest remote branch SHA, CI checkout contents, or the current viewer's right to read a fork. Old branch snapshots are not current-source proof.

`workspace_selection.total_candidates` and `returned_count` count only the current visible Health evidence, not the entire repository population. Existing PR/CI coverage and truncation remain authoritative. `checked_pins` counts distinct physical verification attempts (maximum 16); `checked_count` counts logical entries with a definite physical integrity conclusion. `unverified_count` counts entries requiring verification whose conclusion remains unknown, not known unprepared entries. Historical snapshots without these fields retain an empty evidence list and null selection summary: no inspection is implied.

The top-level summary is `available` only when every entry is known available, `partial` when available and other states coexist, `unavailable` when all entries are explicitly not available, and `unknown` when no available evidence exists and uncertainty remains (including no evidence). Freeze/publish generation ordering and failed-refresh retention remain unchanged.

## Exceptional physical-guard recovery

Expiry is not evidence of process death. Normal parent exit/cancellation is handled by the owning supervisor, which terminates/reaps its tree before a nonce-bound physical drain acknowledgement. An unknown guard is never automatically cleared from PID absence, an unavailable Windows Job name, a successful readiness check, or a restarted application.

The online implementation deliberately retains unknown guards. Windows exceptional online proof, if available to an operator, requires the still-live original runtime's retained matching handles and creation identity. A restarted/foreign runtime cannot reconstruct that proof from a PID or path. Use the following offline procedure when that original proof is unavailable.

1. Record the affected deployment's exact cache root and all unresolved operation IDs, nonces, runtime UUIDs, supervisor creation identities and Jobs. Query `workspace_process_guards`, joined to `workspace_versions` and `repositories`, and verify numeric cache ownership. Exclude every other deployment's rows. Save this inventory before changing any state.
2. Stop admission and **all** API/Worker instances of this owned deployment, including replicas and old containers. On Linux, use the deployment's normal Compose/container stop command, then inspect every previously inventoried API/Worker container: it must be stopped and have no live init PID. A completed stop and destruction of each old PID namespace establishes that its descendants ended. Stopping only one Python process inside a live container does not. A failed/still-running stop is not proof.
3. For native Windows, stop only the inventoried owned application/supervisor trees, and verify the original owned Job/process handles are signaled with no active processes. If those handles/ownership cannot be established, retain the guards; a verified host reboot ending the old runtime is the fallback. Do not kill arbitrary Git processes or infer death from absent/reused PIDs.
4. Keep API/Worker admission stopped while applying a short database maintenance transaction to each **exact inventoried** guard. The following parameterized SQL is a template, not an automatic proof checker. Bind values from the verified inventory, not a user request or a broad age-based query:

   ```sql
   BEGIN;
   SELECT operation_id, nonce, runtime_id, repository_id, workspace_id,
          lock_path, state, identity
     FROM workspace_process_guards
    WHERE operation_id = :operation_id AND nonce = :nonce
      AND runtime_id = :runtime_id
    FOR UPDATE;
   -- Compare the single selected row against the saved verified-stop inventory.
   UPDATE workspace_process_guards
      SET state = 'drained', proof = 'offline_verified',
          drained_at = clock_timestamp()
    WHERE operation_id = :operation_id AND nonce = :nonce
      AND runtime_id = :runtime_id
      AND state IN ('reserved', 'active', 'cleanup_required');
   COMMIT;
   ```

   Require exactly the expected row; otherwise roll back. Do not change published version status to ready, replace a nonce, rewrite old leases, remove provenance or delete the cache in this transaction. `offline_verified` releases exclusion after operator proof but cannot satisfy the normal write-publication requirement for `physical`.
5. Restart the owned deployment. Ordinary queue recovery revokes expired Jobs; retries receive new attempts. Maintenance still enforces protection, last-use and grace before reclaiming old refs. Run a new Health snapshot; positively missing objects become unavailable, while indeterminate checks remain unknown. If capacity or containment stays unresolved, stop and retain the evidence rather than repeatedly clearing guards.

Unknown/unanchored directories or layouts that cannot be inspected safely require a separate operator-scoped storage repair after the same verified stop. Verify resolved absolute targets are inside the exact owned root and preserve a backup/evidence inventory. No online generic path deletion or repository-adoption command is provided.

This runbook does not claim an executed production stop/recovery exercise. Real Linux containment/fetch, private/fork permissions, near-1-GiB usage, restore and deployment hard-quota checks remain the separate phase acceptance gates.

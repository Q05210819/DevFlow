export type CorpusStatus = "queued" | "building" | "ready" | "ready_partial" | "failed" | "superseded";
export type CorpusState = {
  import_id: string;
  kind: "upload" | "workspace" | "delete";
  status: CorpusStatus;
  target_generation: number;
  included: number;
  skipped: number;
  unsupported: number;
  over_limit: number;
  unscanned: boolean;
  complete: boolean;
  scan_reason: string | null;
  processed_chunks: number;
  created_at: string;
  deadline: string;
};
export type DocumentSummary = {
  document_id: string;
  kind: "upload" | "workspace";
  path: string | null;
  filename: string | null;
  deleted: boolean;
  published_revisions: number;
  latest_published_revision_id: string | null;
  latest_published_at: string | null;
};
export type DocumentPage = { items: DocumentSummary[]; next_cursor: string | null };
export type ScanLimits = { max_files: number; max_scan_bytes: number; max_file_bytes: number };
export type AvailabilityStatus = "not_indexed" | "building" | "ready" | "stale";
export type AvailabilityReason =
  | "no_target" | "target_not_indexed" | "target_building" | "target_failed" | "target_pending"
  | "active_unavailable" | "vectors_reclaimed" | "fingerprint_mismatch" | "schema_unsupported"
  | "collection_missing" | "schema_mismatch" | "vectors_missing";
export type KnowledgeView = {
  repository_id: string;
  target_generation: number;
  target_workspace_id: string | null;
  target_sha: string | null;
  path_rule_version: string;
  document_paths: string[];
  code_paths: string[];
  corpus: CorpusState | null;
  needs_reimport: boolean;
  index_status: AvailabilityStatus;
  index_reason: AvailabilityReason | null;
  active_index_generation: number | null;
  active_index_sha: string | null;
  documents: DocumentPage;
  limits: ScanLimits;
};
export type RevisionSummary = { revision_id: string; revision_hash: string; published_at: string; is_current: boolean; availability: "available" | "expired" | "deleted" | "unavailable" };
export type SourceIdentity = {
  repository_id: string;
  source_repository_id: string;
  dependency_repository_ids: string[];
  document_id: string;
  revision_hash: string;
  workspace_id: string | null;
  sha: string | null;
  path: string | null;
};
export type ChunkLocation = { start_line: number; end_line: number; start_byte: number; end_byte: number };
export type SourcePreview = {
  identity: SourceIdentity;
  text: string | null;
  location: ChunkLocation | null;
  is_current: boolean;
  partial: boolean;
  next_cursor: string | null;
  reason: "source_expired" | "source_unavailable" | null;
};
export type DocumentView = {
  document_id: string;
  repository_id: string;
  kind: "upload" | "workspace";
  path: string | null;
  filename: string | null;
  deleted: boolean;
  revisions: RevisionSummary[];
  revisions_next_cursor: string | null;
  preview: SourcePreview | null;
};
export type PathEntry = { path: string | null; mode: string; size: number | null; outcome: "selected" | "unsupported" | "excluded"; reason: string | null };
export type PathPage = { items: PathEntry[]; next_cursor: string | null; exhausted: boolean; truncated: boolean };
export type QueryTarget = "target" | "active";
export type IndexSelection = { repository_id: string; sha: string; target: QueryTarget };
export type QuerySelection = { selected_repository_ids: string[]; indexes: IndexSelection[] };
export type QueryReceipt = { query_id: string; job_id: string };
export type CoverageView = {
  repository_id: string;
  sha: string | null;
  target_generation: number;
  included: number;
  skipped: number;
  unsupported: number;
  over_limit: number;
  unscanned: boolean;
  complete: boolean;
};
export type EvidenceView = { citation_id: string; text: string; rank: number };
export type QueryResult = { status: "evidence" | "no_evidence"; evidence: EvidenceView[]; coverage: CoverageView[] };
export type CitationEntry = {
  citation_id: string;
  identity: SourceIdentity;
  location: ChunkLocation;
  url: string;
  content_hash: string;
  rank: number;
};
export type EvidenceManifest = { entries: CitationEntry[] };
export type QueryView = {
  query_id: string;
  job_id: string;
  status: "queued" | "running" | "retry_wait" | "succeeded" | "failed";
  selection: QuerySelection;
  question: string;
  manifest: EvidenceManifest | null;
  result: QueryResult | null;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
};
export type StageReceipt = { staging_id: string; expires_at: string };
export type ImportReceipt = { import_id: string; job_id: string; target_generation: number };

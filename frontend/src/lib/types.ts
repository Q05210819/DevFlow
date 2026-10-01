export type Role = "admin" | "member";

export type UserView = {
  id: string;
  github_id: number;
  login: string | null;
  role: Role;
  enabled: boolean;
};

export type Session = { user: UserView; csrf_token: string };

export type Page<T> = { items: T[]; next_cursor: string | null };

export type CandidateRepository = { github_repository_id: number; installation_id: number; full_name: string; private: boolean; default_branch: string | null };
export type RepositoryView = CandidateRepository & { id: string };
export type JobStatus = "queued" | "running" | "retry_wait" | "succeeded" | "failed";
export type ProbeResult = Omit<CandidateRepository, "installation_id"> & { checked_at: string };
export type JobView = { id: string; repository_id: string; requested_by: string; status: JobStatus; attempt: number; max_attempts: number; result: ProbeResult | null; error_code: string | null; last_sequence: number; created_at: string; updated_at: string };

export type AuditView = {
  id: string;
  actor_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  outcome: string;
  request_id: string | null;
  job_id: string | null;
  reason_code: string | null;
  created_at: string;
};

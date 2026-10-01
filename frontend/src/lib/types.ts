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

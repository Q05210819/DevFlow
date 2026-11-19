export type RunStatus = "queued" | "running" | "succeeded" | "failed";

export type RunSummary = { run_id: string; job_id: string; status: RunStatus };

export type ConversationView = {
  id: string;
  selected_repository_ids: string[];
  archived: boolean;
  created_at: string;
};

export type ConversationSummary = {
  id: string;
  selected_repository_ids: string[];
  created_at: string;
  active_run: RunSummary | null;
  latest_run: RunSummary | null;
};

export type ConversationList = { conversations: ConversationSummary[] };

export type RunReceipt = { run_id: string; job_id: string; message_id: string };

export type CitationView = {
  citation_id: string;
  url: string;
  repository_id: string;
  source_repository_id: string;
  path: string | null;
  sha: string | null;
  start_line: number;
  end_line: number;
};

export type MessageView = {
  id: string;
  role: "user" | "assistant";
  body: string;
  ordinal: number;
  created_at: string;
  citations: CitationView[];
};

export type MessagePage = { messages: MessageView[]; next_cursor: string | null };

export type DeletionReceipt = { conversation_id: string; job_id: string };

export interface Feedback {
  rating: "like" | "dislike" | null;
  comment: string | null;
}
export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  feedback: Feedback | null;
}
export interface Source {
  marker: string;
  document_id: string;
  filename: string;
  title: string;
  page: number | null;
  section_heading: string | null;
  unavailable: boolean;
}
export interface Message {
  id: string;
  conversation_id: string;
  turn_id: string;
  run_id: string | null;
  attempt_number: number;
  role: "user" | "assistant";
  content: string;
  status: "pending" | "completed" | "failed";
  sources: Source[];
  message_order: number;
  feedback: Feedback | null;
}
export interface Turn {
  id: string;
  status: "active" | "completed" | "failed";
  retry_available: boolean;
  attempts: {
    id: string;
    assistant_message_id: string;
    status: Message["status"];
  }[];
}
export interface PendingRequest {
  key: string;
  content: string;
  turnId?: string;
  expectedRunId?: string;
}
export type Recovery = Map<string, PendingRequest>;

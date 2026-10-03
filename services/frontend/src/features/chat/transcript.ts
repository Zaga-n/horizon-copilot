import type { Conversation, Message } from "./models";
import type { StreamState } from "./stream";
const TITLE_LIMIT = 72;
export function streamedMessage(
  state: StreamState,
  conversationId: string,
): Message {
  return {
    id: state.messageId!,
    conversation_id: conversationId,
    turn_id: state.turnId!,
    run_id: state.runId!,
    attempt_number: state.attempt,
    role: "assistant",
    content: state.text,
    status:
      state.phase === "completed"
        ? "completed"
        : state.phase === "failed"
          ? "failed"
          : "pending",
    sources: state.sources,
    message_order: Number.MAX_SAFE_INTEGER,
    feedback: null,
  };
}
// One keyed collection keeps the streaming bubble mounted when history takes over.
export function visibleTranscript(
  messages: Message[],
  transient: Message | null,
  provisionalQuestion: string,
  conversationId: string | null,
): Message[] {
  const visible = messages.filter((message) => message.id !== transient?.id);
  if (
    provisionalQuestion &&
    !messages.some(
      (message) =>
        message.role === "user" && message.turn_id === transient?.turn_id,
    )
  ) {
    visible.push({
      id: "provisional",
      conversation_id: conversationId!,
      turn_id: "",
      run_id: null,
      attempt_number: 1,
      role: "user",
      content: provisionalQuestion,
      status: "pending",
      sources: [],
      message_order: 0,
      feedback: null,
    });
  }
  if (transient) visible.push(transient);
  return visible;
}
// Show the first question immediately, then use the server title after reconciliation.
export function conversationTitle(
  detail: Conversation | null,
  visible: Message[],
): string {
  if (detail?.title && detail.title !== "New conversation") return detail.title;
  const firstQuestion = (
    visible.find((message) => message.role === "user")?.content ?? ""
  )
    .trim()
    .replace(/\s+/g, " ");
  const questionTitle =
    firstQuestion.length > TITLE_LIMIT
      ? firstQuestion.slice(0, TITLE_LIMIT - 1).trimEnd() + "…"
      : firstQuestion;
  return questionTitle || "New conversation";
}

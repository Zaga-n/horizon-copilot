import type { Source } from "./models";
interface Envelope {
  conversation_id: string;
  turn_id: string;
  run_id: string;
  assistant_message_id: string;
  attempt_number: number;
  sequence: number;
}
export type StreamEvent = Envelope &
  (
    | { type: "started" | "completed"; data: Record<string, never> }
    | { type: "delta"; data: { text: string } }
    | { type: "progress"; data: { phase: string; message: string } }
    | { type: "sources"; data: { sources: Source[] } }
    | {
        type: "failed";
        data: {
          persistence_pending: boolean;
          retry_available: boolean;
          text: string;
        };
      }
  );
function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object";
}
export function parseEvent(text: string): StreamEvent {
  const value: unknown = JSON.parse(text);
  if (!record(value) || !record(value.data)) throw new Error("Invalid event.");
  for (const field of [
    "conversation_id",
    "turn_id",
    "run_id",
    "assistant_message_id",
  ])
    if (typeof value[field] !== "string" || !value[field])
      throw new Error("Missing event identity.");
  if (
    !Number.isInteger(value.sequence) ||
    Number(value.sequence) < 1 ||
    !Number.isInteger(value.attempt_number) ||
    Number(value.attempt_number) < 1
  )
    throw new Error("Invalid sequence.");
  const data = value.data;
  switch (value.type) {
    case "started":
    case "completed":
      break;
    case "delta":
      if (typeof data.text !== "string") throw new Error("Invalid delta.");
      break;
    case "progress":
      if (typeof data.phase !== "string" || typeof data.message !== "string")
        throw new Error("Invalid progress.");
      break;
    case "failed":
      if (
        typeof data.text !== "string" ||
        typeof data.persistence_pending !== "boolean" ||
        typeof data.retry_available !== "boolean"
      )
        throw new Error("Invalid failure.");
      break;
    case "sources":
      if (
        !Array.isArray(data.sources) ||
        !data.sources.every(
          (source: unknown) =>
            record(source) &&
            typeof source.marker === "string" &&
            typeof source.filename === "string" &&
            typeof source.title === "string" &&
            typeof source.document_id === "string" &&
            typeof source.unavailable === "boolean" &&
            (source.page == null || typeof source.page === "number"),
        )
      )
        throw new Error("Invalid sources.");
      break;
    default:
      throw new Error("Unknown event.");
  }
  return value as unknown as StreamEvent;
}
export async function decodeSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let data: string[] = [];
  let kind = "";
  const line = (value: string) => {
    if (value === "") {
      if (data.length) {
        const event = parseEvent(data.join("\n"));
        if (kind && kind !== event.type)
          throw new Error("Mismatched event type.");
        onEvent(event);
      }
      data = [];
      kind = "";
      return;
    }
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    const rest = colon < 0 ? "" : value.slice(colon + 1).replace(/^ /, "");
    if (field === "data") data.push(rest);
    if (field === "event") kind = rest;
  };
  const consume = (end: boolean) => {
    for (;;) {
      const match = /[\r\n]/.exec(buffer);
      if (!match) break;
      const index = match.index;
      if (buffer[index] === "\r" && index === buffer.length - 1 && !end) break;
      const width =
        buffer[index] === "\r" && buffer[index + 1] === "\n" ? 2 : 1;
      line(buffer.slice(0, index));
      buffer = buffer.slice(index + width);
    }
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      consume(false);
    }
    buffer += decoder.decode();
    consume(true);
    // An incomplete event at EOF is not a committed terminal outcome.
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
export interface StreamState {
  phase: "waiting" | "streaming" | "completed" | "failed";
  sequence: number;
  runId?: string;
  turnId?: string;
  messageId?: string;
  attempt: number;
  text: string;
  progress: string;
  sources: Source[];
  persistencePending: boolean;
}
export const initialStream: StreamState = {
  phase: "waiting",
  sequence: 0,
  attempt: 1,
  text: "",
  progress: "Connecting…",
  sources: [],
  persistencePending: false,
};
export function applyEvent(
  state: StreamState,
  event: StreamEvent,
): StreamState {
  if (state.phase === "completed" || state.phase === "failed") return state;
  if (state.runId && state.runId !== event.run_id)
    throw new Error("Run changed during stream.");
  if (event.sequence <= state.sequence) return state;
  if (
    event.sequence !== state.sequence + 1 ||
    (state.sequence === 0 && event.type !== "started")
  )
    throw new Error("Stream sequence gap.");
  const next: StreamState = {
    ...state,
    phase: "streaming",
    sequence: event.sequence,
    runId: event.run_id,
    turnId: event.turn_id,
    messageId: event.assistant_message_id,
    attempt: event.attempt_number,
  };
  switch (event.type) {
    case "delta":
      next.text += event.data.text;
      break;
    case "sources":
      next.sources = event.data.sources;
      break;
    case "progress":
      next.progress = event.data.message;
      break;
    case "completed":
      next.phase = "completed";
      next.progress = "Answer complete.";
      break;
    case "failed":
      next.phase = "failed";
      next.progress = event.data.text;
      next.persistencePending = event.data.persistence_pending;
      break;
  }
  return next;
}

import { useCallback, useEffect, useRef, useState } from "react";
import { Api, ApiError, errorMessage } from "../../shared/api";
import type {
  Conversation,
  Feedback,
  Message,
  PendingRequest,
  Recovery,
  Turn,
} from "./models";
import { applyEvent, decodeSse, initialStream } from "./stream";
import { streamedMessage } from "./transcript";
const POLL_INTERVAL_MS = 2000;
interface SessionEvents {
  onSelect: () => void;
  onStreamStart: () => void;
  onStreamSettled: () => void;
}
// Owns conversation history, the live stream, and reconciliation with saved server state.
export function useChatSession(
  api: Api,
  recovery: Recovery,
  events: SessionEvents,
) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [turns, setTurns] = useState<Record<string, Turn>>({});
  const [transient, setTransient] = useState<Message | null>(null);
  const [provisionalQuestion, setProvisionalQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [transportActive, setTransportActive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [draft, setDraft] = useState("");
  const streamController = useRef<AbortController | null>(null);
  const current = useRef<string | null>(null);
  const busyRef = useRef(false);
  const refresh = useCallback(
    async (id: string, signal?: AbortSignal): Promise<void> => {
      const all: Message[] = [];
      let next: number | null = 0;
      const detailPromise = api.json<Conversation>(`/conversations/${id}`, {
        signal,
      });
      try {
        do {
          const page: { messages: Message[]; next_cursor: number | null } =
            await api.json(
              `/conversations/${id}/messages?cursor=${next}&limit=100`,
              { signal },
            );
          all.push(...page.messages);
          next = page.next_cursor;
        } while (next !== null);
        const conversation = await detailPromise;
        const request = recovery.get(id);
        const turnIds = new Set(
          all
            .filter(
              (message) =>
                message.role === "assistant" && message.status !== "completed",
            )
            .map((message) => message.turn_id),
        );
        if (request?.turnId) turnIds.add(request.turnId);
        const values = await Promise.all(
          [...turnIds].map((turnId) =>
            api.json<Turn>(`/conversations/${id}/turns/${turnId}`, { signal }),
          ),
        );
        if (current.current !== id || signal?.aborted) return;
        setMessages(all.sort((a, b) => a.message_order - b.message_order));
        setDetail(conversation);
        setConversations((previous) =>
          previous.map((item) => (item.id === id ? conversation : item)),
        );
        setTurns(Object.fromEntries(values.map((turn) => [turn.id, turn])));
        const accepted = request?.turnId
          ? values.find((turn) => turn.id === request.turnId)
          : undefined;
        if (accepted && accepted.status !== "active") recovery.delete(id);
        const active =
          values.some((turn) => turn.status === "active") ||
          all.some((message) => message.status === "pending") ||
          recovery.has(id);
        busyRef.current = active;
        setBusy(active);
        setStatus(active ? "Checking saved work…" : "");
      } catch (error) {
        await detailPromise.catch(() => undefined);
        throw error;
      }
    },
    [api, recovery],
  );
  const loadConversations = useCallback(
    async (next?: string): Promise<void> => {
      const page = await api.json<{
        conversations: Conversation[];
        next_cursor: string | null;
      }>(
        `/conversations?limit=50${next ? `&cursor=${encodeURIComponent(next)}` : ""}`,
      );
      setConversations((previous) =>
        next ? [...previous, ...page.conversations] : page.conversations,
      );
      setCursor(page.next_cursor);
    },
    [api],
  );
  useEffect(() => {
    let active = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- The loader sets state only after the network response.
    void loadConversations()
      .catch((error) => {
        if (active) setError(errorMessage(error));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [loadConversations]);
  useEffect(() => {
    if (!selected) return;
    // A newly created conversation is populated by its stream; an empty history
    // response must not overwrite the active turn's progress or busy state.
    if (streamController.current && !streamController.current.signal.aborted)
      return () => streamController.current?.abort();
    const controller = new AbortController();
    void refresh(selected, controller.signal)
      .catch((error) => {
        if (!controller.signal.aborted) setError(errorMessage(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      streamController.current?.abort();
    };
  }, [selected, refresh]);
  useEffect(() => {
    if (!selected || !busy || transient || transportActive) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!document.hidden) {
        try {
          await refresh(selected);
        } catch (error) {
          if (!stopped) setError(errorMessage(error));
        }
      }
      if (!stopped) timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    };
    timer = setTimeout(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [selected, busy, transient, transportActive, refresh]);
  function select(id: string | null): void {
    streamController.current?.abort();
    current.current = id;
    setSelected(id);
    setMessages([]);
    setDetail(null);
    setTurns({});
    setTransient(null);
    setProvisionalQuestion("");
    setDraft("");
    setError("");
    setStatus("");
    setLoading(!!id);
    busyRef.current = !!id;
    setBusy(!!id);
    setTransportActive(false);
    events.onSelect();
  }
  async function create(): Promise<string> {
    const conversation = await api.json<Conversation>("/conversations", {
      method: "POST",
    });
    setConversations((previous) => [conversation, ...previous]);
    select(conversation.id);
    return conversation.id;
  }
  async function execute(id: string, request: PendingRequest): Promise<void> {
    if (streamController.current && !streamController.current.signal.aborted)
      return;
    const controller = new AbortController();
    streamController.current = controller;
    setTransportActive(true);
    setLoading(false);
    busyRef.current = true;
    setBusy(true);
    setError("");
    setStatus("Connecting…");
    events.onStreamStart();
    let state = { ...initialStream };
    const retrying = !!request.expectedRunId;
    if (!retrying) setProvisionalQuestion(request.content);
    try {
      const response = await api.response(
        retrying
          ? `/conversations/${id}/turns/${request.turnId}:retry`
          : `/conversations/${id}/turns:stream`,
        {
          method: "POST",
          signal: controller.signal,
          headers: { "Idempotency-Key": request.key },
          body: JSON.stringify(
            retrying
              ? { expected_run_id: request.expectedRunId }
              : { content: request.content },
          ),
        },
      );
      if (response.headers.get("Content-Type")?.includes("text/event-stream")) {
        if (!response.body) throw new Error("Missing stream.");
        await decodeSse(response.body, (event) => {
          if (event.conversation_id !== id)
            throw new Error("Conversation changed.");
          state = applyEvent(state, event);
          request.turnId = state.turnId;
          if (current.current !== id || controller.signal.aborted) return;
          setStatus(state.progress);
          setTransient(streamedMessage(state, id));
        });
        if (state.phase !== "completed" && state.phase !== "failed")
          throw new Error("Completion unknown.");
      } else {
        const turn = (await response.json()) as Turn;
        request.turnId = turn.id;
      }
      if (current.current === id && !controller.signal.aborted) {
        await refresh(id, controller.signal);
        setTransient(null);
        setProvisionalQuestion("");
      }
    } catch (error) {
      if (current.current !== id || controller.signal.aborted) return;
      if (
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500 &&
        error.status !== 401 &&
        error.status !== 409
      )
        recovery.delete(id);
      setError(errorMessage(error));
      setStatus("Reconciling saved work…");
      try {
        await refresh(id, controller.signal);
        setTransient(null);
        setProvisionalQuestion("");
      } catch {
        setStatus("Outcome unknown. Check the saved status before continuing.");
      }
    } finally {
      if (current.current === id && !controller.signal.aborted) {
        streamController.current = null;
        setTransportActive(false);
        events.onStreamSettled();
      }
    }
  }
  async function send(): Promise<void> {
    if (
      busyRef.current ||
      (streamController.current && !streamController.current.signal.aborted) ||
      !draft.trim()
    )
      return;
    busyRef.current = true;
    setBusy(true);
    const content = draft.trim();
    try {
      const id = selected ?? (await create());
      const request = { key: crypto.randomUUID(), content };
      recovery.set(id, request);
      setDraft("");
      await execute(id, request);
    } catch (error) {
      busyRef.current = false;
      setBusy(false);
      setError(errorMessage(error));
    }
  }
  async function retry(message: Message): Promise<void> {
    if (!selected || busyRef.current || !message.run_id) return;
    const request = {
      key: crypto.randomUUID(),
      content: "",
      turnId: message.turn_id,
      expectedRunId: message.run_id,
    };
    recovery.set(selected, request);
    await execute(selected, request);
  }
  function checkOutcome(): void {
    if (selected) void execute(selected, recovery.get(selected)!);
  }
  function reconcile(): void {
    if (selected)
      void refresh(selected)
        .then(() => {
          setTransient(null);
          setProvisionalQuestion("");
        })
        .catch((error) => setError(errorMessage(error)));
    else
      void loadConversations().catch((error) => setError(errorMessage(error)));
  }
  function loadMore(): void {
    if (cursor)
      void loadConversations(cursor).catch((error) =>
        setError(errorMessage(error)),
      );
  }
  function conversationFeedbackSaved(value: Feedback | null): void {
    setDetail((previous) =>
      previous ? { ...previous, feedback: value } : null,
    );
  }
  function answerFeedbackSaved(id: string, value: Feedback | null): void {
    setMessages((previous) =>
      previous.map((item) =>
        item.id === id ? { ...item, feedback: value } : item,
      ),
    );
  }
  const outcomeUnknown =
    !!selected && recovery.has(selected) && !recovery.get(selected)?.turnId;
  return {
    conversations,
    cursor,
    selected,
    detail,
    messages,
    turns,
    transient,
    provisionalQuestion,
    busy,
    transportActive,
    loading,
    status,
    error,
    draft,
    outcomeUnknown,
    setDraft,
    select,
    send,
    retry,
    checkOutcome,
    reconcile,
    loadMore,
    conversationFeedbackSaved,
    answerFeedbackSaved,
  };
}

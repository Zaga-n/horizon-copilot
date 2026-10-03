import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ArrowDown, Compass, FileText, Menu } from "lucide-react";
import { Api } from "../../shared/api";
import { Dialog } from "../../shared/Dialog";
import type { Feedback, Message, Recovery } from "./models";
import { MessageBubble } from "./MessageBubble";
import { ConversationNav } from "./ConversationNav";
import { EmptyChat } from "./EmptyChat";
import { Composer } from "./Composer";
import { conversationTitle, visibleTranscript } from "./transcript";
import { useChatSession } from "./useChatSession";
import { useTranscriptScroll } from "./useTranscriptScroll";
interface Props {
  api: Api;
  recovery: Recovery;
  openDocuments: () => void;
  signOut: () => void;
  feedback: (
    kind: "answer" | "conversation",
    id: string,
    value: Feedback | null,
    saved: (value: Feedback | null) => void,
  ) => ReactNode;
}
export function Chat({
  api,
  recovery,
  openDocuments,
  signOut,
  feedback,
}: Props): ReactNode {
  const [navOpen, setNavOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const { list, away, sync, pin, reset, jumpToLatest, handlers } =
    useTranscriptScroll();
  const session = useChatSession(api, recovery, {
    onSelect: () => {
      setNavOpen(false);
      reset();
    },
    onStreamStart: pin,
    onStreamSettled: () => composer.current?.focus({ preventScroll: true }),
  });
  const {
    selected,
    messages,
    transient,
    provisionalQuestion,
    status,
    turns,
    busy,
  } = session;
  useEffect(() => {
    const frame = requestAnimationFrame(sync);
    return () => cancelAnimationFrame(frame);
  }, [messages, transient, provisionalQuestion, status, sync]);
  const visibleMessages = visibleTranscript(
    messages,
    transient,
    provisionalQuestion,
    selected,
  );
  const progressMessage =
    transient ??
    [...visibleMessages]
      .reverse()
      .find(
        (message) =>
          message.role === "assistant" && message.status === "pending",
      );
  const selectedTitle = conversationTitle(session.detail, visibleMessages);
  function footer(message: Message): ReactNode {
    if (
      message.role === "assistant" &&
      message.status === "completed" &&
      message.id !== transient?.id
    )
      return feedback("answer", message.id, message.feedback, (value) =>
        session.answerFeedbackSaved(message.id, value),
      );
    const turn = turns[message.turn_id];
    if (
      message.status === "failed" &&
      turn?.retry_available &&
      turn.attempts.at(-1)?.id === message.run_id
    )
      return (
        <button disabled={busy} onClick={() => void session.retry(message)}>
          Retry answer
        </button>
      );
    return undefined;
  }
  const navigation = (
    <ConversationNav
      conversations={session.conversations}
      selected={selected}
      selectedTitle={selectedTitle}
      loading={session.loading}
      hasMore={!!session.cursor}
      onSelect={session.select}
      onLoadMore={session.loadMore}
      onCollapse={() => {
        setCollapsed(true);
        setNavOpen(false);
      }}
      openDocuments={openDocuments}
      signOut={signOut}
    />
  );
  return (
    <div className={`workspace ${collapsed ? "nav-collapsed" : ""}`}>
      <aside className="sidebar">{navigation}</aside>
      <main className="chat">
        <header className="chat-header">
          <div className="header-title">
            <button
              aria-label="Open navigation"
              onClick={() => {
                setCollapsed(false);
                setNavOpen(true);
              }}
            >
              <Menu size={20} />
            </button>
            <div>
              <h1>{selected ? selectedTitle : "Your workspace"}</h1>
              <small>Grounded in your knowledge</small>
            </div>
          </div>
          <div className="header-actions">
            {session.detail &&
              feedback(
                "conversation",
                session.detail.id,
                session.detail.feedback,
                session.conversationFeedbackSaved,
              )}
            <button onClick={openDocuments}>
              <FileText size={18} /> Documents
            </button>
          </div>
        </header>
        <div
          className="transcript"
          ref={list}
          tabIndex={0}
          aria-label="Conversation messages"
          {...handlers}
        >
          <div className="reading-column">
            {session.loading && <p role="status">Loading workspace…</p>}
            {!session.loading &&
              !messages.length &&
              !transient &&
              !provisionalQuestion && (
                <EmptyChat
                  onStarter={(starter) => {
                    session.setDraft(starter);
                    composer.current?.focus();
                  }}
                  openDocuments={openDocuments}
                />
              )}
            {visibleMessages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                progress={
                  message.id === progressMessage?.id ? status : undefined
                }
                footer={footer(message)}
              />
            ))}
            {status && !progressMessage && (
              <article
                className="message assistant"
                aria-label="Horizon progress"
              >
                <div className="message-label">
                  <Compass size={18} /> Horizon
                </div>
                <div className="chat-status" role="status">
                  {status}
                </div>
              </article>
            )}
          </div>
        </div>
        <div className="composer-area">
          {away && (
            <button className="jump-latest" onClick={jumpToLatest}>
              Latest content <ArrowDown size={16} />
            </button>
          )}
          {session.error && (
            <div className="error" role="alert">
              {session.error}
              <button onClick={session.reconcile}>Refresh saved status</button>
            </div>
          )}
          {session.outcomeUnknown && (
            <button
              disabled={session.transportActive}
              onClick={session.checkOutcome}
            >
              Check submission outcome
            </button>
          )}
          <Composer
            ref={composer}
            draft={session.draft}
            disabled={busy || session.transportActive}
            onDraftChange={session.setDraft}
            onSend={() => void session.send()}
          />
        </div>
      </main>
      {navOpen && (
        <Dialog title="Conversations" drawer onClose={() => setNavOpen(false)}>
          <div className="mobile-nav">{navigation}</div>
        </Dialog>
      )}
    </div>
  );
}

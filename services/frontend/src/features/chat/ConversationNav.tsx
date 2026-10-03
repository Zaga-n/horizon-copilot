import type { ReactNode } from "react";
import { Compass, FileText, LogOut, PanelLeftClose, Plus } from "lucide-react";
import type { Conversation } from "./models";
function createdLabel(createdAt: string): string {
  const date = new Date(createdAt);
  return `${date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  })} · ${date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}
export function ConversationNav({
  conversations,
  selected,
  selectedTitle,
  loading,
  hasMore,
  onSelect,
  onLoadMore,
  onCollapse,
  openDocuments,
  signOut,
}: {
  conversations: Conversation[];
  selected: string | null;
  selectedTitle: string;
  loading: boolean;
  hasMore: boolean;
  onSelect: (id: string | null) => void;
  onLoadMore: () => void;
  onCollapse: () => void;
  openDocuments: () => void;
  signOut: () => void;
}): ReactNode {
  return (
    <>
      <div className="sidebar-brand">
        <div className="brand">
          <Compass size={24} /> Horizon
        </div>
        <button aria-label="Collapse navigation" onClick={onCollapse}>
          <PanelLeftClose size={18} />
        </button>
      </div>
      <button className="new-chat" onClick={() => onSelect(null)}>
        <Plus size={18} /> New conversation
      </button>
      <p className="eyebrow">YOUR CONVERSATIONS</p>
      <nav aria-label="Conversations">
        {conversations.map((conversation) => (
          <button
            key={conversation.id}
            aria-current={selected === conversation.id ? "page" : undefined}
            onClick={() => onSelect(conversation.id)}
          >
            {conversation.id === selected
              ? selectedTitle
              : conversation.title || "New conversation"}
            <small>{createdLabel(conversation.created_at)}</small>
          </button>
        ))}
        {!conversations.length && !loading && (
          <p className="muted">Your conversations will appear here.</p>
        )}
        {hasMore && (
          <button onClick={onLoadMore}>Load more conversations</button>
        )}
      </nav>
      <div className="sidebar-bottom">
        <button onClick={openDocuments}>
          <FileText size={18} /> Your documents
        </button>
        <button onClick={signOut}>
          <LogOut size={18} /> Sign out
        </button>
      </div>
    </>
  );
}

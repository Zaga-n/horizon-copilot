import { useState } from "react";
import type { ReactNode } from "react";
import { ThumbsUp, ThumbsDown, MessageSquare } from "lucide-react";
import { Dialog } from "../../shared/Dialog";
import { errorMessage } from "../../shared/api";
export interface SavedFeedback {
  rating: "like" | "dislike" | null;
  comment: string | null;
}
export function FeedbackControl({
  kind,
  value,
  save,
}: {
  kind: "answer" | "conversation";
  value: SavedFeedback | null;
  save: (value: SavedFeedback | null) => Promise<void>;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(value?.comment ?? "");
  const [rating, setRating] = useState<SavedFeedback["rating"]>(
    value?.rating ?? null,
  );
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  async function commit(
    next: SavedFeedback | null,
    close = false,
  ): Promise<void> {
    setPending(true);
    setError("");
    try {
      await save(next);
      if (close) setOpen(false);
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setPending(false);
    }
  }
  async function select(next: "like" | "dislike"): Promise<void> {
    setRating(next);
    await commit({
      rating: next,
      comment: next === "dislike" ? (value?.comment ?? null) : null,
    });
    if (next === "dislike") setOpen(true);
  }
  return (
    <div className="feedback-controls" aria-label={`${kind} feedback`}>
      {kind === "answer" ? (
        <>
          <button
            aria-label="Like answer"
            aria-pressed={value?.rating === "like"}
            disabled={pending}
            onClick={() => void select("like")}
          >
            <ThumbsUp size={16} />
          </button>
          <button
            aria-label="Dislike answer"
            aria-pressed={value?.rating === "dislike"}
            disabled={pending}
            onClick={() => void select("dislike")}
          >
            <ThumbsDown size={16} />
          </button>
          {value?.rating === "dislike" && (
            <button
              disabled={pending}
              onClick={() => {
                setRating("dislike");
                setOpen(true);
              }}
            >
              Edit comment
            </button>
          )}
          {value && (
            <button disabled={pending} onClick={() => void commit(null)}>
              Clear feedback
            </button>
          )}
        </>
      ) : (
        <button
          onClick={() => {
            setRating(value?.rating ?? null);
            setOpen(true);
          }}
        >
          <MessageSquare size={16} /> Conversation feedback
          {value ? " · Saved" : ""}
        </button>
      )}
      {pending && <span role="status">Saving…</span>}
      {error && !open && <p role="alert">{error}</p>}
      {open && (
        <Dialog
          title={
            kind === "answer"
              ? "Optional dislike comment"
              : "Conversation feedback"
          }
          onClose={() => setOpen(false)}
        >
          {kind === "conversation" && (
            <div className="rating-options">
              <button
                aria-pressed={rating === "like"}
                onClick={() => setRating(rating === "like" ? null : "like")}
              >
                Like
              </button>
              <button
                aria-pressed={rating === "dislike"}
                onClick={() =>
                  setRating(rating === "dislike" ? null : "dislike")
                }
              >
                Dislike
              </button>
            </div>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void commit(
                {
                  rating: kind === "answer" ? "dislike" : rating,
                  comment: draft.trim() || null,
                },
                true,
              );
            }}
          >
            <label htmlFor="feedback-comment">
              {kind === "answer"
                ? "What could be better? (optional)"
                : "Your comment (optional)"}
            </label>
            <textarea
              id="feedback-comment"
              maxLength={4000}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              rows={5}
            />
            {error && <p role="alert">{error}</p>}
            <div className="dialog-actions">
              <button type="button" onClick={() => setOpen(false)}>
                Dismiss
              </button>
              {value && (
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => void commit(null, true)}
                >
                  Delete feedback
                </button>
              )}
              <button
                className="primary"
                disabled={
                  pending ||
                  (kind === "conversation" && !rating && !draft.trim())
                }
              >
                Save comment
              </button>
            </div>
          </form>
        </Dialog>
      )}
    </div>
  );
}

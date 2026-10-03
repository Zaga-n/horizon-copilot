import type { ReactNode, RefObject } from "react";
import { ArrowUp } from "lucide-react";
export function Composer({
  ref,
  draft,
  disabled,
  onDraftChange,
  onSend,
}: {
  ref: RefObject<HTMLTextAreaElement | null>;
  draft: string;
  disabled: boolean;
  onDraftChange: (draft: string) => void;
  onSend: () => void;
}): ReactNode {
  return (
    <>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          onSend();
        }}
      >
        <label className="sr-only" htmlFor="question">
          Ask Horizon
        </label>
        <textarea
          id="question"
          ref={ref}
          value={draft}
          onChange={(event) => onDraftChange(event.target.value)}
          placeholder="Ask a question about your documents…"
          maxLength={16000}
          rows={2}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              onSend();
            }
          }}
        />
        <button
          className="primary send"
          aria-label="Send question"
          disabled={disabled || !draft.trim()}
        >
          <ArrowUp size={20} />
        </button>
      </form>
      <p className="composer-note">
        Horizon can make mistakes. Check the sources.{" "}
        <span>Shift + Enter for a new line</span>
      </p>
    </>
  );
}

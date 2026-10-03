import { memo } from "react";
import type { ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Compass } from "lucide-react";
import type { Message } from "./models";
export const MessageBubble = memo(function MessageBubble({
  message,
  footer,
  progress,
}: {
  message: Message;
  footer?: ReactNode;
  progress?: string;
}): ReactNode {
  return (
    <article
      className={`message ${message.role}`}
      aria-label={`${message.role === "user" ? "Your question" : "Horizon answer"}${message.status === "failed" ? ", failed attempt" : ""}`}
    >
      <div className="message-label">
        {message.role === "assistant" && <Compass size={18} />}{" "}
        {message.role === "assistant" ? "Horizon" : "You"}
        {message.attempt_number > 1 && (
          <small>Attempt {message.attempt_number}</small>
        )}
      </div>
      {progress && (
        <div className="chat-status" role="status">
          {progress}
        </div>
      )}
      <div className="message-body">
        <Markdown
          remarkPlugins={[remarkGfm]}
          skipHtml
          components={{
            img: () => null,
            a: ({ children, href }) => (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ),
          }}
        >
          {message.content}
        </Markdown>
      </div>
      {message.status === "failed" && (
        <p className="failed-label">
          This answer could not be completed. Partial text may be incomplete.
        </p>
      )}
      {message.sources.length > 0 && (
        <details className="sources">
          <summary>Sources · {message.sources.length}</summary>
          <ol>
            {message.sources.map((source) => (
              <li key={source.marker}>
                <strong>
                  {source.marker} {source.title || source.filename}
                </strong>
                <span>
                  {source.filename}
                  {source.page ? ` · Page ${source.page}` : ""}
                  {source.section_heading ? ` · ${source.section_heading}` : ""}
                  {source.unavailable ? " · Source unavailable" : ""}
                </span>
              </li>
            ))}
          </ol>
        </details>
      )}
      {footer}
    </article>
  );
});

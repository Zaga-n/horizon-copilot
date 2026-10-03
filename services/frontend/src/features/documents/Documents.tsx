import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  FileText,
  Upload,
  Trash2,
  RotateCcw,
  LoaderCircle,
  Check,
} from "lucide-react";
import { Dialog } from "../../shared/Dialog";
import type { Api } from "../../shared/api";
import { errorMessage } from "../../shared/api";
import type { DocumentRecovery, DocumentStatus } from "./models";
import { isActive, stageLabel } from "./models";
import { useDocuments } from "./useDocuments";
export function Documents({
  api,
  recovery,
  open,
  onClose,
}: {
  api: Api;
  recovery: DocumentRecovery;
  open: boolean;
  onClose: () => void;
}): ReactNode {
  // Keep the feature mounted when its drawer closes so accepted work stays visible on reopen.
  const state = useDocuments(api, recovery);
  const [confirmation, setConfirmation] = useState<DocumentStatus | null>(null);
  const [dragging, setDragging] = useState(false);
  const [refreshError, setRefreshError] = useState("");
  const refresh = state.refresh;
  useEffect(() => {
    if (open)
      void refresh().catch((error) => setRefreshError(errorMessage(error)));
  }, [open, refresh]);
  const unavailable = state.transfer === "transferring" || !!recovery.upload;
  if (!open) return null;
  return (
    <Dialog title="Your documents" drawer onClose={onClose}>
      <p className="documents-intro">
        Your sources, ready for a clearer answer.
      </p>
      <div
        className={`upload-zone ${dragging ? "dragging" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (unavailable) return;
          if (event.dataTransfer.files.length === 1)
            void state.choose(event.dataTransfer.files[0]);
          else setRefreshError("Choose one document at a time.");
        }}
      >
        <Upload size={24} />
        <strong>Bring your knowledge here</strong>
        <p>Drop a PDF or Word document</p>
        <label className={`file-button ${unavailable ? "disabled" : ""}`}>
          Choose document
          <input
            type="file"
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            aria-label="Choose document"
            disabled={unavailable}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void state.choose(file);
            }}
          />
        </label>
        <small>PDF or .docx · one file at a time</small>
      </div>
      <div role="status" className="upload-status">
        {state.transfer === "transferring" && (
          <>
            <LoaderCircle className="spinner" size={16} />
            Transferring {state.upload?.file.name} · waiting for acceptance
          </>
        )}
        {state.transfer === "accepted" &&
          "Upload accepted. Following ingestion status."}
      </div>
      {(state.error || refreshError) && (
        <div className="error" role="alert">
          {state.error || refreshError}
          <button
            onClick={() => {
              setRefreshError("");
              void state
                .refresh()
                .catch((error) => setRefreshError(errorMessage(error)));
            }}
          >
            Refresh document status
          </button>
          {state.upload &&
            recovery.upload &&
            state.transfer !== "transferring" && (
              <button onClick={() => void state.resumeUpload()}>
                Retry same upload
              </button>
            )}
        </div>
      )}
      <div className="documents-heading">
        <h3>Workspace sources</h3>
        <span>{state.documents.length}</span>
      </div>
      {state.loading && <p role="status">Loading your documents…</p>}
      {!state.loading && !state.documents.length && (
        <div className="documents-empty">
          <FileText size={24} />
          <p>
            No documents yet.
            <br />
            Add a source to start exploring.
          </p>
        </div>
      )}
      <ul className="document-list">
        {state.documents.map((document) => (
          <li
            key={document.document_id}
            className="document-card"
            aria-label={document.filename ?? "Document"}
          >
            <div className="document-name">
              <FileText size={19} />
              <h3>{document.filename ?? "Document"}</h3>
            </div>
            <div
              className={`document-stage ${document.status === "failed" ? "failed-label" : ""}`}
            >
              {isActive(document) ? (
                <LoaderCircle className="spinner" size={14} />
              ) : document.status === "ready" ? (
                <Check size={14} />
              ) : null}
              <span>{stageLabel(document)}</span>
            </div>
            {document.lifecycle === "live" &&
              document.total_chunks !== null && (
                <>
                  <div className="chunk-count">
                    <span>Committed chunks</span>
                    <strong>
                      {document.completed_chunks}/{document.total_chunks}
                    </strong>
                  </div>
                  <progress
                    aria-label={`Committed chunks for ${document.filename}`}
                    value={document.completed_chunks}
                    max={Math.max(1, document.total_chunks)}
                  />
                </>
              )}
            {document.lifecycle === "live" && document.status === "failed" && (
              <p className="document-error">
                {document.retry_available
                  ? "Ingestion stopped. Completed chunks are saved."
                  : "This document could not be indexed. Check that it contains readable text and upload a corrected file."}
              </p>
            )}
            {document.lifecycle === "deleting" && (
              <p className="document-error">
                {document.status === "failed"
                  ? "Cleanup was interrupted. Retry cleanup to finish removing the file."
                  : "The source is unavailable to new answers. Waiting for cleanup to finish."}
              </p>
            )}
            {document.retrying_chunks > 0 && document.lifecycle === "live" && (
              <small>{document.retrying_chunks} chunks retrying</small>
            )}
            <div className="document-actions">
              {document.lifecycle === "live" && document.retry_available && (
                <button
                  disabled={state.pending.has(document.document_id)}
                  onClick={() => void state.retry(document)}
                >
                  <RotateCcw size={14} />
                  Retry ingestion
                </button>
              )}
              {document.lifecycle === "live" && (
                <button
                  aria-label={`Delete ${document.filename}`}
                  disabled={state.pending.has(document.document_id)}
                  onClick={() => setConfirmation(document)}
                >
                  <Trash2 size={14} />
                  Delete
                </button>
              )}
              {document.lifecycle === "deleting" &&
                document.status === "failed" && (
                  <button
                    disabled={state.pending.has(document.document_id)}
                    onClick={() => void state.remove(document)}
                  >
                    <RotateCcw size={14} />
                    Retry cleanup
                  </button>
                )}
            </div>
          </li>
        ))}
      </ul>
      {confirmation && (
        <Dialog title="Delete document?" onClose={() => setConfirmation(null)}>
          <p>
            Delete <strong>{confirmation.filename}</strong>? This removes it
            from new answers and cancels any ingestion in progress.
          </p>
          <p className="muted">
            Saved answers remain readable. Their source references will be
            marked unavailable.
          </p>
          <div className="dialog-actions">
            <button onClick={() => setConfirmation(null)}>Keep document</button>
            <button
              className="danger"
              disabled={state.pending.has(confirmation.document_id)}
              onClick={() =>
                void state.remove(confirmation).then((accepted) => {
                  if (accepted) setConfirmation(null);
                })
              }
            >
              Delete document
            </button>
          </div>
          {state.error && <p role="alert">{state.error}</p>}
        </Dialog>
      )}
    </Dialog>
  );
}

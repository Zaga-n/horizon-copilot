import { useCallback, useEffect, useRef, useState } from "react";
import { Api, ApiError, errorMessage } from "../../shared/api";
import type {
  Acceptance,
  DocumentRecovery,
  DocumentStatus,
  UploadRequest,
} from "./models";
import { isActive, validateFile } from "./models";
export function useDocuments(api: Api, recovery: DocumentRecovery) {
  const [documents, setDocuments] = useState<DocumentStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(() =>
    recovery.upload
      ? "Your previous upload outcome is unknown. Check its status or retry the same upload."
      : "",
  );
  const [upload, setUpload] = useState<UploadRequest | null>(
    () => recovery.upload ?? null,
  );
  const [transfer, setTransfer] = useState<
    "idle" | "transferring" | "accepted" | "unknown" | "rejected"
  >(() => (recovery.upload ? "unknown" : "idle"));
  const [pending, setPending] = useState<Set<string>>(() => new Set());
  const activeRequests = useRef(new Set<string>());
  const transferActive = useRef(false);
  const failures = useRef(0);
  const refresh = useCallback(
    async (signal?: AbortSignal): Promise<void> => {
      let offset = 0;
      const all: DocumentStatus[] = [];
      for (;;) {
        const page = await api.json<DocumentStatus[]>(
          `/documents?limit=100&offset=${offset}`,
          { signal },
        );
        all.push(...page);
        if (page.length < 100) break;
        offset += page.length;
      }
      // A delete whose response was lost must keep reconciling through document status.
      const ids = all.map((item) => item.document_id);
      for (const id of recovery.deleting) {
        if (!ids.includes(id))
          all.push(
            await api.json<DocumentStatus>(`/documents/${id}`, { signal }),
          );
      }
      if (signal?.aborted) return;
      for (const item of all)
        if (item.lifecycle !== "live")
          recovery.deleting.delete(item.document_id);
      setDocuments(all.filter((item) => item.lifecycle !== "deleted"));
      setLoading(false);
      failures.current = 0;
    },
    [api, recovery],
  );
  const polling = documents.some(isActive) || recovery.deleting.size > 0;
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = (delay: number) => {
      clearTimeout(timer);
      timer = setTimeout(() => void poll(), delay);
    };
    const poll = async () => {
      if (document.hidden) return;
      try {
        await refresh(controller.signal);
        if (!recovery.upload) setError("");
      } catch (error) {
        if (!controller.signal.aborted) {
          failures.current++;
          setError(errorMessage(error));
          setLoading(false);
        }
      }
      if (!controller.signal.aborted && (polling || failures.current > 0))
        schedule(Math.min(30000, 2000 * 2 ** Math.min(failures.current, 4)));
    };
    const resume = () => {
      if (!document.hidden) schedule(0);
      else clearTimeout(timer);
    };
    document.addEventListener("visibilitychange", resume);
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [refresh, polling, recovery]);
  async function transmit(request: UploadRequest): Promise<void> {
    if (transferActive.current) return;
    transferActive.current = true;
    recovery.setUpload(request);
    setUpload(request);
    setTransfer("transferring");
    setError("");
    const form = new FormData();
    form.append("file", request.file);
    try {
      const accepted = await api.json<Acceptance>("/documents", {
        method: "POST",
        body: form,
        headers: { "Idempotency-Key": request.key },
      });
      request.documentId = accepted.document_id;
      recovery.clearUpload();
      setTransfer("accepted");
      // Fetch canonical server metadata rather than retaining the selected filename.
      const status = await api.json<DocumentStatus>(
        `/documents/${accepted.document_id}`,
      );
      setDocuments((previous) =>
        [
          status,
          ...previous.filter((item) => item.document_id !== status.document_id),
        ].filter((item) => item.lifecycle !== "deleted"),
      );
      setUpload(null);
    } catch (error) {
      if (request.documentId) {
        setTransfer("accepted");
        setUpload(null);
        setError(
          "The upload was accepted. Refresh document status to follow ingestion.",
        );
      } else if (
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500 &&
        ![401, 408, 429].includes(error.status)
      ) {
        recovery.clearUpload();
        setTransfer("rejected");
        setError(errorMessage(error));
      } else {
        setTransfer("unknown");
        setError(
          "Upload acknowledgment is unknown. Check your documents or retry this same upload safely.",
        );
      }
    } finally {
      transferActive.current = false;
    }
  }
  async function choose(file: File): Promise<void> {
    if (recovery.upload || transferActive.current) return;
    const invalid = validateFile(file);
    if (invalid) {
      setError(invalid);
      return;
    }
    await transmit({ key: crypto.randomUUID(), file });
  }
  async function retry(document: DocumentStatus): Promise<void> {
    if (
      !document.retry_available ||
      document.lifecycle !== "live" ||
      activeRequests.current.has(document.document_id)
    )
      return;
    activeRequests.current.add(document.document_id);
    setPending(new Set(activeRequests.current));
    setError("");
    try {
      const status = await api.json<DocumentStatus>(
        `/ingestion-jobs/${document.job_id}:retry`,
        { method: "POST" },
      );
      setDocuments((previous) =>
        previous.map((item) =>
          item.document_id === status.document_id ? status : item,
        ),
      );
    } catch (error) {
      setError(errorMessage(error));
      await refresh().catch(() => undefined);
    } finally {
      activeRequests.current.delete(document.document_id);
      setPending(new Set(activeRequests.current));
    }
  }
  async function remove(document: DocumentStatus): Promise<boolean> {
    if (activeRequests.current.has(document.document_id)) return false;
    activeRequests.current.add(document.document_id);
    setPending(new Set(activeRequests.current));
    recovery.deleting.add(document.document_id);
    setError("");
    try {
      const result = await api.json<{ lifecycle: DocumentStatus["lifecycle"] }>(
        `/documents/${document.document_id}`,
        { method: "DELETE" },
      );
      setDocuments((previous) =>
        result.lifecycle === "deleted"
          ? previous.filter((item) => item.document_id !== document.document_id)
          : previous.map((item) =>
              item.document_id === document.document_id
                ? {
                    ...item,
                    lifecycle: "deleting",
                    status: "queued",
                    stage: "cleanup",
                    retry_available: false,
                  }
                : item,
            ),
      );
      if (result.lifecycle !== "live")
        recovery.deleting.delete(document.document_id);
      return true;
    } catch (error) {
      setError(
        "Delete acknowledgment is unknown. Refresh status before trying again.",
      );
      await refresh().catch(() => setError(errorMessage(error)));
      const accepted = !recovery.deleting.has(document.document_id);
      if (accepted) setError("");
      return accepted;
    } finally {
      activeRequests.current.delete(document.document_id);
      setPending(new Set(activeRequests.current));
    }
  }
  return {
    documents,
    loading,
    error,
    upload,
    transfer,
    pending,
    choose,
    retry,
    remove,
    refresh,
    resumeUpload: () =>
      recovery.upload ? transmit(recovery.upload) : Promise.resolve(),
  };
}

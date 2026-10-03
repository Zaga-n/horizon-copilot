export interface DocumentStatus {
  document_id: string;
  filename: string | null;
  version_id: string | null;
  job_id: string;
  status:
    "queued" | "processing" | "retrying" | "ready" | "failed" | "cancelled";
  stage: "extraction" | "embedding" | "publication" | "cleanup" | "done";
  lifecycle: "live" | "deleting" | "deleted";
  total_chunks: number | null;
  completed_chunks: number;
  retrying_chunks: number;
  failed_chunks: number;
  attempts: number;
  retry_cycle: number;
  error_category: string | null;
  retry_available: boolean;
}
export interface Acceptance {
  document_id: string;
  version_id: string;
  job_id: string;
  status: DocumentStatus["status"];
  deduplicated: boolean;
}
export interface UploadRequest {
  key: string;
  file: File;
  documentId?: string;
}
export class DocumentRecovery {
  private request?: UploadRequest;
  readonly deleting = new Set<string>();
  get upload(): UploadRequest | undefined {
    return this.request;
  }
  setUpload(request: UploadRequest): void {
    this.request = request;
  }
  clearUpload(): void {
    this.request = undefined;
  }
  clear(): void {
    this.clearUpload();
    this.deleting.clear();
  }
}
export function isActive(document: DocumentStatus): boolean {
  return (
    document.lifecycle === "deleting" ||
    (document.lifecycle === "live" &&
      ["queued", "processing", "retrying"].includes(document.status))
  );
}
export function stageLabel(document: DocumentStatus): string {
  if (document.lifecycle === "deleting")
    return document.status === "failed"
      ? "Deleting · cleanup interrupted"
      : "Deleting";
  if (document.lifecycle === "deleted") return "Deleted";
  if (document.status === "ready") return "Ready";
  if (document.status === "failed") return "Ingestion failed";
  if (document.status === "cancelled") return "Cancelled";
  if (document.status === "queued") return "Queued";
  return {
    extraction: "Extracting text",
    embedding: "Indexing",
    publication: "Publishing",
    cleanup: "Cleaning up",
    done: "Finishing",
  }[document.stage];
}
export function validateFile(file: File): string | null {
  if (!/\.(pdf|docx)$/i.test(file.name))
    return "Choose a PDF or .docx document.";
  if (!file.size) return "This file is empty. Choose another document.";
  // The server owns byte limits and actual format validation.
  return null;
}

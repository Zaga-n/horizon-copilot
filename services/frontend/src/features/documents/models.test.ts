import { expect, it } from "vitest";
import { stageLabel, isActive, validateFile } from "./models";
import type { DocumentStatus } from "./models";
const status: DocumentStatus = {
  document_id: "doc",
  filename: "Guide.pdf",
  version_id: "version",
  job_id: "job",
  status: "processing",
  stage: "extraction",
  lifecycle: "live",
  total_chunks: null,
  completed_chunks: 0,
  retrying_chunks: 0,
  failed_chunks: 0,
  attempts: 1,
  retry_cycle: 0,
  error_category: null,
  retry_available: false,
};
it("keeps publishing and deletion active even with completed chunk totals", () => {
  expect(
    stageLabel({
      ...status,
      stage: "publication",
      completed_chunks: 155,
      total_chunks: 155,
    }),
  ).toBe("Publishing");
  expect(isActive({ ...status, lifecycle: "deleting", status: "failed" })).toBe(
    true,
  );
  expect(
    stageLabel({ ...status, lifecycle: "deleting", status: "ready" }),
  ).toBe("Deleting");
  expect(isActive({ ...status, status: "ready" })).toBe(false);
});
it("rejects empty and unsupported files while leaving size and content validation to the server", () => {
  expect(validateFile(new File(["bytes"], "guide.DOCX"))).toBeNull();
  expect(validateFile(new File(["bytes"], "guide.doc"))).toContain(
    "PDF or .docx",
  );
  expect(validateFile(new File([], "empty.pdf"))).toContain("empty");
});

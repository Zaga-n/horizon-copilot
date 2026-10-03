import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { setup } from "./fixtures";
import type { DocumentStatus } from "../src/features/documents/models";
function documentStatus(
  overrides: Partial<DocumentStatus> = {},
): DocumentStatus {
  return {
    document_id: "document",
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
    ...overrides,
  };
}
async function documents(
  page: Page,
  initial: DocumentStatus[] = [],
  lostUpload = false,
) {
  await setup(page);
  const state = {
    documents: initial,
    keys: [] as string[],
    uploads: 0,
    deletes: 0,
    retries: 0,
    rejected: false,
    reads: 0,
    lostDelete: false,
    expiredUpload: false,
  };
  await page.route("http://localhost:8081/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    expect(request.headers().authorization).toContain("Bearer header.");
    if (request.method() === "POST" && path === "/v1/documents") {
      state.uploads++;
      state.keys.push(request.headers()["idempotency-key"]);
      expect(request.postDataBuffer()?.toString()).toContain('name="file"');
      if (state.rejected)
        return route.fulfill({
          status: 422,
          contentType: "application/json",
          body: "{}",
        });
      if (!state.documents.length) state.documents.push(documentStatus());
      if (state.expiredUpload && state.uploads === 1)
        return route.fulfill({
          status: 401,
          contentType: "application/json",
          body: "{}",
        });
      if (lostUpload && state.uploads === 1) return route.abort("failed");
      return route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          document_id: "document",
          version_id: "version",
          job_id: "job",
          status: "queued",
          deduplicated: state.uploads > 1,
        }),
      });
    }
    if (request.method() === "DELETE") {
      state.deletes++;
      const item = state.documents.find((item) =>
        path.endsWith(item.document_id),
      )!;
      Object.assign(item, {
        lifecycle: "deleting",
        status: "queued",
        stage: "cleanup",
        retry_available: false,
      });
      if (state.lostDelete) {
        state.lostDelete = false;
        return route.abort("failed");
      }
      return route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          document_id: item.document_id,
          lifecycle: "deleting",
          status_url: path,
        }),
      });
    }
    if (path.endsWith(":retry")) {
      state.retries++;
      const item = state.documents.find((item) => path.includes(item.job_id))!;
      Object.assign(item, {
        status: "retrying",
        retry_available: false,
        retry_cycle: item.retry_cycle + 1,
      });
      return route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify(item),
      });
    }
    state.reads++;
    const value =
      path === "/v1/documents"
        ? state.documents
        : state.documents.find(
            (item) =>
              path.endsWith(item.document_id) || path.endsWith(item.job_id),
          );
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(value),
    });
  });
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  return state;
}
async function selectFile(page: Page, name = "Guide.pdf"): Promise<void> {
  await page.getByLabel("Choose document").setInputFiles({
    name,
    mimeType: name.endsWith(".pdf")
      ? "application/pdf"
      : "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    buffer: Buffer.from("Content for validation on the server"),
  });
}
test("upload follows unknown totals, committed fractions, publication, and Ready", async ({
  page,
}) => {
  const state = await documents(page);
  await selectFile(page);
  const card = page.getByRole("listitem", { name: "Guide.pdf" });
  await expect(card.getByText("Extracting text")).toBeVisible();
  await expect(card.getByRole("progressbar")).toHaveCount(0);
  Object.assign(state.documents[0], {
    stage: "embedding",
    total_chunks: 155,
    completed_chunks: 3,
  });
  await expect(card.getByText("3/155")).toBeVisible();
  Object.assign(state.documents[0], {
    stage: "publication",
    completed_chunks: 155,
  });
  await expect(card.getByText("Publishing", { exact: true })).toBeVisible();
  await expect(card.getByText("155/155")).toBeVisible();
  await expect(card.getByText("Ready", { exact: true })).toHaveCount(0);
  Object.assign(state.documents[0], { status: "ready", stage: "done" });
  await expect(card.getByText("Ready", { exact: true })).toBeVisible();
  expect(state.uploads).toBe(1);
});
test("lost upload acknowledgment reuses the same key and canonical filename", async ({
  page,
}) => {
  const state = await documents(page, [], true);
  await selectFile(page, "Renamed.pdf");
  await expect(
    page.getByRole("button", { name: "Retry same upload" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retry same upload" }).click();
  await expect(page.getByRole("listitem", { name: "Guide.pdf" })).toBeVisible();
  expect(state.keys).toHaveLength(2);
  expect(state.keys[0]).toBe(state.keys[1]);
  await expect(page.getByRole("listitem", { name: "Renamed.pdf" })).toHaveCount(
    0,
  );
});
test("valid Word files, rejected inputs, and keyboard upload errors remain actionable", async ({
  page,
}) => {
  const state = await documents(page);
  await selectFile(page, "Legacy.doc");
  await expect(page.getByRole("alert")).toContainText("PDF or .docx");
  expect(state.uploads).toBe(0);
  state.rejected = true;
  await selectFile(page, "Broken.pdf");
  await expect(page.getByRole("alert")).toContainText("server rejected");
  state.rejected = false;
  await selectFile(page, "Notes.docx");
  await expect(page.getByRole("listitem", { name: "Guide.pdf" })).toBeVisible();
});
test("retry preserves 107/111 chunks and targets the existing job", async ({
  page,
}) => {
  const state = await documents(page, [
    documentStatus({
      status: "failed",
      stage: "embedding",
      total_chunks: 111,
      completed_chunks: 107,
      failed_chunks: 4,
      retry_available: true,
    }),
  ]);
  await expect(page.getByText("107/111")).toBeVisible();
  await page.getByRole("button", { name: "Retry ingestion" }).click();
  await expect(
    page.getByRole("button", { name: "Retry ingestion" }),
  ).toHaveCount(0);
  await expect(page.getByText("107/111")).toBeVisible();
  expect(state.retries).toBe(1);
  expect(state.documents).toHaveLength(1);
});
test("named deletion stays Deleting through delayed cleanup and removes only Deleted", async ({
  page,
}) => {
  const state = await documents(page, [
    documentStatus({
      status: "ready",
      stage: "done",
      total_chunks: 155,
      completed_chunks: 155,
    }),
  ]);
  await page
    .getByRole("button", { name: "Delete Guide.pdf", exact: true })
    .click();
  const confirmation = page.getByRole("dialog", { name: "Delete document?" });
  await expect(
    confirmation.getByText("Guide.pdf", { exact: true }),
  ).toBeVisible();
  await expect(
    confirmation.getByRole("button", { name: "Close dialog", exact: true }),
  ).toBeFocused();
  await page
    .getByRole("button", { name: "Delete document", exact: true })
    .click();
  await expect(confirmation).toHaveCount(0);
  const card = page.getByRole("listitem", { name: "Guide.pdf" });
  await expect(card.getByText("Deleting", { exact: true })).toBeVisible();
  await expect(
    card.getByRole("button", { name: "Retry ingestion" }),
  ).toHaveCount(0);
  Object.assign(state.documents[0], {
    status: "retrying",
    error_category: "storage_unavailable",
  });
  await expect(
    card.getByText("Waiting for cleanup to finish.", { exact: false }),
  ).toBeVisible();
  await expect(card.getByText("Ready", { exact: true })).toHaveCount(0);
  Object.assign(state.documents[0], {
    lifecycle: "deleted",
    status: "ready",
    stage: "done",
    filename: null,
  });
  await expect(card).toHaveCount(0);
  expect(state.deletes).toBe(1);
});
test("processing documents can be deleted and failed cleanup can explicitly recover", async ({
  page,
}) => {
  const state = await documents(page, [documentStatus()]);
  await page
    .getByRole("button", { name: "Delete Guide.pdf", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Delete document", exact: true })
    .click();
  Object.assign(state.documents[0], {
    status: "failed",
    lifecycle: "deleting",
  });
  await expect(
    page.getByRole("button", { name: "Retry cleanup" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retry cleanup" }).click();
  await expect(page.getByText("Deleting", { exact: true })).toBeVisible();
  expect(state.deletes).toBe(2);
  expect(state.retries).toBe(0);
});
test("reload restores polling and filename from server and mobile dialogs pass accessibility", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const state = await documents(page, [
    documentStatus({
      stage: "embedding",
      total_chunks: 155,
      completed_chunks: 3,
    }),
  ]);
  await page.reload();
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  await expect(page.getByText("3/155")).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await expect(page.locator(".spinner").first()).toHaveCSS(
    "animation-name",
    "none",
  );
  Object.assign(state.documents[0], { completed_chunks: 4 });
  await expect(page.getByText("4/155")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Documents", exact: true }),
  ).toBeFocused();
});
test("drag and drop accepts one supported document", async ({ page }) => {
  const state = await documents(page);
  await page.locator(".upload-zone").evaluate((element) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(
      new File(["PDF content"], "Dropped.pdf", { type: "application/pdf" }),
    );
    element.dispatchEvent(
      new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer }),
    );
  });
  await expect(page.getByRole("listitem", { name: "Guide.pdf" })).toBeVisible();
  expect(state.uploads).toBe(1);
});
test("lost delete acknowledgment reconciles the accepted lifecycle without another delete", async ({
  page,
}) => {
  const state = await documents(page, [
    documentStatus({ status: "ready", stage: "done" }),
  ]);
  state.lostDelete = true;
  await page
    .getByRole("button", { name: "Delete Guide.pdf", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Delete document", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Delete document?" }),
  ).toHaveCount(0);
  await expect(page.getByText("Deleting", { exact: true })).toBeVisible();
  expect(state.deletes).toBe(1);
});
test("expiry retains same-owner upload recovery without automatically replaying acceptance", async ({
  page,
}) => {
  const state = await documents(page);
  state.expiredUpload = true;
  await selectFile(page);
  await expect(
    page.getByRole("button", { name: "Sign in with Google" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Retry same upload" }),
  ).toBeVisible();
  expect(state.uploads).toBe(1);
  await page.getByRole("button", { name: "Retry same upload" }).click();
  await expect(
    page.getByRole("button", { name: "Retry same upload" }),
  ).toHaveCount(0);
  expect(state.keys[0]).toBe(state.keys[1]);
});
test("another account cannot inherit an expired owner's document or pending upload", async ({
  page,
}) => {
  const state = await documents(page);
  state.expiredUpload = true;
  await selectFile(page, "Private.pdf");
  await expect(
    page.getByRole("button", { name: "Sign in with Google" }),
  ).toBeVisible();
  state.documents = [];
  state.expiredUpload = false;
  await page.evaluate(() => Reflect.set(window, "__owner", "owner-b"));
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  await expect(
    page.getByText("No documents yet.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Retry same upload" }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Choose document")).toBeEnabled();
  await selectFile(page, "Other.pdf");
  await expect(page.getByRole("listitem", { name: "Guide.pdf" })).toBeVisible();
  expect(state.keys).toHaveLength(2);
  expect(state.keys[1]).not.toBe(state.keys[0]);
});
test("polling pauses when hidden, resumes on visibility, and stops at Ready", async ({
  page,
}) => {
  await page.clock.install();
  const state = await documents(page, [documentStatus()]);
  await expect(page.getByText("Extracting text")).toBeVisible();
  await page.evaluate(() => {
    Object.assign(window, { __hidden: true });
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => Reflect.get(window, "__hidden"),
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const hiddenReads = state.reads;
  await page.clock.runFor(10000);
  expect(state.reads).toBe(hiddenReads);
  await page.evaluate(() => {
    Reflect.set(window, "__hidden", false);
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.runFor(1);
  await expect.poll(() => state.reads).toBeGreaterThan(hiddenReads);
  Object.assign(state.documents[0], { stage: "done", status: "ready" });
  await page.clock.runFor(2000);
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
  // Changing the lifecycle subscription performs one final reconciliation fetch.
  await page.clock.runFor(1);
  await page.getByRole("button", { name: "Close dialog", exact: true }).click();
  const settledReads = state.reads;
  await page.clock.runFor(30000);
  expect(state.reads).toBe(settledReads);
});

import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import { setup, TEST_ORIGIN } from "./fixtures";
import AxeBuilder from "@axe-core/playwright";
async function ask(page: Page): Promise<void> {
  await page.getByLabel("Ask Horizon").fill("What does the guide say?");
  await page.getByRole("button", { name: "Send question" }).click();
}
test("latest content stays hidden at the end even after an upward gesture", async ({
  page,
}) => {
  await setup(page);
  await ask(page);
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toBeVisible();
  const transcript = page.locator(".transcript");
  await transcript.evaluate((element) => {
    element.dispatchEvent(
      new WheelEvent("wheel", { deltaY: -40, bubbles: true }),
    );
  });
  await expect(
    page.getByRole("button", { name: "Latest content" }),
  ).toHaveCount(0);
});
test("saved answer feedback and text-only conversation feedback use separate original targets", async ({
  page,
}) => {
  const state = await setup(page);
  await ask(page);
  await expect(
    page.getByRole("button", { name: "Dislike answer" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Dislike answer" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "Dismiss" }).click();
  await expect(
    page.getByRole("button", { name: "Dislike answer" }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(state.feedbackWrites[0]).toEqual({
    path: "/v1/messages/answer-1/feedback",
    body: { rating: "dislike", comment: null },
  });
  await page
    .getByRole("button", { name: "Conversation feedback", exact: true })
    .click();
  await page.getByLabel("Your comment (optional)").fill("Useful conversation");
  await page.getByRole("button", { name: "Save comment" }).click();
  await expect(
    page.getByRole("button", { name: "Conversation feedback · Saved" }),
  ).toBeVisible();
  expect(state.feedbackWrites.at(-1)).toEqual({
    path: "/v1/conversations/conversation/feedback",
    body: { rating: null, comment: "Useful conversation" },
  });
  await page.reload();
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await expect(
    page.getByRole("button", { name: /What does the guide say\?/ }),
  ).toBeVisible();
  await page.getByRole("button", { name: /What does the guide say\?/ }).click();
  await expect(
    page.getByRole("heading", { name: "What does the guide say?" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Dislike answer" }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("button", { name: "Conversation feedback · Saved" }),
  ).toBeVisible();
  await page.getByText("Sources · 1").click();
  await expect(page.getByText(/Source unavailable/)).toBeVisible();
  expect(state.requests()).toBe(1);
});
test("failed attempt retries the original turn without another user message", async ({
  page,
}) => {
  const state = await setup(page, { failed: true });
  await ask(page);
  await expect(page.getByText("Partial answer")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Retry answer" }).click();
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator(".transcript")
      .getByText("What does the guide say?", { exact: true }),
  ).toHaveCount(1);
  await expect(page.getByText("Partial answer", { exact: true })).toHaveCount(
    1,
  );
  expect(
    state.messages.filter((message) => message.role === "user"),
  ).toHaveLength(1);
  expect(state.requests()).toBe(2);
});
test("lost completion reconciles history without invoking another run", async ({
  page,
}) => {
  const state = await setup(page, { lost: true });
  await ask(page);
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toBeVisible();
  expect(state.requests()).toBe(1);
  await expect(
    page
      .locator(".transcript")
      .getByText("What does the guide say?", { exact: true }),
  ).toHaveCount(1);
});
test("sign-out aborts and removes all owner state before another account enters", async ({
  page,
}) => {
  await setup(page);
  await ask(page);
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(
    page
      .locator(".transcript")
      .getByText("What does the guide say?", { exact: true }),
  ).toHaveCount(0);
  await page.evaluate(() => {
    Object.assign(window, { __owner: "owner-b" });
  });
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await expect(page.getByLabel("Ask Horizon")).toHaveValue("");
  await expect(
    page.getByText("A grounded answer", { exact: false }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
});
test("denied account receives an actionable error", async ({ page }) => {
  await setup(page, { denied: true });
  await expect(page.getByRole("alert")).toContainText("does not have access");
});
test("mobile workspace fits, passes axe, and dialog restores focus", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Open navigation" }),
  ).toBeFocused();
});
test("feedback failure retains draft, dismiss preserves dislike, and clear targets the answer", async ({
  page,
}) => {
  const state = await setup(page);
  await ask(page);
  await page.getByRole("button", { name: "Dislike answer" }).click();
  await page
    .getByLabel("What could be better? (optional)")
    .fill("Please add detail");
  await page.route(
    "http://localhost:8080/v1/messages/answer-1/feedback",
    (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: "{}",
      }),
    { times: 1 },
  );
  await page.getByRole("button", { name: "Save comment" }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("What could be better? (optional)")).toHaveValue(
    "Please add detail",
  );
  await page.getByRole("button", { name: "Dismiss" }).click();
  await page.getByRole("button", { name: "Edit comment" }).click();
  await expect(page.getByLabel("What could be better? (optional)")).toHaveValue(
    "Please add detail",
  );
  await page.getByRole("button", { name: "Save comment" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.feedbackWrites.at(-1)?.body).toEqual({
    rating: "dislike",
    comment: "Please add detail",
  });
  await page.getByRole("button", { name: "Like answer", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(state.feedbackWrites.at(-1)?.body).toEqual({
    rating: "like",
    comment: null,
  });
  await page.getByRole("button", { name: "Clear feedback" }).click();
  await expect(
    page.getByRole("button", { name: "Clear feedback" }),
  ).toHaveCount(0);
});
test("conversation feedback supports edit and deletion independently", async ({
  page,
}) => {
  const state = await setup(page);
  await ask(page);
  await page
    .getByRole("button", { name: "Conversation feedback", exact: true })
    .click();
  await page.getByLabel("Your comment (optional)").fill("First");
  await page.getByRole("button", { name: "Save comment" }).click();
  await page
    .getByRole("button", { name: "Conversation feedback · Saved" })
    .click();
  await page.getByLabel("Your comment (optional)").fill("Edited");
  await page.getByRole("button", { name: "Dislike", exact: true }).click();
  await page.getByRole("button", { name: "Save comment" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.feedbackWrites.at(-1)?.body).toEqual({
    rating: "dislike",
    comment: "Edited",
  });
  await page
    .getByRole("button", { name: "Conversation feedback · Saved" })
    .click();
  await page.getByRole("button", { name: "Delete feedback" }).click();
  await expect(
    page.getByRole("button", { name: "Conversation feedback", exact: true }),
  ).toBeVisible();
  expect(state.feedbackWrites.at(-1)).toEqual({
    path: "/v1/conversations/conversation/feedback",
    body: null,
  });
});
test("markdown never runs HTML or javascript links", async ({ page }) => {
  const state = await setup(page);
  await ask(page);
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toBeVisible();
  state.messages.at(-1)!.content =
    "Safe <script>window.compromised=true</script> [unsafe](javascript:alert(1)) ![tracking](https://tracker.example/pixel)";
  await page.getByRole("button", { name: "New conversation" }).click();
  await page.getByRole("button", { name: /What does the guide say\?/ }).click();
  await expect(page.getByText("Safe", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => "compromised" in window)).toBe(false);
  await expect(page.locator(".message img")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "unsafe" })).toHaveAttribute(
    "href",
    "",
  );
});
test("renders real incremental deltas, blocks overlapping turns, and preserves readers scroll", async ({
  page,
}) => {
  const { createServer } = await import("node:http");
  const state = await setup(page);
  let release: (() => void) | undefined;
  let finish: (() => void) | undefined;
  const server = createServer((request, response) => {
    response.setHeader("Access-Control-Allow-Origin", TEST_ORIGIN);
    response.setHeader(
      "Access-Control-Allow-Headers",
      "Authorization,Content-Type,Idempotency-Key",
    );
    response.setHeader("Access-Control-Allow-Methods", "POST,OPTIONS");
    if (request.method === "OPTIONS") {
      response.writeHead(204);
      response.end();
      return;
    }
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    const emit = (type: string, sequence: number, data: unknown) =>
      response.write(
        `event: ${type}\ndata: ${JSON.stringify({ type, sequence, data, conversation_id: "conversation", turn_id: "turn", run_id: "run-1", assistant_message_id: "answer-1", attempt_number: 1 })}\n\n`,
      );
    emit("started", 1, {});
    const longText = Array.from(
      { length: 100 },
      (_, index) => `Paragraph ${index}. A long answer to explore.\n\n`,
    ).join("");
    emit("progress", 2, {
      phase: "retrieval",
      message: "Checking saved work…",
    });
    emit("delta", 3, { text: longText });
    finish = () => {
      emit("completed", 5, {});
      response.end();
    };
    release = () => {
      emit("delta", 4, { text: "Last visible delta." });
      state.messages.push(
        {
          id: "user-message",
          conversation_id: "conversation",
          turn_id: "turn",
          run_id: null,
          role: "user",
          status: "completed",
          content: "What does the guide say?",
          sources: [],
          attempt_number: 1,
          message_order: 1,
          feedback: null,
        },
        {
          id: "answer-1",
          conversation_id: "conversation",
          turn_id: "turn",
          run_id: "run-1",
          role: "assistant",
          status: "completed",
          content: longText + "Last visible delta.",
          sources: [],
          attempt_number: 1,
          message_order: 2,
          feedback: null,
        },
      );
      // Keep the stream active while the test verifies scroll and incremental text.
    };
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string")
    throw new Error("No test server address");
  try {
    await page.route(
      "http://localhost:8080/v1/conversations/conversation/turns:stream",
      (route) =>
        route.continue({ url: `http://127.0.0.1:${address.port}/stream` }),
    );
    await ask(page);
    await expect(
      page.getByText("Paragraph 0. A long answer to explore.", { exact: true }),
    ).toBeVisible();
    await page.getByLabel("Ask Horizon").fill("A second question");
    await expect(
      page.getByRole("button", { name: "Send question" }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "Like answer", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.locator(".message.assistant").getByRole("status"),
    ).toHaveText("Checking saved work…");
    await expect(
      page.locator(".composer-area").getByRole("status"),
    ).toHaveCount(0);
    const transcript = page.locator(".transcript");
    await expect
      .poll(() =>
        transcript.evaluate(
          (element) =>
            element.scrollHeight - element.scrollTop - element.clientHeight,
        ),
      )
      .toBeLessThan(4);
    await transcript.hover();
    await page.mouse.wheel(0, -40);
    await expect
      .poll(() =>
        transcript.evaluate(
          (element) =>
            element.scrollHeight - element.scrollTop - element.clientHeight,
        ),
      )
      .toBeGreaterThan(4);
    const readerPosition = await transcript.evaluate(
      (element) => element.scrollTop,
    );
    expect(
      await transcript.evaluate(
        (element) =>
          element.scrollHeight - element.scrollTop - element.clientHeight,
      ),
    ).toBeLessThan(80);
    await expect(
      page.getByRole("button", { name: "Latest content" }),
    ).toBeVisible();
    await page
      .locator(".message.assistant")
      .evaluate((element) =>
        Object.assign(window, { __streamBubble: element }),
      );
    release!();
    await expect(
      page.getByText("Last visible delta.", { exact: true }),
    ).toBeAttached();
    expect(
      await page
        .locator(".transcript")
        .evaluate((element) => element.scrollTop),
    ).toBe(readerPosition);
    await page.getByRole("button", { name: "Latest content" }).click();
    await expect(
      page.getByRole("button", { name: "Latest content" }),
    ).toHaveCount(0);
    finish!();
    await expect(
      page.getByRole("button", { name: "Like answer", exact: true }),
    ).toBeVisible();
    expect(
      await page
        .locator(".message.assistant")
        .evaluate(
          (element) => element === Reflect.get(window, "__streamBubble"),
        ),
    ).toBe(true);
    // A resize can reveal the end without a downward scroll gesture.
    await transcript.hover();
    await page.mouse.wheel(0, -100);
    await expect(
      page.getByRole("button", { name: "Latest content" }),
    ).toBeVisible();
    await page.setViewportSize({ width: 1280, height: 10000 });
    await expect(
      page.getByRole("button", { name: "Latest content" }),
    ).toHaveCount(0);
  } finally {
    await page.close();
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
test("cursor-paginated history restores all ordered server messages", async ({
  page,
}) => {
  const state = await setup(page);
  await ask(page);
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toBeVisible();
  for (let index = 3; index <= 132; index++)
    state.messages.push({
      ...state.messages[0],
      id: `history-${index}`,
      message_order: index,
      content: `Saved history message ${index}`,
    });
  const cursors: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/messages?"))
      cursors.push(new URL(request.url()).searchParams.get("cursor")!);
  });
  await page.getByRole("button", { name: "New conversation" }).click();
  await page.getByRole("button", { name: /What does the guide say\?/ }).click();
  await expect(
    page.getByText("Saved history message 132", { exact: true }),
  ).toBeVisible();
  expect(cursors).toContain("100");
  const rendered = await page.locator(".message").allTextContents();
  expect(rendered).toHaveLength(132);
  expect(rendered[2]).toContain("Saved history message 3");
  expect(rendered[131]).toContain("Saved history message 132");
});
test("unknown acceptance checks the original key and never creates a duplicate user message", async ({
  page,
}) => {
  const state = await setup(page);
  const keys: string[] = [];
  await page.route(
    "http://localhost:8080/v1/conversations/conversation/turns:stream",
    async (route) => {
      keys.push(route.request().headers()["idempotency-key"]);
      if (keys.length === 1) {
        state.messages.push(
          {
            id: "user-message",
            conversation_id: "conversation",
            turn_id: "turn",
            run_id: null,
            role: "user",
            status: "completed",
            content: "What does the guide say?",
            sources: [],
            attempt_number: 1,
            message_order: 1,
            feedback: null,
          },
          {
            id: "answer-1",
            conversation_id: "conversation",
            turn_id: "turn",
            run_id: "run-1",
            role: "assistant",
            status: "completed",
            content: "A saved answer",
            sources: [],
            attempt_number: 1,
            message_order: 2,
            feedback: null,
          },
        );
        return route.abort("failed");
      }
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          id: "turn",
          status: "completed",
          retry_available: false,
          attempts: [],
        }),
      });
    },
  );
  await ask(page);
  await expect(
    page.getByRole("button", { name: "Check submission outcome" }),
  ).toBeEnabled();
  await page.getByLabel("Ask Horizon").fill("Another question");
  await expect(
    page.getByRole("button", { name: "Send question" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Check submission outcome" }).click();
  await expect(
    page.getByRole("button", { name: "Check submission outcome" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Send question" }),
  ).toBeEnabled();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  await expect(
    page
      .locator(".transcript")
      .getByText("What does the guide say?", { exact: true }),
  ).toHaveCount(1);
});
test("expired API identity requires sign-in and restores saved work without replay", async ({
  page,
}) => {
  const state = await setup(page);
  await ask(page);
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toBeVisible();
  await page.route(
    "http://localhost:8080/v1/conversations/conversation",
    (route) =>
      route.fulfill({
        status: 401,
        contentType: "application/json",
        body: "{}",
      }),
    { times: 1 },
  );
  await page.getByRole("button", { name: "New conversation" }).click();
  await page.getByRole("button", { name: /What does the guide say\?/ }).click();
  await expect(
    page.getByText("Your session expired. Sign in to recover your saved work."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await page.getByRole("button", { name: /What does the guide say\?/ }).click();
  await expect(
    page.getByRole("button", { name: "Like answer", exact: true }),
  ).toBeVisible();
  expect(state.requests()).toBe(1);
});

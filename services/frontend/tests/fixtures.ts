import { expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";
export const TEST_ORIGIN = `http://localhost:${process.env.FRONTEND_TEST_PORT || "3002"}`;
interface Feedback {
  rating: string | null;
  comment: string | null;
}
interface Message {
  id: string;
  conversation_id: string;
  turn_id: string;
  run_id: string | null;
  role: string;
  status: string;
  content: string;
  sources: unknown[];
  attempt_number: number;
  message_order: number;
  feedback: Feedback | null;
}
export async function setup(
  page: Page,
  options: { failed?: boolean; lost?: boolean; denied?: boolean } = {},
) {
  const messages: Message[] = [];
  let feedback: Feedback | null = null;
  let retry = false;
  let requests = 0;
  const feedbackWrites: { path: string; body: unknown }[] = [];
  const conversation = () => ({
    id: "conversation",
    title:
      messages.find((message) => message.role === "user")?.content ||
      "New conversation",
    created_at: "2026-10-02T12:00:00Z",
    updated_at: "2026-10-02T12:00:00Z",
    feedback,
  });
  await page.route("https://fonts.googleapis.com/**", (route) => route.abort());
  await page.route("**/config.js", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: `window.HORIZON_CONFIG={googleClientId:'public-client',chatApiUrl:'http://localhost:8080',ingestionApiUrl:'http://localhost:8081',localIdentity:false};`,
    }),
  );
  await page.route("https://accounts.google.com/gsi/client", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: `window.google={accounts:{id:{initialize(options){window.__credential=options.callback},renderButton(element){const button=document.createElement('button');button.textContent='Sign in with Google';button.onclick=()=>window.__credential({credential:'header.'+btoa(JSON.stringify({sub:window.__owner||'owner-a',exp:Math.floor(Date.now()/1000)+3600}))+'.signature'});element.append(button)},disableAutoSelect(){}}}};`,
    }),
  );
  const json = (route: Route, value: unknown, status = 200) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(value),
    });
  await page.route("http://localhost:8080/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (method === "OPTIONS")
      return route.fulfill({
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": TEST_ORIGIN,
          "Access-Control-Allow-Headers":
            "Authorization,Content-Type,Idempotency-Key",
          "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE",
        },
      });
    expect(request.headers().authorization).toContain("Bearer header.");
    if (options.denied) return json(route, {}, 403);
    if (path.endsWith("/feedback")) {
      const body =
        method === "DELETE" ? null : (request.postDataJSON() as Feedback);
      feedbackWrites.push({ path, body });
      if (path.includes("/messages/")) {
        messages.find((message) => path.includes(message.id))!.feedback = body;
      } else feedback = body;
      return route.fulfill({ status: 204 });
    }
    if (path.endsWith(":stream") || path.endsWith(":retry")) {
      requests++;
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      if (path.endsWith(":retry")) {
        expect(request.postDataJSON()).toEqual({ expected_run_id: "run-1" });
        retry = true;
      }
      const number = retry ? 2 : 1;
      const failure = !!options.failed && !retry;
      const text = failure
        ? "Partial answer"
        : "A grounded answer with **sources**.";
      if (!retry)
        messages.push({
          id: "user-message",
          conversation_id: "conversation",
          turn_id: "turn",
          run_id: null,
          role: "user",
          status: "completed",
          content: request.postDataJSON().content,
          sources: [],
          attempt_number: 1,
          message_order: 1,
          feedback: null,
        });
      messages.push({
        id: `answer-${number}`,
        conversation_id: "conversation",
        turn_id: "turn",
        run_id: `run-${number}`,
        role: "assistant",
        status: failure ? "failed" : "completed",
        content: text,
        sources: [
          {
            marker: "[1]",
            filename: "Guide.pdf",
            title: "Guide",
            document_id: "document",
            page: 2,
            section_heading: null,
            unavailable: true,
          },
        ],
        attempt_number: number,
        message_order: number + 1,
        feedback: null,
      });
      const events = [
        { type: "started", data: {} },
        {
          type: "progress",
          data: { phase: "retrieval", message: "Searching your documents…" },
        },
        { type: "delta", data: { text } },
        { type: "sources", data: { sources: messages.at(-1)!.sources } },
        ...(options.lost
          ? []
          : [
              {
                type: failure ? "failed" : "completed",
                data: failure
                  ? {
                      text: "Please check the saved turn.",
                      persistence_pending: false,
                      retry_available: true,
                    }
                  : {},
              },
            ]),
      ];
      return route.fulfill({
        contentType: "text/event-stream",
        body: events
          .map(
            (event, index) =>
              `event: ${event.type}\ndata: ${JSON.stringify({ ...event, event_id: `run-${number}:${index + 1}`, sequence: index + 1, conversation_id: "conversation", turn_id: "turn", run_id: `run-${number}`, assistant_message_id: `answer-${number}`, attempt_number: number })}\n\n`,
          )
          .join(""),
      });
    }
    if (path.endsWith("/messages")) {
      const remaining = messages.filter(
        (message) =>
          message.message_order > Number(url.searchParams.get("cursor") || 0),
      );
      const items = remaining.slice(0, 100);
      return json(route, {
        messages: items,
        next_cursor:
          remaining.length > 100 ? items.at(-1)!.message_order : null,
      });
    }
    if (path.includes("/turns/"))
      return json(route, {
        id: "turn",
        status: options.failed && !retry ? "failed" : "completed",
        retry_available: options.failed && !retry,
        attempts: [
          {
            id: retry ? "run-2" : "run-1",
            status: options.failed && !retry ? "failed" : "completed",
          },
        ],
      });
    if (path === "/v1/conversations")
      return json(
        route,
        method === "POST"
          ? conversation()
          : {
              conversations: messages.length ? [conversation()] : [],
              next_cursor: null,
            },
      );
    return json(route, conversation());
  });
  await page.route("http://localhost:8081/v1/**", (route) => {
    expect(route.request().headers().authorization).toContain("Bearer header.");
    return json(route, []);
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in with Google" }).click();
  await expect(
    page.getByRole("heading", { name: "Your workspace" }),
  ).toBeVisible();
  return { feedbackWrites, requests: () => requests, messages };
}

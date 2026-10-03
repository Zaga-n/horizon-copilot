import { describe, expect, it } from "vitest";
import { applyEvent, decodeSse, initialStream, parseEvent } from "./stream";
import type { StreamEvent } from "./stream";
function event(
  type: string,
  sequence: number,
  data: unknown = {},
): StreamEvent {
  return parseEvent(
    JSON.stringify({
      type,
      sequence,
      data,
      conversation_id: "conversation",
      turn_id: "turn",
      run_id: "run",
      assistant_message_id: "message",
      attempt_number: 1,
    }),
  );
}
function bytes(parts: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(part);
      controller.close();
    },
  });
}
describe("POST SSE decoding", () => {
  it("preserves UTF-8 and CRLF boundaries at every byte split, including multiple events", async () => {
    const expected = [
      event("started", 1),
      event("delta", 2, { text: "Clarity 🌍 Αθήνα" }),
      event("completed", 3),
    ];
    const source =
      ": heartbeat\r\n\r\n" +
      expected
        .map(
          (frame) =>
            `id: run:${frame.sequence}\r\nevent: ${frame.type}\r\ndata: ${JSON.stringify(frame)}\r\n\r\n`,
        )
        .join("");
    const encoded = new TextEncoder().encode(source);
    for (let split = 1; split < encoded.length; split++) {
      const actual: StreamEvent[] = [];
      await decodeSse(
        bytes([encoded.slice(0, split), encoded.slice(split)]),
        (frame) => actual.push(frame),
      );
      expect(actual).toEqual(expected);
    }
    const actual: StreamEvent[] = [];
    await decodeSse(
      bytes([...encoded].map((byte) => new Uint8Array([byte]))),
      (frame) => actual.push(frame),
    );
    expect(actual).toEqual(expected);
  });
  it("does not dispatch an unterminated EOF event", async () => {
    const actual: StreamEvent[] = [];
    await decodeSse(
      bytes([
        new TextEncoder().encode(
          `data: ${JSON.stringify(event("completed", 1))}\n`,
        ),
      ]),
      (frame) => actual.push(frame),
    );
    expect(actual).toEqual([]);
  });
  it("rejects invalid deltas and missing identities", () => {
    expect(() => event("delta", 2, { text: 42 })).toThrow();
    expect(() => parseEvent('{"type":"started","data":{}}')).toThrow();
  });
});
describe("attempt state", () => {
  it("ignores duplicates, rejects gaps and run changes, and settles only once", () => {
    const started = applyEvent(initialStream, event("started", 1));
    const delta = event("delta", 2, { text: "A" });
    const streaming = applyEvent(started, delta);
    expect(streaming.text).toBe("A");
    expect(applyEvent(streaming, delta)).toBe(streaming);
    expect(() =>
      applyEvent(started, event("delta", 3, { text: "B" })),
    ).toThrow();
    expect(() => applyEvent(started, { ...delta, run_id: "other" })).toThrow();
    const completed = applyEvent(streaming, event("completed", 3));
    expect(
      applyEvent(
        completed,
        event("failed", 4, {
          text: "failure",
          persistence_pending: false,
          retry_available: true,
        }),
      ),
    ).toBe(completed);
  });
  it("retains partial failed text and marks pending persistence", () => {
    const state = [
      event("started", 1),
      event("delta", 2, { text: "Partial" }),
      event("failed", 3, {
        text: "Failed",
        persistence_pending: true,
        retry_available: false,
      }),
    ].reduce(applyEvent, initialStream);
    expect(state).toMatchObject({
      phase: "failed",
      text: "Partial",
      persistencePending: true,
    });
  });
});

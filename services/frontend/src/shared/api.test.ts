import { afterEach, expect, it, vi } from "vitest";
import { Api } from "./api";
afterEach(() => vi.unstubAllGlobals());
it("sends only a bearer token, expires on 401, and aborts account-scoped requests", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 }));
  vi.stubGlobal("fetch", fetch);
  const expired = vi.fn();
  const api = new Api("https://api.example", "memory-token", expired);
  await expect(api.response("/conversations")).rejects.toMatchObject({
    status: 401,
  });
  expect(expired).toHaveBeenCalledOnce();
  const init = fetch.mock.calls[0][1] as RequestInit;
  expect(new Headers(init.headers).get("Authorization")).toBe(
    "Bearer memory-token",
  );
  expect(init.credentials).toBe("omit");
  api.dispose();
  expect(init.signal?.aborted).toBe(true);
});

import { expect, it } from "vitest";
import { parseIdentity } from "./identity";
it("rejects expired and malformed identity claims and preserves the owner subject", () => {
  const token = (claims: unknown) =>
    `header.${btoa(JSON.stringify(claims))}.signature`;
  expect(
    parseIdentity(
      token({ sub: "owner", exp: Math.floor(Date.now() / 1000) + 60 }),
    ).subject,
  ).toBe("owner");
  expect(() => parseIdentity(token({ sub: "owner", exp: 1 }))).toThrow();
  expect(() => parseIdentity(token({ exp: 9999999999 }))).toThrow();
});

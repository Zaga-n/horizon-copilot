export interface Identity {
  token: string;
  subject: string;
  expires: number;
}
export function parseIdentity(token: string): Identity {
  const part = token.split(".")[1];
  if (!part) throw new Error("Invalid sign-in response.");
  const value: unknown = JSON.parse(
    atob(part.replace(/-/g, "+").replace(/_/g, "/")),
  );
  if (
    !value ||
    typeof value !== "object" ||
    !("sub" in value) ||
    typeof value.sub !== "string" ||
    !("exp" in value) ||
    typeof value.exp !== "number" ||
    value.exp * 1000 <= Date.now()
  )
    throw new Error("Sign-in response has expired.");
  // Decoded claims only scope client state. The APIs verify signature and audience.
  return { token, subject: value.sub, expires: value.exp * 1000 };
}

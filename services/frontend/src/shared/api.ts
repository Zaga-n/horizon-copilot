export class ApiError extends Error {
  constructor(public readonly status: number) {
    super(
      status === 401
        ? "Your session has expired. Sign in to continue."
        : status === 403
          ? "This account does not have access."
          : status === 409
            ? "The request conflicts with saved work. Refresh its status before continuing."
            : status === 422 || status === 413
              ? "The server rejected this input. Check the file or text and try again."
              : "The service could not complete this request. Try again.",
    );
  }
}
export class Api {
  private readonly lifetime = new AbortController();
  constructor(
    private readonly base: string,
    private readonly token: string,
    private readonly expired: () => void,
  ) {}
  dispose(): void {
    this.lifetime.abort();
  }
  async response(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    if (typeof init.body === "string")
      headers.set("Content-Type", "application/json");
    const signal = init.signal
      ? AbortSignal.any([init.signal, this.lifetime.signal])
      : this.lifetime.signal;
    const response = await fetch(`${this.base.replace(/\/$/, "")}/v1${path}`, {
      ...init,
      headers,
      signal,
      cache: "no-store",
      credentials: "omit",
    });
    if (!response.ok) {
      if (response.status === 401) this.expired();
      throw new ApiError(response.status);
    }
    return response;
  }
  async json<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.response(path, init);
    return response.status === 204
      ? (undefined as T)
      : (response.json() as Promise<T>);
  }
}
export function errorMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : "Connection interrupted. Check the saved status before trying again.";
}

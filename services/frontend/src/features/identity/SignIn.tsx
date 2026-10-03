import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { parseIdentity } from "./identity";
import type { Identity } from "./identity";
import { Compass } from "lucide-react";
import type { PublicConfig } from "../../shared/config";
interface GoogleIdentity {
  initialize(options: {
    client_id: string;
    callback: (response: { credential: string }) => void;
    auto_select: boolean;
  }): void;
  renderButton(
    element: HTMLElement,
    options: { theme: string; size: string; width: number },
  ): void;
  disableAutoSelect(): void;
}
declare global {
  interface Window {
    google?: { accounts: { id: GoogleIdentity } };
  }
}
export function SignIn({
  config,
  onSignIn,
  notice,
}: {
  config: PublicConfig;
  onSignIn: (identity: Identity) => void;
  notice: string;
}): ReactNode {
  const button = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!config.googleClientId) return;
    let active = true;
    const render = () => {
      if (!active || !window.google || !button.current) return;
      window.google.accounts.id.initialize({
        client_id: config.googleClientId,
        auto_select: false,
        callback: ({ credential }) => {
          if (!active) return;
          try {
            onSignIn(parseIdentity(credential));
          } catch {
            setError("Sign-in failed. Please try again.");
          }
        },
      });
      window.google.accounts.id.renderButton(button.current, {
        theme: "outline",
        size: "large",
        width: 280,
      });
    };
    let script = document.querySelector<HTMLScriptElement>(
      "script[data-google-identity]",
    );
    if (window.google) render();
    else {
      if (!script) {
        script = document.createElement("script");
        script.src = "https://accounts.google.com/gsi/client";
        script.async = true;
        script.dataset.googleIdentity = "true";
        document.head.append(script);
      }
      script.addEventListener("load", render);
    }
    const failed = () =>
      setError(
        "Google sign-in could not load. Check your connection and reload.",
      );
    script?.addEventListener("error", failed);
    return () => {
      active = false;
      script?.removeEventListener("load", render);
      script?.removeEventListener("error", failed);
    };
  }, [config.googleClientId, onSignIn]);
  return (
    <main className="signin">
      <div className="signin-card">
        <div className="brand">
          <Compass size={32} /> Horizon
        </div>
        <p className="eyebrow">YOUR KNOWLEDGE, CONNECTED</p>
        <h1>
          A clearer view
          <br />
          of what you know.
        </h1>
        <p>
          Ask questions. Explore your documents.
          <br />
          Find answers with sources you can trust.
        </p>
        <div ref={button} />
        {!config.googleClientId && (
          <p>Configure the public Google client ID to sign in.</p>
        )}
        {config.localIdentity && (
          <button
            className="primary"
            onClick={() =>
              onSignIn({
                token: "",
                subject: "local-horizon-user",
                expires: Date.now() + 3600000,
              })
            }
          >
            Enter local workspace
          </button>
        )}
        {notice && <p role="status">{notice}</p>}
        {error && <p role="alert">{error}</p>}
        <small>Only your account can access your workspace.</small>
      </div>
    </main>
  );
}

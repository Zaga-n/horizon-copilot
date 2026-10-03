import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Api } from "../shared/api";
import { Documents } from "../features/documents/Documents";
import { DocumentRecovery } from "../features/documents/models";
import { readConfig } from "../shared/config";
import { SignIn } from "../features/identity/SignIn";
import type { Identity } from "../features/identity/identity";
import { Chat } from "../features/chat/Chat";
import type { Recovery } from "../features/chat/models";
import { FeedbackControl } from "../features/feedback/FeedbackControl";
const config = readConfig();
export function App(): ReactNode {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [notice, setNotice] = useState("");
  const [recovery] = useState<Recovery>(() => new Map());
  const [documentRecovery] = useState(() => new DocumentRecovery());
  const subject = useRef<string | null>(null);
  const signIn = useCallback(
    (next: Identity) => {
      if (subject.current !== next.subject) {
        recovery.clear();
        documentRecovery.clear();
      }
      subject.current = next.subject;
      setIdentity(next);
      setNotice("");
    },
    [recovery, documentRecovery],
  );
  const expire = useCallback(() => {
    setIdentity(null);
    setNotice("Your session expired. Sign in to recover your saved work.");
  }, []);
  const signOut = useCallback(() => {
    window.google?.accounts.id.disableAutoSelect();
    recovery.clear();
    documentRecovery.clear();
    subject.current = null;
    setIdentity(null);
    setNotice("");
  }, [recovery, documentRecovery]);
  useEffect(() => {
    if (!identity) return;
    const timer = setTimeout(
      expire,
      Math.max(0, identity.expires - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [identity, expire]);
  return identity ? (
    <Authenticated
      key={identity.token || identity.subject}
      identity={identity}
      recovery={recovery}
      documentRecovery={documentRecovery}
      expire={expire}
      signOut={signOut}
    />
  ) : (
    <SignIn config={config} onSignIn={signIn} notice={notice} />
  );
}
function Authenticated({
  identity,
  recovery,
  documentRecovery,
  expire,
  signOut,
}: {
  identity: Identity;
  recovery: Recovery;
  documentRecovery: DocumentRecovery;
  expire: () => void;
  signOut: () => void;
}): ReactNode {
  const [chat] = useState(
    () => new Api(config.chatApiUrl, identity.token, expire),
  );
  const [ingestion] = useState(
    () => new Api(config.ingestionApiUrl, identity.token, expire),
  );
  const [documentsOpen, setDocumentsOpen] = useState(false);
  useEffect(
    () => () => {
      chat.dispose();
      ingestion.dispose();
    },
    [chat, ingestion],
  );
  return (
    <>
      <Chat
        api={chat}
        recovery={recovery}
        signOut={signOut}
        openDocuments={() => setDocumentsOpen(true)}
        feedback={(kind, id, value, saved) => (
          <FeedbackControl
            key={`${kind}:${id}`}
            kind={kind}
            value={value}
            save={async (next) => {
              await chat.json(
                `/${kind === "answer" ? "messages" : "conversations"}/${id}/feedback`,
                {
                  method: next ? "PUT" : "DELETE",
                  body: next ? JSON.stringify(next) : undefined,
                },
              );
              saved(next);
            }}
          />
        )}
      />
      <Documents
        api={ingestion}
        recovery={documentRecovery}
        open={documentsOpen}
        onClose={() => setDocumentsOpen(false)}
      />
    </>
  );
}

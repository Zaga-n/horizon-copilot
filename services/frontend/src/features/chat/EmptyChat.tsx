import type { ReactNode } from "react";
import { ArrowUp, Compass, Plus } from "lucide-react";
const STARTERS = [
  "Summarize the key points in my documents",
  "What should I know about this project?",
];
export function EmptyChat({
  onStarter,
  openDocuments,
}: {
  onStarter: (starter: string) => void;
  openDocuments: () => void;
}): ReactNode {
  return (
    <div className="empty-chat">
      <div className="horizon-mark">
        <Compass size={36} />
      </div>
      <p className="eyebrow">A LITTLE CLARITY GOES A LONG WAY</p>
      <h2>
        What would you like
        <br />
        to understand?
      </h2>
      <p>
        Bring your questions. Horizon connects the dots
        <br />
        across your documents, with sources along the way.
      </p>
      <div className="starters">
        {STARTERS.map((starter) => (
          <button key={starter} onClick={() => onStarter(starter)}>
            {starter}
            <ArrowUp size={16} />
          </button>
        ))}
      </div>
      <button className="text-button" onClick={openDocuments}>
        <Plus size={16} /> Add your first document
      </button>
    </div>
  );
}

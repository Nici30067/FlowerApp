import { Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { fmtTime } from "@/lib/travel/planner";
import type { Brief } from "@/lib/travel/types";
import { cn } from "@/lib/utils";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

const EXAMPLES = [
  "Saturday in Berlin, 10 to 5, around €60, I like art and parks",
  "A day in Lisbon from 9 until 6, €80, views and food",
  "Paris, 11 to 7, cheap day, galleries and gardens",
];

export function IntakeChat({
  messages,
  brief,
  busy,
  onSend,
  showBrief = true,
  disabled = false,
  placeholder = "Describe your day…",
}: {
  messages: ChatMessage[];
  brief: Brief;
  busy: boolean;
  onSend: (text: string) => void;
  showBrief?: boolean;
  disabled?: boolean;
  placeholder?: string;
}) {
  const [value, setValue] = useState("");
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages.length, busy]);

  const submit = (text: string) => {
    const t = text.trim();
    if (!t || busy || disabled) return;
    setValue("");
    onSend(t);
  };

  const facts: Array<[string, string | null]> = [
    [
      "City",
      brief.cityId
        ? brief.cityId.charAt(0).toUpperCase() + brief.cityId.slice(1)
        : null,
    ],
    [
      "Hours",
      brief.startMin !== null && brief.endMin !== null
        ? `${fmtTime(brief.startMin)}–${fmtTime(brief.endMin)}`
        : null,
    ],
    ["Budget", brief.budgetEur !== null ? `€${brief.budgetEur}` : null],
    ["Interests", brief.interests.length ? brief.interests.join(", ") : null],
    ["Max walk", brief.maxWalkKm !== null ? `${brief.maxWalkKm} km` : null],
  ];

  return (
    <div className="space-y-4">
      {showBrief && (
        <div className="rounded-xl border bg-card p-4">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Your day so far
          </h2>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            {facts.map(([label, val]) => (
              <div key={label} className="min-w-0">
                <dt className="text-xs text-muted-foreground">{label}</dt>
                <dd
                  className={cn(
                    "truncate font-medium",
                    !val && "italic text-muted-foreground/70",
                  )}
                >
                  {val ?? "unknown"}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      <div className="space-y-3">
        {messages.map((m, i) => (
          <div
            key={i}
            className={cn(
              "flex items-end gap-2",
              m.role === "user" && "justify-end",
            )}
          >
            {m.role === "assistant" && (
              <span className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent text-[11px] font-semibold text-accent-foreground">
                C
              </span>
            )}
            <div
              className={cn(
                "max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
                m.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : "border bg-card",
              )}
            >
              {m.content}
            </div>
          </div>
        ))}
        {busy && (
          <div className="flex items-end gap-2">
            <span className="mb-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent text-[11px] font-semibold text-accent-foreground">
              C
            </span>
            <div className="w-16 rounded-2xl border bg-card px-4 py-3">
              <span className="flex gap-1">
                {[0, 1, 2].map((i) => (
                  <span
                    key={i}
                    className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground"
                    style={{ animationDelay: `${i * 120}ms` }}
                  />
                ))}
              </span>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {messages.length === 0 && (
        <div className="flex flex-wrap gap-2">
          {EXAMPLES.map((e) => (
            <button
              key={e}
              onClick={() => submit(e)}
              className="rounded-full border bg-card px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-foreground"
            >
              {e}
            </button>
          ))}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit(value);
        }}
        className="flex gap-2"
      >
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={placeholder}
          disabled={disabled}
          className="flex-1 rounded-lg border bg-card px-3 py-2.5 text-sm outline-none focus:ring-2 focus:ring-ring/40 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={busy || disabled || !value.trim()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          <Send className="h-4 w-4" />
        </button>
      </form>
    </div>
  );
}

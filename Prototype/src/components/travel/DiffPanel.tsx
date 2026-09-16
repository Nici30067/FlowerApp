import { ArrowRight, Check, Equal, Minus, Plus, X } from "lucide-react";

import { diffSummary } from "@/lib/travel/diff";
import type { DiffRow } from "@/lib/travel/types";

const STYLES: Record<DiffRow["kind"], { icon: typeof Plus; color: string; label: string }> = {
  added: { icon: Plus, color: "text-added", label: "Added" },
  removed: { icon: Minus, color: "text-removed", label: "Removed" },
  retimed: { icon: ArrowRight, color: "text-retimed", label: "Retimed" },
  kept: { icon: Equal, color: "text-muted-foreground", label: "Kept" },
};

export function DiffPanel({
  rows,
  reason,
  revision,
  onAccept,
  onDiscard,
}: {
  rows: DiffRow[];
  reason: string;
  revision: number;
  onAccept: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="rounded-xl border-2 border-primary/50 bg-card p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold">Proposed revision {revision + 1}</h2>
        <span className="text-xs text-muted-foreground">{diffSummary(rows)}</span>
      </div>
      <p className="mt-1 text-sm text-muted-foreground">{reason}</p>

      <ul className="mt-3 space-y-1.5">
        {rows.map((row, i) => {
          const s = STYLES[row.kind];
          const Icon = s.icon;
          return (
            <li key={`${row.kind}-${row.name}-${i}`} className="flex items-start gap-2 text-sm">
              <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${s.color}`} />
              <span className="font-medium">{row.name}</span>
              <span className="text-xs text-muted-foreground">{row.detail}</span>
            </li>
          );
        })}
      </ul>

      <div className="mt-4 flex gap-2">
        <button
          onClick={onAccept}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
        >
          <Check className="h-4 w-4" /> Accept revision
        </button>
        <button
          onClick={onDiscard}
          className="inline-flex items-center gap-1.5 rounded-lg border px-4 py-2 text-sm font-medium transition-colors hover:bg-secondary"
        >
          <X className="h-4 w-4" /> Keep current plan
        </button>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        Your live itinerary is untouched until you accept.
      </p>
    </div>
  );
}

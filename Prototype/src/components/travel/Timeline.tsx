import { Check, Coffee, Lock, LockOpen, MapPin, TriangleAlert } from "lucide-react";

import { fmtTime } from "@/lib/travel/planner";
import type { Itinerary } from "@/lib/travel/types";
import { cn } from "@/lib/utils";

interface Props {
  itinerary: Itinerary;
  activeIndex: number | null;
  onHover: (index: number | null) => void;
  onToggleLock: (placeId: string) => void;
  onToggleDone: (placeId: string) => void;
}

export function Timeline({ itinerary, activeIndex, onHover, onToggleLock, onToggleDone }: Props) {
  let placeIdx = -1;

  return (
    <ol className="space-y-3">
      {itinerary.stops.map((stop) => {
        if (stop.kind === "place") placeIdx += 1;
        const index = placeIdx;
        const isBreak = stop.kind === "break";

        return (
          <li
            key={stop.placeId}
            onMouseEnter={() => onHover(isBreak ? null : index)}
            onMouseLeave={() => onHover(null)}
            className={cn(
              "rounded-xl border bg-card p-4 transition-shadow",
              !isBreak && activeIndex === index && "shadow-md ring-1 ring-ring/40",
              stop.done && "opacity-60",
            )}
          >
            <div className="flex items-start gap-3">
              <div
                className={cn(
                  "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold",
                  isBreak
                    ? "bg-muted text-muted-foreground"
                    : "bg-primary text-primary-foreground",
                )}
              >
                {isBreak ? <Coffee className="h-4 w-4" /> : index + 1}
              </div>

              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className="font-mono text-sm tabular-nums text-muted-foreground">
                    {fmtTime(stop.arriveMin)}–{fmtTime(stop.leaveMin)}
                  </span>
                  <h3 className={cn("text-base font-semibold", stop.done && "line-through")}>
                    {stop.name}
                  </h3>
                  {stop.locked && (
                    <span className="rounded-full bg-secondary px-2 py-0.5 text-[11px] font-medium text-secondary-foreground">
                      locked
                    </span>
                  )}
                </div>

                <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{stop.reason}</p>

                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                  {!isBreak && (
                    <span className="inline-flex items-center gap-1">
                      <MapPin className="h-3 w-3" />
                      {stop.walkKm.toFixed(2)} km · {stop.walkMin} min walk
                    </span>
                  )}
                  <span>
                    {stop.costEur === null ? (
                      <span className="italic">cost unknown</span>
                    ) : stop.costEur === 0 ? (
                      "free"
                    ) : (
                      `€${stop.costEur}`
                    )}
                  </span>
                  {!isBreak && <span>{stop.outdoor ? "outdoor" : "indoor"}</span>}
                </div>

                {stop.warnings.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {stop.warnings.map((w) => (
                      <li
                        key={w}
                        className="flex items-start gap-1.5 text-xs text-destructive"
                      >
                        <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                        {w}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {!isBreak && (
                <div className="flex shrink-0 flex-col gap-1">
                  <button
                    onClick={() => onToggleLock(stop.placeId)}
                    aria-label={stop.locked ? "Unlock stop" : "Lock stop"}
                    className={cn(
                      "rounded-md border p-1.5 transition-colors hover:bg-secondary",
                      stop.locked && "border-primary text-primary",
                    )}
                  >
                    {stop.locked ? (
                      <Lock className="h-3.5 w-3.5" />
                    ) : (
                      <LockOpen className="h-3.5 w-3.5" />
                    )}
                  </button>
                  <button
                    onClick={() => onToggleDone(stop.placeId)}
                    aria-label={stop.done ? "Mark as not done" : "Mark as done"}
                    className={cn(
                      "rounded-md border p-1.5 transition-colors hover:bg-secondary",
                      stop.done && "border-primary bg-primary text-primary-foreground",
                    )}
                  >
                    <Check className="h-3.5 w-3.5" />
                  </button>
                </div>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

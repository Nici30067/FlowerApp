import { CloudSun, Compass, Footprints, Wallet } from "lucide-react";

import type { AgentFinding } from "@/lib/travel/types";
import { cn } from "@/lib/utils";

const AGENTS = [
  { key: "discovery", label: "Discovery", icon: Compass, blurb: "Ranks places against your interests" },
  { key: "conditions", label: "Conditions", icon: CloudSun, blurb: "Weather and exposed stops" },
  { key: "mobility", label: "Mobility", icon: Footprints, blurb: "What's reachable on foot" },
  { key: "budget", label: "Budget & pace", icon: Wallet, blurb: "Money and hours" },
] as const;

export function AgentPanels({
  findings,
  running,
}: {
  findings: AgentFinding[];
  running: boolean;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {AGENTS.map((agent) => {
        const finding = findings.find((f) => f.agent === agent.key);
        const Icon = agent.icon;
        return (
          <div
            key={agent.key}
            className={cn(
              "rounded-xl border bg-card p-4",
              running && !finding && "animate-pulse",
            )}
          >
            <div className="flex items-center gap-2">
              <span className="flex h-7 w-7 items-center justify-center rounded-full bg-accent text-accent-foreground">
                <Icon className="h-4 w-4" />
              </span>
              <h3 className="text-sm font-semibold">{agent.label}</h3>
            </div>

            {finding ? (
              <>
                <p className="mt-2 text-sm font-medium">{finding.headline}</p>
                <ul className="mt-1.5 space-y-1">
                  {finding.notes.map((n) => (
                    <li key={n} className="text-xs leading-relaxed text-muted-foreground">
                      · {n}
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">
                {running ? "Working…" : agent.blurb}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

import { createFileRoute } from "@tanstack/react-router";
import { ClientOnly } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import {
  CloudRain,
  Clock,
  Footprints,
  RefreshCw,
  RotateCcw,
  TriangleAlert,
} from "lucide-react";
import {
  Suspense,
  lazy,
  useCallback,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { AgentPanels } from "@/components/travel/AgentPanels";
import { DiffPanel } from "@/components/travel/DiffPanel";
import { IntakeChat, type ChatMessage } from "@/components/travel/IntakeChat";
import { Timeline } from "@/components/travel/Timeline";
import { intakeTurn, runSpecialists } from "@/lib/travel.functions";
import { CITIES, getCity } from "@/lib/travel/data";
import { diffItineraries } from "@/lib/travel/diff";
import { buildItinerary, fmtTime } from "@/lib/travel/planner";
import type {
  AgentFinding,
  Brief,
  DiffRow,
  Itinerary,
} from "@/lib/travel/types";

const MapView = lazy(() => import("@/components/travel/MapView"));

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Travel Companion — plan one perfect day in a city" },
      {
        name: "description",
        content:
          "Describe your day in plain words and four AI specialists propose a walkable, budgeted city itinerary on a map. You approve every change.",
      },
      {
        property: "og:title",
        content: "Travel Companion — plan one perfect day in a city",
      },
      {
        property: "og:description",
        content:
          "A map-based one-day city planner where AI specialists suggest and you approve every revision.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Planner,
});

const EMPTY_BRIEF: Brief = {
  cityId: null,
  startMin: null,
  endMin: null,
  budgetEur: null,
  interests: [],
  maxWalkKm: null,
};

interface Proposal {
  itinerary: Itinerary;
  rows: DiffRow[];
  reason: string;
}

function Planner() {
  const intake = useServerFn(intakeTurn);
  const specialists = useServerFn(runSpecialists);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [brief, setBrief] = useState<Brief>(EMPTY_BRIEF);
  const [chatBusy, setChatBusy] = useState(false);
  const [planning, setPlanning] = useState(false);
  const [findings, setFindings] = useState<AgentFinding[]>([]);
  const [itinerary, setItinerary] = useState<Itinerary | null>(null);
  const [history, setHistory] = useState<Itinerary[]>([]);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);

  const city = useMemo(
    () => getCity(itinerary?.cityId ?? brief.cityId ?? "") ?? CITIES[0]!,
    [itinerary?.cityId, brief.cityId],
  );

  const say = useCallback((content: string) => {
    setMessages((prev) => [...prev, { role: "assistant", content }]);
  }, []);

  const plan = useCallback(
    async (
      nextBrief: Brief,
      trigger: string | null,
      live: Itinerary | null,
    ) => {
      setPlanning(true);
      setError(null);
      setFindings([]);
      try {
        const lockedIds = (live?.stops ?? [])
          .filter((s) => s.locked)
          .map((s) => s.placeId);
        const doneIds = (live?.stops ?? [])
          .filter((s) => s.done)
          .map((s) => s.placeId);

        const res = await specialists({
          data: { brief: nextBrief, lockedIds, doneIds, trigger },
        });

        if (!res.ok) {
          setError(res.message);
          return;
        }
        setFindings(res.findings);

        const built = buildItinerary({
          brief: nextBrief,
          orderedPlaceIds: res.orderedPlaceIds,
          reasons: res.reasons,
          revision: live?.revision ?? 1,
          preserved: live?.stops ?? [],
          lockedIds,
        });

        if (!live) {
          setItinerary(built);
          setProposal(null);
          const cityName = getCity(built.cityId)?.name ?? "your city";
          say(
            `Here's your day in ${cityName} — take a look below, and just tell me what changes.`,
          );
        } else {
          setProposal({
            itinerary: built,
            rows: diffItineraries(live, built),
            reason: trigger ?? "Manual re-plan.",
          });
          say(
            "Here's a revised plan — review the changes below and accept or keep your current day.",
          );
        }
      } catch {
        setError("Could not reach the planning service. Please try again.");
      } finally {
        setPlanning(false);
      }
    },
    [specialists, say],
  );

  const send = useCallback(
    async (text: string) => {
      const next = [...messages, { role: "user" as const, content: text }];
      setMessages(next);
      setChatBusy(true);
      setError(null);
      try {
        const res = await intake({ data: { messages: next, brief } });
        if (!res.ok) {
          setError(res.message);
          return;
        }
        const merged: Brief = {
          cityId: res.cityId ?? brief.cityId,
          startMin: res.startMin ?? brief.startMin,
          endMin: res.endMin ?? brief.endMin,
          budgetEur: res.budgetEur ?? brief.budgetEur,
          interests: res.interests.length ? res.interests : brief.interests,
          maxWalkKm: res.maxWalkKm ?? brief.maxWalkKm,
        };
        setBrief(merged);
        setMessages([...next, { role: "assistant", content: res.reply }]);
        if (res.complete) {
          void plan(
            { ...merged, maxWalkKm: merged.maxWalkKm ?? 6 },
            null,
            null,
          );
        }
      } catch {
        setError("Could not reach the assistant. Please try again.");
      } finally {
        setChatBusy(false);
      }
    },
    [messages, brief, intake, plan],
  );

  const toggle = (placeId: string, field: "locked" | "done") => {
    setItinerary((prev) =>
      prev
        ? {
            ...prev,
            stops: prev.stops.map((s) =>
              s.placeId === placeId ? { ...s, [field]: !s[field] } : s,
            ),
          }
        : prev,
    );
  };

  const accept = () => {
    if (!proposal || !itinerary) return;
    setHistory((h) => [...h, itinerary]);
    setItinerary({ ...proposal.itinerary, revision: itinerary.revision + 1 });
    setProposal(null);
    setMessages((prev) => [
      ...prev,
      { role: "user", content: "Accept revision" },
    ]);
    say("Done — that's your live plan now.");
  };

  const discard = () => {
    setProposal(null);
    setMessages((prev) => [
      ...prev,
      { role: "user", content: "Keep current plan" },
    ]);
    say("No problem, keeping your current plan as-is.");
  };

  const ask = (label: string, run: () => void) => {
    setMessages((prev) => [...prev, { role: "user", content: label }]);
    run();
  };

  const triggers = itinerary
    ? [
        {
          label: "Rain this afternoon",
          icon: CloudRain,
          run: () =>
            ask("Rain this afternoon", () =>
              plan(
                { ...brief },
                "Rain moved into the afternoon — outdoor stops after midday should be swapped for indoor ones.",
                itinerary,
              ),
            ),
        },
        {
          label: "Too much walking",
          icon: Footprints,
          run: () =>
            ask("Too much walking", () =>
              plan(
                {
                  ...brief,
                  maxWalkKm: Math.max(1.5, (brief.maxWalkKm ?? 6) * 0.6),
                },
                "The traveller says the day involves too much walking — tighten the route.",
                itinerary,
              ),
            ),
        },
        {
          label: "Running 45 min late",
          icon: Clock,
          run: () =>
            ask("Running 45 min late", () =>
              plan(
                { ...brief, startMin: (brief.startMin ?? 600) + 45 },
                "The traveller is running 45 minutes late — the remaining day has to be shortened.",
                itinerary,
              ),
            ),
        },
        {
          label: "Re-plan",
          icon: RefreshCw,
          run: () =>
            ask("Re-plan", () =>
              plan({ ...brief }, "Manual re-plan requested.", itinerary),
            ),
        },
      ]
    : [];

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card/70 backdrop-blur">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-3 px-5 py-4">
          <div>
            <h1 className="text-2xl font-semibold">Travel Companion</h1>
            <p className="text-sm text-muted-foreground">
              One day, one city, four specialists — and you approve every
              change.
            </p>
          </div>
          {itinerary && (
            <div className="flex items-center gap-4 text-sm">
              <div className="text-right">
                <div className="font-mono text-xs uppercase tracking-wide text-muted-foreground">
                  Revision
                </div>
                <div className="font-semibold">v{itinerary.revision}</div>
              </div>
              <div className="text-right">
                <div className="font-mono text-xs uppercase tracking-wide text-muted-foreground">
                  Walking
                </div>
                <div className="font-semibold">
                  {itinerary.totalWalkKm.toFixed(1)} km
                </div>
              </div>
              <div className="text-right">
                <div className="font-mono text-xs uppercase tracking-wide text-muted-foreground">
                  Cost
                </div>
                <div className="font-semibold">
                  €{itinerary.totalCostEur}
                  {itinerary.unknownCostCount > 0 && "+"}
                </div>
              </div>
              <div className="text-right">
                <div className="font-mono text-xs uppercase tracking-wide text-muted-foreground">
                  Ends
                </div>
                <div className="font-semibold">
                  {fmtTime(itinerary.endsAtMin)}
                </div>
              </div>
            </div>
          )}
        </div>
      </header>

      <div className="mx-auto grid max-w-[1400px] gap-6 px-5 py-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <section className="order-2 space-y-4 lg:order-1">
          {error && (
            <div className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <IntakeChat
            messages={messages}
            brief={brief}
            busy={chatBusy}
            onSend={send}
            showBrief={!itinerary}
            disabled={Boolean(itinerary)}
            placeholder={
              itinerary
                ? "Use the quick replies below to adjust your plan…"
                : "Describe your day…"
            }
          />

          {(planning || findings.length > 0) && (
            <Bubble>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Specialist findings
              </p>
              <AgentPanels findings={findings} running={planning} />
              <p className="mt-2 text-xs text-muted-foreground">
                The specialists only rank and suggest. Arrival times, walking
                distances, opening hours, rest breaks and totals are computed by
                the app from its own data.
              </p>
            </Bubble>
          )}

          {itinerary && (
            <Bubble>
              {itinerary.warnings.length > 0 && (
                <ul className="mb-3 space-y-1 rounded-lg border border-destructive/30 bg-destructive/5 p-3">
                  {itinerary.warnings.map((w) => (
                    <li
                      key={w}
                      className="flex items-start gap-2 text-xs text-destructive"
                    >
                      <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                      {w}
                    </li>
                  ))}
                </ul>
              )}

              <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Your day in {city.name}
              </p>
              <Timeline
                itinerary={itinerary}
                activeIndex={activeIndex}
                onHover={setActiveIndex}
                onToggleLock={(id) => toggle(id, "locked")}
                onToggleDone={(id) => toggle(id, "done")}
              />

              <div className="mt-4 flex flex-wrap gap-2">
                {triggers.map((t) => (
                  <button
                    key={t.label}
                    disabled={planning}
                    onClick={() => void t.run()}
                    className="inline-flex items-center gap-1.5 rounded-full border bg-background px-3 py-1.5 text-xs font-medium transition-colors hover:border-primary disabled:opacity-50"
                  >
                    <t.icon className="h-3.5 w-3.5" />
                    {t.label}
                  </button>
                ))}
              </div>
            </Bubble>
          )}

          {proposal && (
            <Bubble>
              <DiffPanel
                rows={proposal.rows}
                reason={proposal.reason}
                revision={itinerary?.revision ?? 1}
                onAccept={accept}
                onDiscard={discard}
              />
            </Bubble>
          )}

          {history.length > 0 && (
            <Bubble>
              <h3 className="flex items-center gap-1.5 text-sm font-semibold">
                <RotateCcw className="h-3.5 w-3.5" /> Revision history
              </h3>
              <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                {history.map((h) => (
                  <li key={h.revision}>
                    v{h.revision} ·{" "}
                    {h.stops.filter((s) => s.kind === "place").length} stops ·{" "}
                    {h.totalWalkKm.toFixed(1)} km · €{h.totalCostEur}
                  </li>
                ))}
              </ul>
            </Bubble>
          )}
        </section>

        <section className="order-1 lg:order-2">
          <div className="sticky top-6 h-[420px] overflow-hidden rounded-2xl border shadow-sm lg:h-[calc(100vh-7rem)]">
            <ClientOnly
              fallback={
                <div className="h-full w-full animate-pulse bg-muted" />
              }
            >
              <Suspense
                fallback={
                  <div className="h-full w-full animate-pulse bg-muted" />
                }
              >
                <MapView
                  city={city}
                  stops={itinerary?.stops ?? []}
                  ghostStops={proposal?.itinerary.stops ?? []}
                  activeIndex={activeIndex}
                />
              </Suspense>
            </ClientOnly>
          </div>
        </section>
      </div>
    </main>
  );
}

function Bubble({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <span className="mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent text-[11px] font-semibold text-accent-foreground">
        C
      </span>
      <div className="min-w-0 flex-1 rounded-2xl border bg-card p-4">
        {children}
      </div>
    </div>
  );
}

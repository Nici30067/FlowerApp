import { fmtTime } from "./planner";
import type { DiffRow, Itinerary } from "./types";

export function diffItineraries(live: Itinerary, next: Itinerary): DiffRow[] {
  const rows: DiffRow[] = [];
  const liveStops = live.stops.filter((s) => s.kind === "place");
  const nextStops = next.stops.filter((s) => s.kind === "place");
  const nextById = new Map(nextStops.map((s) => [s.placeId, s]));
  const liveById = new Map(liveStops.map((s) => [s.placeId, s]));

  for (const s of liveStops) {
    const n = nextById.get(s.placeId);
    if (!n) {
      rows.push({
        kind: "removed",
        name: s.name,
        detail: s.locked ? "Locked — should not be removed" : `Was ${fmtTime(s.arriveMin)}`,
      });
    } else if (n.arriveMin !== s.arriveMin) {
      rows.push({
        kind: "retimed",
        name: s.name,
        detail: `${fmtTime(s.arriveMin)} → ${fmtTime(n.arriveMin)}`,
      });
    } else {
      rows.push({ kind: "kept", name: s.name, detail: `Stays at ${fmtTime(s.arriveMin)}` });
    }
  }

  for (const n of nextStops) {
    if (!liveById.has(n.placeId)) {
      rows.push({
        kind: "added",
        name: n.name,
        detail: `${fmtTime(n.arriveMin)} · ${n.reason}`,
      });
    }
  }

  const order = { added: 0, removed: 1, retimed: 2, kept: 3 } as const;
  rows.sort((a, b) => order[a.kind] - order[b.kind]);
  return rows;
}

export function diffSummary(rows: DiffRow[]): string {
  const c = (k: DiffRow["kind"]) => rows.filter((r) => r.kind === k).length;
  return `${c("added")} added · ${c("removed")} removed · ${c("retimed")} retimed · ${c("kept")} kept`;
}

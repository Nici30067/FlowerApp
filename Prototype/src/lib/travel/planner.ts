import { getCity, getPlace } from "./data";
import type { Brief, Itinerary, Place, Stop } from "./types";

export const WALK_KMH = 4.5;
const DEFAULT_VISIT_MIN = 45;
const REST_AFTER_MIN = 210; // insert a rest break after this much elapsed time
const REST_LEN_MIN = 20;

export function haversineKm(
  a: { lat: number; lng: number },
  b: { lat: number; lng: number },
): number {
  const R = 6371;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLng = ((b.lng - a.lng) * Math.PI) / 180;
  const la1 = (a.lat * Math.PI) / 180;
  const la2 = (b.lat * Math.PI) / 180;
  const x =
    Math.sin(dLat / 2) ** 2 + Math.sin(dLng / 2) ** 2 * Math.cos(la1) * Math.cos(la2);
  return 2 * R * Math.asin(Math.sqrt(x));
}

export function walkMinutes(km: number): number {
  return Math.round((km / WALK_KMH) * 60);
}

export function fmtTime(min: number): string {
  const h = Math.floor(min / 60) % 24;
  const m = Math.round(min % 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

export function parseTime(text: string): number | null {
  const m = text.match(/^(\d{1,2})(?::(\d{2}))?$/);
  if (!m) return null;
  return Number(m[1]) * 60 + Number(m[2] ?? 0);
}

export interface BuildOptions {
  brief: Brief;
  orderedPlaceIds: string[];
  reasons: Record<string, string>;
  revision: number;
  /** stops the user already completed, kept at the front and untouched */
  preserved?: Stop[];
  lockedIds?: string[];
}

/**
 * Deterministic itinerary construction. The AI never supplies times, distances
 * or prices — everything here is computed from the curated place data.
 */
export function buildItinerary(opts: BuildOptions): Itinerary {
  const { brief, orderedPlaceIds, reasons, revision } = opts;
  const cityId = brief.cityId ?? "berlin";
  const city = getCity(cityId);
  const startMin = brief.startMin ?? 10 * 60;
  const endMin = brief.endMin ?? 17 * 60;
  const lockedIds = new Set(opts.lockedIds ?? []);

  const preserved = (opts.preserved ?? []).filter((s) => s.done);
  const stops: Stop[] = preserved.map((s) => ({ ...s }));

  const last: Stop | undefined = stops[stops.length - 1];
  let cursor = last ? last.leaveMin : startMin;
  let prevPoint: { lat: number; lng: number } = last
    ? { lat: last.lat, lng: last.lng }
    : (city?.center ?? { lat: 0, lng: 0 });

  let sinceRest = 0;
  const warnings: string[] = [];
  let totalWalkKm = 0;
  let totalCost = 0;
  let unknownCostCount = 0;
  const used = new Set(stops.map((s) => s.placeId));

  for (const id of orderedPlaceIds) {
    if (used.has(id)) continue;
    const place = getPlace(id);
    if (!place || place.cityId !== cityId) continue;

    const km = haversineKm(prevPoint, place);
    const wMin = walkMinutes(km);
    let arrive = cursor + wMin;

    const stopWarnings: string[] = [];

    // Rest break when the day has run long without one
    if (sinceRest >= REST_AFTER_MIN) {
      stops.push({
        placeId: `break-${stops.length}`,
        kind: "break",
        name: "Rest break",
        arriveMin: cursor,
        leaveMin: cursor + REST_LEN_MIN,
        walkKm: 0,
        walkMin: 0,
        costEur: null,
        reason: "Inserted automatically after more than 3.5 hours on the move.",
        locked: false,
        done: false,
        warnings: [],
        lat: prevPoint.lat,
        lng: prevPoint.lng,
        outdoor: true,
      });
      cursor += REST_LEN_MIN;
      arrive += REST_LEN_MIN;
      sinceRest = 0;
    }

    // Opening hours
    if (place.hours) {
      if (arrive < place.hours.open) {
        stopWarnings.push(`Opens at ${fmtTime(place.hours.open)} — waiting on arrival.`);
        arrive = place.hours.open;
      }
      if (arrive >= place.hours.close) {
        stopWarnings.push(`Closed by the time you'd arrive (shuts ${fmtTime(place.hours.close)}).`);
      }
    } else {
      stopWarnings.push("Opening hours unknown.");
    }

    const visit = place.durationMin ?? DEFAULT_VISIT_MIN;
    const leave = arrive + visit;

    if (leave > endMin) {
      warnings.push(`${place.name} would run past your ${fmtTime(endMin)} finish — dropped.`);
      continue;
    }

    if (place.priceEur === null) {
      unknownCostCount += 1;
      stopWarnings.push("Price unknown.");
    } else {
      totalCost += place.priceEur;
    }

    if (
      brief.maxWalkKm !== null &&
      brief.maxWalkKm !== undefined &&
      totalWalkKm + km > brief.maxWalkKm &&
      !lockedIds.has(id)
    ) {
      warnings.push(`${place.name} sits beyond your ${brief.maxWalkKm} km walking limit — dropped.`);
      if (place.priceEur !== null) totalCost -= place.priceEur;
      continue;
    }

    totalWalkKm += km;
    if (km > 2.5) stopWarnings.push(`Long walk: ${km.toFixed(1)} km.`);

    stops.push({
      placeId: place.id,
      kind: "place",
      name: place.name,
      arriveMin: arrive,
      leaveMin: leave,
      walkKm: Number(km.toFixed(2)),
      walkMin: wMin,
      costEur: place.priceEur,
      reason: reasons[place.id] ?? place.blurb,
      locked: lockedIds.has(place.id),
      done: false,
      warnings: stopWarnings,
      lat: place.lat,
      lng: place.lng,
      outdoor: place.outdoor,
    });

    used.add(place.id);
    sinceRest += wMin + visit;
    cursor = leave;
    prevPoint = { lat: place.lat, lng: place.lng };
  }

  if (brief.budgetEur !== null && brief.budgetEur !== undefined && totalCost > brief.budgetEur) {
    warnings.push(
      `Total of €${totalCost} is over your €${brief.budgetEur} budget by €${totalCost - brief.budgetEur}.`,
    );
  }
  if (unknownCostCount > 0) {
    warnings.push(`${unknownCostCount} stop(s) have an unknown price — the total is a floor, not a final figure.`);
  }
  if (stops.length === 0) {
    warnings.push("Nothing fitted the constraints. Try a wider time window or a higher walking limit.");
  }

  return {
    revision,
    cityId,
    stops,
    totalWalkKm: Number(totalWalkKm.toFixed(2)),
    totalCostEur: totalCost,
    unknownCostCount,
    endsAtMin: cursor,
    warnings,
  };
}

/** Deterministic fallback ordering used when the AI is unavailable. */
export function fallbackOrder(places: Place[], brief: Brief): string[] {
  const interests = brief.interests.map((i) => i.toLowerCase());
  const scored = places.map((p) => {
    let score = 0;
    for (const t of [...p.tags, p.category]) {
      if (interests.some((i) => t.includes(i) || i.includes(t))) score += 2;
    }
    if (p.priceEur === 0) score += 0.5;
    return { p, score };
  });
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, 6).map((s) => s.p.id);
}

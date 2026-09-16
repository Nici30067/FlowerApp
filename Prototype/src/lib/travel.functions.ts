import { createServerFn } from "@tanstack/react-start";
import { z } from "zod";

import { CITIES, getCity, placesForCity } from "@/lib/travel/data";
import { fmtTime, fallbackOrder, haversineKm } from "@/lib/travel/planner";
import type { AgentFinding, Brief, Place } from "@/lib/travel/types";

const briefSchema = z.object({
  cityId: z.string().nullable(),
  startMin: z.number().nullable(),
  endMin: z.number().nullable(),
  budgetEur: z.number().nullable(),
  interests: z.array(z.string()),
  maxWalkKm: z.number().nullable(),
});

const messageSchema = z.object({
  role: z.enum(["user", "assistant"]),
  content: z.string(),
});

/* ------------------------------------------------------------------ */
/* 1. Intake — one question at a time until the brief is complete,     */
/*    parsed locally with no external AI call.                        */
/* ------------------------------------------------------------------ */

interface IntakeResult {
  reply: string;
  complete: boolean;
  cityId: string | null;
  startMin: number | null;
  endMin: number | null;
  budgetEur: number | null;
  interests: string[];
  maxWalkKm: number | null;
}

function extractCity(text: string): string | null {
  const lower = text.toLowerCase();
  for (const c of CITIES) {
    if (lower.includes(c.id) || lower.includes(c.name.toLowerCase()))
      return c.id;
  }
  return null;
}

function extractHours(text: string): {
  startMin: number | null;
  endMin: number | null;
} {
  const re =
    /(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to|until|-|–)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/i;
  const m = re.exec(text);
  if (!m) return { startMin: null, endMin: null };
  const [, h1, m1, ap1, h2, m2, ap2] = m;
  let startH = Number(h1);
  let endH = Number(h2);
  const startPart = m1 ? Number(m1) : 0;
  const endPart = m2 ? Number(m2) : 0;
  if (ap1?.toLowerCase() === "pm" && startH < 12) startH += 12;
  if (ap2?.toLowerCase() === "pm" && endH < 12) endH += 12;
  if (!ap1 && !ap2 && endH <= startH) endH += 12;
  return { startMin: startH * 60 + startPart, endMin: endH * 60 + endPart };
}

function extractBudget(text: string): number | null {
  const m = /€\s*(\d+)|(\d+)\s*(?:eur|euros?)\b/i.exec(text);
  if (m) return Number(m[1] ?? m[2]);
  if (/\bcheap\b/i.test(text)) return 40;
  return null;
}

const INTEREST_WORDS: Record<string, string> = {
  art: "art",
  architecture: "architecture",
  park: "park",
  parks: "park",
  garden: "park",
  gardens: "park",
  coffee: "coffee",
  history: "history",
  food: "food",
  books: "books",
  shopping: "shopping",
  view: "view",
  views: "view",
  music: "music",
  market: "market",
  markets: "market",
};

function extractInterests(text: string): string[] {
  const lower = text.toLowerCase();
  const found = new Set<string>();
  for (const [word, tag] of Object.entries(INTEREST_WORDS)) {
    if (new RegExp(`\\b${word}\\b`).test(lower)) found.add(tag);
  }
  return [...found];
}

function extractWalk(text: string): number | null {
  const m = /(\d+(?:\.\d+)?)\s*km\b/i.exec(text);
  return m ? Number(m[1]) : null;
}

export const intakeTurn = createServerFn({ method: "POST" })
  .inputValidator((input: unknown) =>
    z
      .object({ messages: z.array(messageSchema), brief: briefSchema })
      .parse(input),
  )
  .handler(
    ({
      data,
    }):
      | ({ ok: true } & IntakeResult)
      | { ok: false; status: number; message: string } => {
      const lastUser = [...data.messages]
        .reverse()
        .find((m) => m.role === "user");
      const text = lastUser?.content ?? "";

      const cityId = extractCity(text) ?? data.brief.cityId;
      const { startMin, endMin } = extractHours(text);
      const budgetEur = extractBudget(text) ?? data.brief.budgetEur;
      const newInterests = extractInterests(text);
      const maxWalkKm = extractWalk(text) ?? data.brief.maxWalkKm;

      const merged: Brief = {
        cityId,
        startMin: startMin ?? data.brief.startMin,
        endMin: endMin ?? data.brief.endMin,
        budgetEur,
        interests: newInterests.length
          ? [...new Set([...data.brief.interests, ...newInterests])]
          : data.brief.interests,
        maxWalkKm,
      };

      const complete = Boolean(
        merged.cityId &&
        merged.startMin !== null &&
        merged.endMin !== null &&
        merged.budgetEur !== null &&
        merged.interests.length > 0,
      );

      let reply: string;
      if (complete) {
        const city = getCity(merged.cityId!)!;
        reply =
          `Great — a day in ${city.name} from ${fmtTime(merged.startMin!)} to ${fmtTime(merged.endMin!)}, ` +
          `around €${merged.budgetEur}, focused on ${merged.interests.join(", ")}. Sending this to the specialists now…`;
      } else if (!merged.cityId) {
        reply = `Which city are you visiting? I can plan for ${CITIES.map((c) => c.name).join(", ")}.`;
      } else if (merged.startMin === null || merged.endMin === null) {
        reply = "What time does your day start and end?";
      } else if (merged.budgetEur === null) {
        reply = "Roughly what's your budget for the day, in euros?";
      } else {
        reply =
          "What are you interested in — art, history, parks, food, views, shopping…?";
      }

      return {
        ok: true,
        reply,
        complete,
        cityId: merged.cityId,
        startMin: merged.startMin,
        endMin: merged.endMin,
        budgetEur: merged.budgetEur,
        interests: merged.interests,
        maxWalkKm: merged.maxWalkKm,
      };
    },
  );

/* ------------------------------------------------------------------ */
/* 2. The four specialists — deterministic ranking, no external AI.    */
/* ------------------------------------------------------------------ */

interface SpecialistResult {
  orderedPlaceIds: string[];
  reasons: Record<string, string>;
  findings: AgentFinding[];
}

export const runSpecialists = createServerFn({ method: "POST" })
  .inputValidator((input: unknown) =>
    z
      .object({
        brief: briefSchema,
        lockedIds: z.array(z.string()),
        doneIds: z.array(z.string()),
        trigger: z.string().nullable(),
      })
      .parse(input),
  )
  .handler(
    ({
      data,
    }):
      | ({ ok: true } & SpecialistResult)
      | { ok: false; status: number; message: string } => {
      const brief: Brief = data.brief;
      const cityId = brief.cityId ?? "berlin";
      const city = getCity(cityId)!;
      const places = placesForCity(cityId);
      const locked = new Set(data.lockedIds);
      const done = new Set(data.doneIds);
      const rainTriggered = (data.trigger ?? "").toLowerCase().includes("rain");
      const rainHeavy =
        city.forecast.filter((f) => f.sky === "rain").length >=
        city.forecast.length / 2;

      const available = places.filter((p) => !done.has(p.id));
      const lockedPlaces = available.filter((p) => locked.has(p.id));

      const ranked = fallbackOrder(
        available.filter(
          (p) => !locked.has(p.id) && !(rainTriggered && p.outdoor),
        ),
        brief,
      ).map((id) => available.find((p) => p.id === id)!);

      const maxWalk = brief.maxWalkKm ?? 6;
      const targetCount = maxWalk < 3 ? 4 : maxWalk > 8 ? 7 : 5;

      const chosen: Place[] = [...lockedPlaces];
      for (const p of ranked) {
        if (chosen.length >= targetCount) break;
        if (chosen.some((c) => c.id === p.id)) continue;
        chosen.push(p);
      }

      if (
        !chosen.some((p) => p.category === "food" || p.category === "market")
      ) {
        const food = ranked.find(
          (p) =>
            (p.category === "food" || p.category === "market") &&
            !chosen.some((c) => c.id === p.id),
        );
        if (food) {
          if (chosen.length >= targetCount) {
            const dropIndex = [...chosen]
              .reverse()
              .findIndex((p) => !locked.has(p.id));
            if (dropIndex !== -1)
              chosen.splice(chosen.length - 1 - dropIndex, 1);
          }
          chosen.push(food);
        }
      }

      // Order into a sensible walking sequence via nearest-neighbour from the city centre.
      const ordered: Place[] = [];
      const pool = [...chosen];
      let cursor: { lat: number; lng: number } = city.center;
      while (pool.length) {
        pool.sort((a, b) => haversineKm(cursor, a) - haversineKm(cursor, b));
        const next = pool.shift()!;
        ordered.push(next);
        cursor = next;
      }

      const reasons: Record<string, string> = {};
      for (const p of ordered) {
        if (locked.has(p.id)) {
          reasons[p.id] = "Kept in place — you locked this stop.";
          continue;
        }
        const matched = p.tags.find((t) => brief.interests.includes(t));
        reasons[p.id] = matched
          ? `Matches your interest in ${matched}.`
          : p.category === "food" || p.category === "market"
            ? "A place to eat along the way."
            : "Fills out a walkable day.";
      }

      const outdoorCount = ordered.filter((p) => p.outdoor).length;
      const unknownPriceCount = ordered.filter(
        (p) => p.priceEur === null,
      ).length;
      const knownCost = ordered.reduce((sum, p) => sum + (p.priceEur ?? 0), 0);

      const findings: AgentFinding[] = [
        {
          agent: "discovery",
          headline: `Ranked ${places.length} places against ${brief.interests.join(", ") || "your interests"}.`,
          notes: [
            `Picked ${ordered.length} stops — ${outdoorCount} outdoor, ${ordered.length - outdoorCount} indoor.`,
            locked.size
              ? `Kept ${locked.size} locked stop(s) in place.`
              : "No locked stops yet.",
          ],
        },
        {
          agent: "conditions",
          headline: rainTriggered
            ? "Rain expected — favoured indoor stops for the rest of the day."
            : rainHeavy
              ? `${city.name}'s forecast turns wet later in the day.`
              : `${city.name} looks mostly dry today.`,
          notes: [
            rainTriggered
              ? "Outdoor-only stops were deprioritised for this revision."
              : "No weather-driven changes needed yet.",
          ],
        },
        {
          agent: "mobility",
          headline: `Ordered stops for a walkable route from ${city.name}'s centre.`,
          notes: [`Walking limit: ${maxWalk} km.`],
        },
        {
          agent: "budget",
          headline: `Known costs total €${knownCost}${unknownPriceCount ? "+" : ""} for this selection.`,
          notes: [
            brief.budgetEur !== null
              ? knownCost <= brief.budgetEur
                ? `Within your €${brief.budgetEur} budget.`
                : `Above your €${brief.budgetEur} budget — consider dropping a paid stop.`
              : "No budget given yet — using free stops where possible.",
          ],
        },
      ];

      return {
        ok: true,
        orderedPlaceIds: ordered.map((p) => p.id),
        reasons,
        findings,
      };
    },
  );

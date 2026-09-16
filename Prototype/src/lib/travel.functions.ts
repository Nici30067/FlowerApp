import { createServerFn } from "@tanstack/react-start";
import { z } from "zod";

import { CITIES, getCity, placesForCity } from "@/lib/travel/data";
import { fmtTime } from "@/lib/travel/planner";
import { GatewayError, callGatewayJson } from "@/lib/travel/gateway.server";
import type { AgentFinding, Brief } from "@/lib/travel/types";

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

function gatewayMessage(error: unknown): { status: number; message: string } {
  if (error instanceof GatewayError) {
    if (error.status === 402)
      return { status: 402, message: `AI credits are exhausted. ${error.message}` };
    if (error.status === 429)
      return { status: 429, message: "The AI is rate limited right now — try again in a moment." };
    if (error.status === 401 || error.status === 403)
      return { status: error.status, message: `AI access is blocked: ${error.message}` };
    return { status: error.status, message: error.message };
  }
  return { status: 500, message: "The AI call failed unexpectedly." };
}

/* ------------------------------------------------------------------ */
/* 1. Intake — one question at a time until the brief is complete      */
/* ------------------------------------------------------------------ */

const INTAKE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    reply: { type: "string" },
    complete: { type: "boolean" },
    cityId: { type: ["string", "null"] },
    startMin: { type: ["integer", "null"] },
    endMin: { type: ["integer", "null"] },
    budgetEur: { type: ["number", "null"] },
    interests: { type: "array", items: { type: "string" } },
    maxWalkKm: { type: ["number", "null"] },
  },
  required: [
    "reply",
    "complete",
    "cityId",
    "startMin",
    "endMin",
    "budgetEur",
    "interests",
    "maxWalkKm",
  ],
} as const;

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

export const intakeTurn = createServerFn({ method: "POST" })
  .inputValidator((input: unknown) =>
    z.object({ messages: z.array(messageSchema), brief: briefSchema }).parse(input),
  )
  .handler(async ({ data }) => {
    const cityList = CITIES.map((c) => `${c.id} (${c.name}, ${c.country})`).join(", ");
    const system = `You are the intake host of "Travel Companion", a one-day city planner.
Collect exactly six facts: city, start time, end time, budget in euros, interests, and maximum total walking in km.
Supported cities only: ${cityList}. If the user names an unsupported city, say so warmly and offer the supported ones.
Ask ONE question per turn, in one or two short friendly sentences. Never ask for something already known.
Times are minutes from midnight (10:00 = 600). If the user gives no walking limit after you ask once, use 6.
Set complete to true only when city, startMin, endMin, budgetEur and at least one interest are known.
When complete, reply with a one-sentence confirmation and say the specialists are starting.
Carry forward every already-known value unchanged. Never invent prices, travel times or place names.`;

    const input = JSON.stringify({ knownSoFar: data.brief, conversation: data.messages });

    try {
      const out = await callGatewayJson<IntakeResult>({
        system,
        input,
        schemaName: "intake_turn",
        schema: INTAKE_SCHEMA,
      });
      return { ok: true as const, ...out };
    } catch (error) {
      const { status, message } = gatewayMessage(error);
      return { ok: false as const, status, message };
    }
  });

/* ------------------------------------------------------------------ */
/* 2. The four specialists                                             */
/* ------------------------------------------------------------------ */

const SPECIALIST_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    orderedPlaceIds: { type: "array", items: { type: "string" } },
    reasons: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        properties: { placeId: { type: "string" }, reason: { type: "string" } },
        required: ["placeId", "reason"],
      },
    },
    findings: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        properties: {
          agent: { type: "string", enum: ["discovery", "conditions", "mobility", "budget"] },
          headline: { type: "string" },
          notes: { type: "array", items: { type: "string" } },
        },
        required: ["agent", "headline", "notes"],
      },
    },
  },
  required: ["orderedPlaceIds", "reasons", "findings"],
} as const;

interface SpecialistResult {
  orderedPlaceIds: string[];
  reasons: Array<{ placeId: string; reason: string }>;
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
  .handler(async ({ data }) => {
    const brief: Brief = data.brief;
    const cityId = brief.cityId ?? "berlin";
    const city = getCity(cityId);
    const places = placesForCity(cityId);

    const catalogue = places.map((p) => ({
      id: p.id,
      name: p.name,
      category: p.category,
      tags: p.tags,
      outdoor: p.outdoor,
      hoursKnown: p.hours !== null,
      opensAt: p.hours ? fmtTime(p.hours.open) : "unknown",
      closesAt: p.hours ? fmtTime(p.hours.close) : "unknown",
      priceKnown: p.priceEur !== null,
      typicalVisitMin: p.durationMin,
      note: p.blurb,
    }));

    const system = `You are a panel of four travel specialists planning ONE day in ${city?.name ?? cityId}.
- discovery: rank places against the traveller's interests
- conditions: judge the weather and which stops are exposed
- mobility: judge what is sensible on foot within the walking limit
- budget: judge whether the selection fits the money and the hours

HARD RULES
- You may ONLY choose place ids from the catalogue given. Never invent a place.
- You must NOT state any arrival time, travel time, distance in km, or price. The app computes all of those.
- Any fact the catalogue marks unknown must be described as unknown, never estimated.
- Locked places MUST appear in orderedPlaceIds. Completed places MUST NOT appear.
- Order the ids as a sensible walking sequence through the day, 4 to 7 stops, including one food stop when interests or the hours suggest a meal.
- Give one short reason per chosen place, written to the traveller ("you said you like...").
- Return exactly four findings, one per agent, each with a one-line headline and 2-3 short notes.`;

    const input = JSON.stringify({
      brief: {
        ...brief,
        startTime: brief.startMin !== null ? fmtTime(brief.startMin) : "unknown",
        endTime: brief.endMin !== null ? fmtTime(brief.endMin) : "unknown",
      },
      forecastByHour: city?.forecast ?? [],
      lockedPlaceIds: data.lockedIds,
      completedPlaceIds: data.doneIds,
      changeTrigger: data.trigger,
      catalogue,
    });

    try {
      const out = await callGatewayJson<SpecialistResult>({
        system,
        input,
        schemaName: "specialist_panel",
        schema: SPECIALIST_SCHEMA,
      });

      const valid = new Set(places.map((p) => p.id));
      const done = new Set(data.doneIds);
      const orderedPlaceIds = out.orderedPlaceIds.filter((id) => valid.has(id) && !done.has(id));
      const reasons: Record<string, string> = {};
      for (const r of out.reasons) if (valid.has(r.placeId)) reasons[r.placeId] = r.reason;

      return { ok: true as const, orderedPlaceIds, reasons, findings: out.findings };
    } catch (error) {
      const { status, message } = gatewayMessage(error);
      return { ok: false as const, status, message };
    }
  });

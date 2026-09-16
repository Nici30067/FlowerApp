export type Category =
  | "art"
  | "history"
  | "park"
  | "food"
  | "market"
  | "view"
  | "music"
  | "shopping";

export interface OpeningHours {
  /** minutes from midnight */
  open: number;
  close: number;
}

export interface Place {
  id: string;
  cityId: string;
  name: string;
  category: Category;
  tags: string[];
  lat: number;
  lng: number;
  /** null means the price is genuinely unknown — never guess it */
  priceEur: number | null;
  /** typical visit length in minutes; null when unknown */
  durationMin: number | null;
  outdoor: boolean;
  /** null means opening hours are unknown */
  hours: OpeningHours | null;
  blurb: string;
}

export type Sky = "sun" | "cloud" | "rain";

export interface HourForecast {
  hour: number;
  sky: Sky;
  tempC: number;
}

export interface City {
  id: string;
  name: string;
  country: string;
  center: { lat: number; lng: number };
  summary: string;
  forecast: HourForecast[];
}

export interface Brief {
  cityId: string | null;
  startMin: number | null;
  endMin: number | null;
  budgetEur: number | null;
  interests: string[];
  maxWalkKm: number | null;
}

export interface Stop {
  placeId: string;
  kind: "place" | "break";
  name: string;
  arriveMin: number;
  leaveMin: number;
  /** walking distance from the previous stop, km */
  walkKm: number;
  walkMin: number;
  costEur: number | null;
  reason: string;
  locked: boolean;
  done: boolean;
  warnings: string[];
  lat: number;
  lng: number;
  outdoor: boolean;
}

export interface Itinerary {
  revision: number;
  cityId: string;
  stops: Stop[];
  totalWalkKm: number;
  totalCostEur: number;
  unknownCostCount: number;
  endsAtMin: number;
  warnings: string[];
}

export interface AgentFinding {
  agent: "discovery" | "conditions" | "mobility" | "budget";
  headline: string;
  notes: string[];
}

export interface AgentProposal {
  orderedPlaceIds: string[];
  reasons: Record<string, string>;
  findings: AgentFinding[];
}

export type DiffKind = "added" | "removed" | "kept" | "retimed";

export interface DiffRow {
  kind: DiffKind;
  name: string;
  detail: string;
}

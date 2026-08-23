import type {
  ArbOpportunity,
  MonitoredGroup,
  MonitoredLeg,
} from "../api/types";

export const POLY = "polymarket_us";
export const KALSHI = "kalshi";
export const COMPLETED_ASK_THRESHOLD = 0.98;

export const CATEGORY_LABELS: Record<string, string> = {
  atp: "ATP Tennis",
  wta: "WTA Tennis",
  mlb: "MLB",
  nfl: "NFL",
  nba: "NBA",
};

export function categoryOf(group: MonitoredGroup): { id: string; label: string } {
  const prefix = group.id.split("-", 1)[0]?.toLowerCase() ?? "";
  if (prefix && CATEGORY_LABELS[prefix]) return { id: prefix, label: CATEGORY_LABELS[prefix] };
  if (prefix) return { id: prefix, label: prefix.toUpperCase() };
  return { id: "other", label: "Other" };
}

function parseDateSuffix(id: string): Date | null {
  const m = id.match(/(\d{4})-(\d{2})-(\d{2})(?:$|-)/);
  if (!m) return null;
  const [, y, mo, d] = m;
  const dt = new Date(Number(y), Number(mo) - 1, Number(d));
  return Number.isNaN(dt.getTime()) ? null : dt;
}

function todayMidnight(): Date {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

export function isCompleted(group: MonitoredGroup): boolean {
  const dt = parseDateSuffix(group.id);
  if (dt && dt.getTime() < todayMidnight().getTime()) return true;
  for (const leg of group.legs) {
    if (leg.ask == null) continue;
    const n = Number(leg.ask);
    if (Number.isFinite(n) && n >= COMPLETED_ASK_THRESHOLD) return true;
  }
  return false;
}

export function isCrossVenue(group: MonitoredGroup): boolean {
  const venues = new Set(group.legs.map((l) => l.venue_id));
  return venues.has(POLY) && venues.has(KALSHI);
}

export function pickLeg(
  group: MonitoredGroup,
  venue: string,
  isYes: boolean,
): MonitoredLeg | null {
  return (
    group.legs.find((l) => l.venue_id === venue && l.is_yes_side === isYes) ?? null
  );
}

export interface Combo {
  key: "comboA" | "comboB" | "best";
  yesVenue: string;
  noVenue: string;
  yesLeg: MonitoredLeg | null;
  noLeg: MonitoredLeg | null;
  total: number | null;
  edge: number | null;
  favorable: boolean;
}

export function buildCombo(
  group: MonitoredGroup,
  key: "comboA" | "comboB" | "best",
  yesVenue: string,
  noVenue: string,
): Combo {
  const yesLeg = pickLeg(group, yesVenue, true);
  const noLeg = pickLeg(group, noVenue, false);
  const yesAsk = yesLeg?.ask != null ? Number(yesLeg.ask) : null;
  const noAsk = noLeg?.ask != null ? Number(noLeg.ask) : null;
  const total =
    yesAsk != null && noAsk != null && Number.isFinite(yesAsk) && Number.isFinite(noAsk)
      ? yesAsk + noAsk
      : null;
  const edge = total != null ? 1 - total : null;
  return {
    key,
    yesVenue,
    noVenue,
    yesLeg,
    noLeg,
    total,
    edge,
    favorable: total != null && total < 1,
  };
}

// K-Yes / P-No and K-No / P-Yes — labels match the Vantage mockup.
export function buildCombos(group: MonitoredGroup): [Combo, Combo] {
  return [
    buildCombo(group, "comboA", KALSHI, POLY),
    buildCombo(group, "comboB", POLY, KALSHI),
  ];
}

/**
 * Find the live opportunity matching this combo.
 *
 * Returns the opportunity itself rather than its index: this array merges
 * websocket-pushed items ahead of REST ones, so a position here does not
 * address the same entry in the server's list.
 */
export function findOpportunity(
  opps: ArbOpportunity[],
  group: MonitoredGroup,
  combo: Combo,
): ArbOpportunity | null {
  if (!combo.yesLeg || !combo.noLeg) return null;
  const yesOutcome = combo.yesLeg.outcome_id;
  const noOutcome = combo.noLeg.outcome_id;
  for (const o of opps) {
    if (o.event_group_id !== group.id) continue;
    const hasYes = o.legs.some(
      (l) => l.outcome_id === yesOutcome && l.venue_id === combo.yesVenue && l.is_buy,
    );
    const hasNo = o.legs.some(
      (l) => l.outcome_id === noOutcome && l.venue_id === combo.noVenue && l.is_buy,
    );
    if (hasYes && hasNo) return o;
  }
  return null;
}

/** Buy-leg outcome ids of an opportunity — the execution descriptor. */
export function buyOutcomeIds(opp: ArbOpportunity): string[] {
  return opp.legs.filter((l) => l.is_buy).map((l) => l.outcome_id);
}

export type ComboState = "ready" | "no-quotes" | "no-edge" | "waiting";

/** Why a combo can or cannot be filled — drives the button's visible label. */
export function comboState(
  combo: Combo,
  opportunity: ArbOpportunity | null,
): ComboState {
  if (combo.total == null) return "no-quotes";
  if (!combo.favorable) return "no-edge";
  if (opportunity == null) return "waiting";
  return "ready";
}

export const COMBO_STATE_LABEL: Record<ComboState, string> = {
  ready: "execute both legs as a paper order",
  "no-quotes": "no quotes yet on one or both legs",
  "no-edge": "both legs cost more than $1 together — no edge",
  waiting: "edge seen, waiting for the engine to publish it",
};

export const COMBO_STATE_BADGE: Record<ComboState, string> = {
  ready: "",
  "no-quotes": "no quotes",
  "no-edge": "no edge",
  waiting: "waiting",
};

/**
 * Best available start instant for a group, as epoch ms.
 *
 * Prefers the venue-reported `start_time` (exact, to the minute) and falls
 * back to the date embedded in the group id, which has no time-of-day and so
 * only orders games to the day.
 */
export function groupStartDate(group: MonitoredGroup): number | null {
  if (group.start_time) {
    const t = Date.parse(group.start_time);
    if (Number.isFinite(t)) return t;
  }
  const dt = parseDateSuffix(group.id);
  return dt ? dt.getTime() : null;
}

/**
 * Deterministic card ordering so rows never reshuffle between polls.
 *
 * Sorts by start time, then title, then id. The API returns groups in
 * dict-insertion order, which changes as discovery registers new games —
 * that is what made cards jump. Groups with no known start sort last rather
 * than interleaving unpredictably.
 */
export function compareGroups(a: MonitoredGroup, b: MonitoredGroup): number {
  const da = groupStartDate(a);
  const db = groupStartDate(b);
  if (da !== db) {
    if (da == null) return 1;
    if (db == null) return -1;
    return da - db;
  }
  const byTitle = (a.title || "").localeCompare(b.title || "");
  if (byTitle !== 0) return byTitle;
  return a.id.localeCompare(b.id);
}

export interface EventClock {
  /** "upcoming" before start, "live" after, "unknown" with no start time. */
  phase: "upcoming" | "live" | "unknown";
  /** "2h 14m" / "47m" / "in 3d" — short enough for a card line. */
  text: string;
  /** True within an hour of start, for emphasis. */
  imminent: boolean;
}

function humanizeMs(ms: number): string {
  const totalMin = Math.floor(ms / 60_000);
  if (totalMin < 1) return "<1m";
  const days = Math.floor(totalMin / 1440);
  const hours = Math.floor((totalMin % 1440) / 60);
  const mins = totalMin % 60;
  if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

/**
 * Time until (or since) an event's scheduled start.
 *
 * Note this is scheduled time only — neither venue publishes a live clock or
 * score, so once a game is under way "live" is the honest ceiling: we know it
 * started, not how far along it is.
 */
export function eventClock(group: MonitoredGroup, now: number): EventClock {
  const start = group.start_time ? Date.parse(group.start_time) : NaN;
  if (!Number.isFinite(start)) {
    return { phase: "unknown", text: "start time unknown", imminent: false };
  }
  const delta = start - now;
  if (delta > 0) {
    return {
      phase: "upcoming",
      text: `starts in ${humanizeMs(delta)}`,
      imminent: delta <= 3_600_000,
    };
  }
  return {
    phase: "live",
    text: `started ${humanizeMs(-delta)} ago`,
    imminent: false,
  };
}

export function askToCents(ask: string | null | undefined): string {
  if (ask == null) return "—";
  const n = Number(ask);
  if (!Number.isFinite(n)) return "—";
  return String(Math.round(n * 100));
}

export function edgeCentsDisplay(edge: number | null): string {
  if (edge == null) return "—";
  const cents = edge * 100;
  return `${cents >= 0 ? "+" : ""}${cents.toFixed(1)}¢`;
}

export interface BestPair {
  /** The pair the backend chose, shaped as a Combo so findOpportunity() and
   *  comboState() work unchanged. Null when the backend reported no pair. */
  combo: Combo | null;
  /** Both cross-venue combos gross-favorable at once — informational marker.
   *  Needs a venue's own YES+NO to cross, which Polymarket US cannot do
   *  structurally but Kalshi can: measured 1 of 245 groups on 2026-08-22. */
  both: boolean;
  /** From the backend's max_tradeable_qty. 0 = known empty, null = unknown. */
  size: number | null;
}

/**
 * The pair the backend chose, looked up by leg outcome_id.
 *
 * The backend ranks candidate (yes, no) pairs by net absolute profit, which
 * needs fee models this frontend does not have -- and it searches *all*
 * combinations, including same-venue pairs like Kalshi-YES + Kalshi-NO, which
 * buildCombos() never constructs. So the pair cannot be re-derived here; it
 * has to be assembled from the outcome_ids the backend already named.
 */
export function bestPair(group: MonitoredGroup): BestPair {
  const [a, b] = buildCombos(group);
  const both = a.favorable && b.favorable;

  const yesId = group.best_pair_yes_outcome_id;
  const noId = group.best_pair_no_outcome_id;
  if (yesId == null || noId == null) {
    return { combo: null, both, size: null };
  }

  const yesLeg = group.legs.find((l) => l.outcome_id === yesId) ?? null;
  const noLeg = group.legs.find((l) => l.outcome_id === noId) ?? null;
  const yesAsk = yesLeg?.ask != null ? Number(yesLeg.ask) : null;
  const noAsk = noLeg?.ask != null ? Number(noLeg.ask) : null;
  const total =
    yesAsk != null && noAsk != null && Number.isFinite(yesAsk) && Number.isFinite(noAsk)
      ? yesAsk + noAsk
      : null;
  const edge = total != null ? 1 - total : null;
  const combo: Combo = {
    key: "best",
    yesVenue: yesLeg?.venue_id ?? "",
    noVenue: noLeg?.venue_id ?? "",
    yesLeg,
    noLeg,
    total,
    edge,
    favorable: total != null && total < 1,
  };

  // Do not recompute from leg depths -- the backend already applied the
  // three-state depth rule and the ticket budget. null (unknown) and 0
  // (known-empty) are both meaningful and must stay distinct.
  const rawQty = group.max_tradeable_qty;
  let size: number | null = null;
  if (rawQty != null) {
    const n = Number(rawQty);
    size = Number.isFinite(n) ? n : null;
  }

  return { combo, both, size };
}

/** Split "Team A vs Team B — Over 41.5 (2026-09-13)" into its two parts.
 *
 *  Matches the *spaced* em-dash only: a name containing a bare em-dash would
 *  otherwise be cut in half. The trailing date is dropped because the Start
 *  column already carries it.
 */
export function splitTitle(title: string): {
  matchup: string;
  market: string | null;
} {
  const withoutDate = title.replace(/\s*\(\d{4}-\d{2}-\d{2}\)\s*$/, "");
  const idx = withoutDate.indexOf(" — ");
  if (idx === -1) return { matchup: withoutDate.trim(), market: null };
  return {
    matchup: withoutDate.slice(0, idx).trim(),
    market: withoutDate.slice(idx + 3).trim() || null,
  };
}

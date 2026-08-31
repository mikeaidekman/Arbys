/**
 * Aggregation behind the performance dashboard.
 *
 * Pure functions over what the API already returns — no fetching, no React —
 * so the arithmetic can be reasoned about on its own. Two conventions carry
 * through:
 *
 * - **Null is unknown, never zero.** A missed ticket has no economics and a
 *   `Number(null)` of 0 would read as a free ticket that made nothing. Sums
 *   skip nulls, and a total whose every contributor was null stays null.
 * - **Two independent axes.** `status` says what happened at *submission*
 *   (filled / rejected / missed / pending); `outcome` says what happened at
 *   *settlement* (open / won / lost / flat). A rejected ticket never traded,
 *   so it has no settlement outcome at all — that is `"none"`, and it is not
 *   the same thing as breaking even.
 */
import type { PnlSnapshot, Ticket, TicketLeg } from "../api/types";

/** Settlement axis. Orthogonal to `Ticket.status`, which is the submission axis. */
export type Outcome = "won" | "lost" | "flat" | "open" | "none";

export interface RangeOption {
  key: string;
  /** Null means every row, however old. */
  days: number | null;
}

/**
 * `All` leads because the ledger is young. A 90-day window over two tickets
 * renders the same single bar as a 7-day one while implying the other 88 days
 * were flat rather than unrecorded.
 */
export const RANGES: RangeOption[] = [
  { key: "All", days: null },
  { key: "7D", days: 7 },
  { key: "30D", days: 30 },
  { key: "90D", days: 90 },
];

/**
 * Split an event group id into the dimensions worth slicing on.
 *
 * `matcher.py:event_group_id` builds `{sport}-{teamA}-{teamB}-{YYYY-MM-DD}`,
 * appending `-{market_type}-{line}` for anything that is not a moneyline. The
 * ticket freezes that string and never joins back to `event_group`, which is
 * the point — discovery retires groups constantly — so this parse is the only
 * route to sport and market type on a historical row.
 *
 * Anchored on the ISO date rather than on segment counts: team codes vary in
 * width (`KU`, `ALTMAIER`) and the date itself contains two hyphens, so
 * counting segments misreads both ends.
 */
const GROUP_ID =
  /^([a-z0-9]+)-(.+?)-(\d{4}-\d{2}-\d{2})(?:-([a-z0-9_]+)-(.+))?$/;

export interface GroupDims {
  sport: string;
  marketType: string;
  gameDate: string | null;
}

export function parseGroupId(id: string): GroupDims {
  const m = GROUP_ID.exec(id);
  if (m === null) return { sport: "unknown", marketType: "unknown", gameDate: null };
  return {
    sport: m[1],
    // A moneyline group carries no market-type segment at all; that absence
    // is the encoding, not missing data.
    marketType: m[4] ?? "moneyline",
    gameDate: m[3],
  };
}

/** Sum, skipping nulls. Null when nothing contributed. */
function sumOrNull(values: (number | null)[]): number | null {
  let total = 0;
  let seen = false;
  for (const v of values) {
    if (v === null) continue;
    total += v;
    seen = true;
  }
  return seen ? total : null;
}

/** What a leg actually cost: filled size at the filled price, plus its fee. */
function legCost(leg: TicketLeg): number | null {
  if (leg.fill_price === null) return null;
  return Number(leg.qty) * Number(leg.fill_price) + Number(leg.fee);
}

/** What a leg paid back. Null while its outcome carries no settlement row. */
function legReturned(leg: TicketLeg): number | null {
  if (leg.resolved_value === null || leg.fill_price === null) return null;
  return Number(leg.qty) * Number(leg.resolved_value);
}

/**
 * Settlement outcome.
 *
 * `realized_profit` is null while *any* leg is unsettled, so a half-settled
 * ticket is still open — correct for an arb, where one resolved leg says
 * nothing about the pair until its hedge resolves too.
 */
export function ticketOutcome(t: Ticket): Outcome {
  if (t.status === "rejected" || t.status === "missed") return "none";
  if (t.realized_profit === null) return "open";
  const n = Number(t.realized_profit);
  if (n > 0) return "won";
  if (n < 0) return "lost";
  return "flat";
}

export interface LedgerRow {
  id: string;
  submittedAt: string;
  title: string;
  eventGroupId: string;
  sport: string;
  marketType: string;
  /** e.g. "kalshi YES / polymarket us LONG" — venue-native, so both are named. */
  pair: string;
  qty: number | null;
  /** All-in cost of one contract pair, in cents. Null when nothing filled. */
  costCents: number | null;
  capital: number | null;
  /** Taker fees inside `capital`. Broken out because fee drag is the single
   *  largest cost this strategy carries and `capital` hides it. */
  fees: number;
  returned: number | null;
  net: number | null;
  roi: number | null;
  /** Detection-time expected edge per contract pair, in cents. */
  edgeCents: number | null;
  status: Ticket["status"];
  outcome: Outcome;
  rejectionReason: string | null;
  /** When the game starts, and so roughly when this ticket pays out. ISO, or
   *  null when unknown. See `settlementDate` for the display fallback. */
  startsAt: string | null;
  /** What this ticket is worth at settlement, guaranteed: `qty - capital`.
   *
   *  A matched arb pair pays exactly $1 whichever side wins, so the payout is
   *  just the contract count and the profit is fixed at fill time. This is the
   *  meaningful number for a hedged book — mark-to-market unrealized value
   *  moves with quotes that cannot change what the pair settles for.
   *
   *  Null when the legs are *not* matched (different filled quantities, or only
   *  one leg filled). That position is directional, its payout depends on who
   *  wins, and showing a guaranteed value for it would be a lie. */
  settlementValue: number | null;
}

/**
 * A ticket's legs as a readable pair.
 *
 * `outcome_id` is venue-native and not portable, so the venue is always
 * carried alongside it. The suffix after the last colon is the side
 * (`:YES`/`:NO` on Kalshi, `:LONG`/`:SHORT` on Polymarket US); where an id
 * carries no suffix the raw id is shown rather than a guess.
 */
function legPair(legs: TicketLeg[]): string {
  if (legs.length === 0) return "—";
  return legs
    .map((leg) => {
      const venue = leg.venue_id.replace(/_/g, " ");
      const idx = leg.outcome_id.lastIndexOf(":");
      const side = idx === -1 ? leg.outcome_id : leg.outcome_id.slice(idx + 1);
      return `${venue} ${side}`;
    })
    .join(" / ");
}

export function toLedgerRow(t: Ticket): LedgerRow {
  const dims = parseGroupId(t.event_group_id);
  const capital = sumOrNull(t.legs.map(legCost));
  const returned = sumOrNull(t.legs.map(legReturned));
  // Realized profit is authoritative where the backend could compute it: it
  // scores the ticket's own fills against settlement. Falling back to
  // returned − capital would silently report a half-settled ticket as a loss
  // the size of its unsettled leg.
  const net = t.realized_profit === null ? null : Number(t.realized_profit);
  const qty = t.legs.length > 0 ? Number(t.legs[0].qty) : null;
  // Guaranteed settlement value, but only for a genuinely matched pair. Every
  // filled leg must carry the same quantity: a matched pair pays $1 per
  // contract whoever wins, an unmatched one pays 0 or 1 depending, and the
  // whole point of this figure is that it is not a forecast.
  const filledQtys = t.legs
    .filter((l) => l.fill_price !== null)
    .map((l) => Number(l.qty));
  const matched =
    filledQtys.length >= 2 && filledQtys.every((q) => q === filledQtys[0]);
  const settlementValue =
    matched && capital !== null ? filledQtys[0] - capital : null;
  return {
    startsAt: t.starts_at,
    settlementValue,
    id: t.id,
    submittedAt: t.submitted_at,
    title: t.title_snapshot,
    eventGroupId: t.event_group_id,
    sport: dims.sport,
    marketType: dims.marketType,
    pair: legPair(t.legs),
    qty,
    costCents: capital !== null && qty !== null && qty > 0 ? (capital / qty) * 100 : null,
    capital,
    fees: t.legs.reduce((a, l) => a + (l.fill_price === null ? 0 : Number(l.fee)), 0),
    returned,
    net,
    roi: net !== null && capital !== null && capital > 0 ? (net / capital) * 100 : null,
    // Basis points of a $1 payout: 100 bps = 1% = 1¢ per contract pair.
    edgeCents:
      t.expected_edge_bps === null ? null : Number(t.expected_edge_bps) / 100,
    status: t.status,
    outcome: ticketOutcome(t),
    rejectionReason: t.rejection_reason,
  };
}

/** A trailing "(YYYY-MM-DD)" in a title, which every discovery-created group
 *  carries. Used only as a fallback — see `settlementDate`. */
// Sentinel for tickets whose payout day is unknown. Cannot collide with a
// real key, which is always YYYY-MM-DD.
const UNKNOWN_DATE_KEY = "date-unknown";

const TITLE_DATE = /\((\d{4}-\d{2}-\d{2})\)\s*$/;

function isoDay(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** The day a ticket pays out, as YYYY-MM-DD, or null when unknowable.
 *
 *  `startsAt` is authoritative: it is the venue's own kickoff time, frozen onto
 *  the ticket at submit. Falling back to the title's trailing date matters for
 *  every row written before that column existed, which is all of them at the
 *  time of writing. The fallback is deliberately *second*, and degrades to null
 *  rather than to a wrong date if the title format ever changes.
 *
 *  Both resolve to the game's local day. Group dates in this project are
 *  Eastern throughout (see CLAUDE.md), so the two sources agree for an Eastern
 *  viewer and can differ by a day for one far enough away.
 */
export function settlementDate(row: LedgerRow): string | null {
  if (row.startsAt !== null) {
    const d = new Date(row.startsAt);
    if (!Number.isNaN(d.getTime())) return isoDay(d);
  }
  const m = TITLE_DATE.exec(row.title);
  return m === null ? null : m[1];
}

export interface SettlementBucket {
  /** YYYY-MM-DD, or null for the "date unknown" bucket. */
  date: string | null;
  /** Guaranteed profit landing that day. */
  value: number;
  /** Capital that comes back with it. */
  capital: number;
  contracts: number;
  tickets: number;
}

/** Group unsettled tickets by the day they pay out, earliest first.
 *
 *  Only rows with a real `settlementValue` are counted: an unmatched pair has
 *  no guaranteed payout, so putting it on a calendar of guaranteed payouts
 *  would misstate the total. The unknown-date bucket sorts last — it is a gap
 *  in what we know, not a date in the far future. */
export function settlementBuckets(rows: LedgerRow[]): SettlementBucket[] {
  const by = new Map<string, SettlementBucket>();
  for (const r of rows) {
    if (r.settlementValue === null) continue;
    const date = settlementDate(r);
    const key = date ?? UNKNOWN_DATE_KEY;
    const b = by.get(key) ?? {
      date,
      value: 0,
      capital: 0,
      contracts: 0,
      tickets: 0,
    };
    b.value += r.settlementValue;
    b.capital += r.capital ?? 0;
    b.contracts += r.qty ?? 0;
    b.tickets += 1;
    by.set(key, b);
  }
  return [...by.values()].sort((a, b) => {
    if (a.date === null) return 1;
    if (b.date === null) return -1;
    return a.date < b.date ? -1 : a.date > b.date ? 1 : 0;
  });
}

export interface LeagueRow {
  name: string;
  net: number | null;
  capital: number | null;
  tickets: number;
  roi: number | null;
}

export interface VenueRow {
  name: string;
  /** Cost of every filled leg on this venue, settled or not. */
  deployed: number;
  /** Payout of the settled legs only. */
  returned: number;
  /** `returned` less the cost of *those same* legs — never the full deployed. */
  net: number;
  /** Cost still riding on unsettled legs. `deployed = settledCost + open`. */
  open: number;
}

export interface EdgeBucket {
  label: string;
  count: number;
}

export interface MixSlice {
  name: string;
  count: number;
  share: number;
  color: string;
}

export interface CurvePoint {
  ts: string;
  value: number;
}

export interface Dashboard {
  rows: LedgerRow[];
  netProfit: number | null;
  /** Settled net BEFORE fees, i.e. netProfit + feesPaid. The pair is what
   *  makes fee drag legible: net alone cannot show what it cost to earn. */
  grossProfit: number | null;
  /** Fees on settled tickets only, so it reconciles against netProfit rather
   *  than against a different population. */
  feesPaid: number;
  /** Fees as a share of gross. Measured at 62% on 2026-08-29 — the dominant
   *  fact about this strategy and previously not on screen anywhere. */
  feeDragPct: number | null;
  capitalDeployed: number | null;
  capitalReturned: number | null;
  returnOnCapital: number | null;
  hitRate: number | null;
  settledCount: number;
  wonCount: number;
  openExposure: number;
  openCount: number;
  attempted: number;
  filled: number;
  meanEdgeCents: number | null;
  byLeague: LeagueRow[];
  byMarketType: LeagueRow[];
  byVenue: VenueRow[];
  edgeBuckets: EdgeBucket[];
  outcomeMix: MixSlice[];
  bothLegsFilled: number | null;
  medianSlippageCents: number | null;
  curve: CurvePoint[];
}

/**
 * Captured-edge buckets, in cents per contract pair.
 *
 * Deliberately sub-cent, unlike the 3/5/7/9¢ steps the mockup drew. Both
 * venues charge a fee peaking at 1.75¢ and 1.5¢ per contract at even money,
 * and measured gross divergence between them topped out at 2.75¢ — so a real
 * *net* edge lives well inside the first cent. Cent-wide buckets would put
 * every row this system has ever produced in one bar.
 */
const EDGE_BUCKETS: { label: string; lo: number; hi: number }[] = [
  { label: "<0.25¢", lo: 0, hi: 0.25 },
  { label: "0.25–0.5¢", lo: 0.25, hi: 0.5 },
  { label: "0.5–1¢", lo: 0.5, hi: 1 },
  { label: "1–2¢", lo: 1, hi: 2 },
  { label: "2¢+", lo: 2, hi: Infinity },
];

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 === 1 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function groupBy(
  rows: LedgerRow[],
  key: (r: LedgerRow) => string,
): LeagueRow[] {
  const map = new Map<string, LeagueRow>();
  for (const r of rows) {
    const name = key(r);
    let row = map.get(name);
    if (row === undefined) {
      row = { name, net: null, capital: null, tickets: 0, roi: null };
      map.set(name, row);
    }
    row.tickets += 1;
    row.net = sumOrNull([row.net, r.net]);
    row.capital = sumOrNull([row.capital, r.capital]);
  }
  for (const row of map.values()) {
    row.roi =
      row.net !== null && row.capital !== null && row.capital > 0
        ? (row.net / row.capital) * 100
        : null;
  }
  // Rows with no settled economics sort last rather than as zero — unknown is
  // not the same as flat.
  return [...map.values()].sort((a, b) => (b.net ?? -Infinity) - (a.net ?? -Infinity));
}

export function summarize(
  tickets: Ticket[],
  snapshots: PnlSnapshot[],
  rangeDays: number | null,
): Dashboard {
  const cutoff =
    rangeDays === null ? null : Date.now() - rangeDays * 86_400_000;
  const inRange = (iso: string) => cutoff === null || new Date(iso).getTime() >= cutoff;

  const rows = tickets.filter((t) => inRange(t.submitted_at)).map(toLedgerRow);
  const traded = rows.filter((r) => r.status === "filled" || r.status === "pending");
  const settled = rows.filter((r) => r.outcome === "won" || r.outcome === "lost" || r.outcome === "flat");
  const open = rows.filter((r) => r.outcome === "open");

  const netProfit = sumOrNull(settled.map((r) => r.net));
  // Settled scope, matching netProfit. Fees on open tickets are real money
  // already spent, but pairing them with a profit that has not happened yet
  // would produce a drag figure that reconciles against nothing.
  const feesPaid = settled.reduce((a, r) => a + r.fees, 0);
  const grossProfit = netProfit === null ? null : netProfit + feesPaid;
  const capitalDeployed = sumOrNull(traded.map((r) => r.capital));
  const capitalReturned = sumOrNull(settled.map((r) => r.returned));
  const settledCapital = sumOrNull(settled.map((r) => r.capital));
  const openExposure = open.reduce((a, r) => a + (r.capital ?? 0), 0);

  const venues = new Map<string, VenueRow>();
  for (const t of tickets) {
    if (!inRange(t.submitted_at)) continue;
    for (const leg of t.legs) {
      const cost = legCost(leg);
      if (cost === null) continue;
      let v = venues.get(leg.venue_id);
      if (v === undefined) {
        v = {
          name: leg.venue_id.replace(/_/g, " "),
          deployed: 0,
          returned: 0,
          net: 0,
          open: 0,
        };
        venues.set(leg.venue_id, v);
      }
      v.deployed += cost;
      const back = legReturned(leg);
      if (back === null) {
        v.open += cost;
      } else {
        v.returned += back;
        // Net is returned less the cost of the settled legs alone. Netting
        // against `deployed` would book every open leg as a total loss.
        v.net += back - cost;
      }
    }
  }

  const edges = rows.map((r) => r.edgeCents).filter((e): e is number => e !== null);
  const slippage: number[] = [];
  for (const t of tickets) {
    if (!inRange(t.submitted_at)) continue;
    for (const leg of t.legs) {
      if (leg.fill_price === null) continue;
      // Buying above the limit is adverse; selling below it is. The sign
      // convention makes positive mean "worse than asked for" either way.
      const delta = Number(leg.fill_price) - Number(leg.limit_price);
      slippage.push((leg.is_buy ? delta : -delta) * 100);
    }
  }

  const filledTickets = rows.filter((r) => r.status === "filled");
  const bothLegs = filledTickets.filter(
    (r) => r.capital !== null && r.qty !== null,
  ).length;

  const mixDefs: { name: string; match: (r: LedgerRow) => boolean; color: string }[] = [
    { name: "Settled won", match: (r) => r.outcome === "won", color: "var(--vt-green)" },
    { name: "Open", match: (r) => r.outcome === "open", color: "var(--color-accent)" },
    { name: "Settled lost", match: (r) => r.outcome === "lost", color: "var(--vt-red-dark)" },
    { name: "Flat", match: (r) => r.outcome === "flat", color: "var(--color-neutral-400)" },
    // Kept as its own slice on purpose: a ticket that never traded is the
    // measurement that says whether latency work is worth anything, and
    // folding it in with a loss would hide it.
    { name: "Never filled", match: (r) => r.outcome === "none", color: "var(--color-neutral-400)" },
  ];

  // Equity change from the start of the window: cumulative net P&L including
  // open marks, which is what the snapshot series actually measures. Derived
  // from snapshots rather than from the ledger because 30-second snapshots are
  // dense where tickets are sparse.
  const scoped = snapshots.filter((s) => inRange(s.ts));
  const base = scoped.length > 0 ? Number(scoped[0].total_equity) : 0;
  const curve = scoped.map((s) => ({
    ts: s.ts,
    value: Number(s.total_equity) - base,
  }));

  return {
    rows,
    netProfit,
    grossProfit,
    feesPaid,
    feeDragPct:
      grossProfit !== null && grossProfit > 0 ? (feesPaid / grossProfit) * 100 : null,
    capitalDeployed,
    capitalReturned,
    returnOnCapital:
      netProfit !== null && settledCapital !== null && settledCapital > 0
        ? (netProfit / settledCapital) * 100
        : null,
    hitRate: settled.length > 0 ? (settled.filter((r) => r.outcome === "won").length / settled.length) * 100 : null,
    settledCount: settled.length,
    wonCount: settled.filter((r) => r.outcome === "won").length,
    openExposure,
    openCount: open.length,
    attempted: rows.length,
    filled: filledTickets.length,
    meanEdgeCents: edges.length > 0 ? edges.reduce((a, b) => a + b, 0) / edges.length : null,
    byLeague: groupBy(rows, (r) => r.sport.toUpperCase()),
    byMarketType: groupBy(rows, (r) => r.marketType),
    byVenue: [...venues.values()].sort((a, b) => b.deployed - a.deployed),
    edgeBuckets: EDGE_BUCKETS.map((b) => ({
      label: b.label,
      count: edges.filter((e) => e >= b.lo && e < b.hi).length,
    })),
    outcomeMix: mixDefs.map((d) => {
      const count = rows.filter(d.match).length;
      return {
        name: d.name,
        count,
        share: rows.length > 0 ? (count / rows.length) * 100 : 0,
        color: d.color,
      };
    }),
    bothLegsFilled:
      filledTickets.length > 0 ? (bothLegs / filledTickets.length) * 100 : null,
    medianSlippageCents: median(slippage),
    curve,
  };
}

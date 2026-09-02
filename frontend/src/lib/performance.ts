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
import type {
  DimensionRow,
  Performance,
  Ticket,
  TicketLeg,
} from "../api/types";

/** Settlement axis. Orthogonal to `Ticket.status`, which is the submission axis. */
export type Outcome = "won" | "lost" | "flat" | "open" | "none";

export interface RangeOption {
  key: string;
  /** Rolling window in days, measured back from now. Null means every row. */
  days: number | null;
  /**
   * Cut at local midnight today instead of rolling back from now.
   *
   * Deliberately not expressed as `days: 1`. A rolling 24 hours at 8pm still
   * contains most of yesterday evening, which is not what anyone reading
   * "Today" expects — and on a bot that trades an evening slate, yesterday
   * evening is exactly the part that would quietly inflate the figure.
   *
   * Local midnight, matching how the rest of this page reads dates. Group dates
   * are Eastern throughout (see CLAUDE.md), so this agrees with the venues'
   * own trading day for an Eastern viewer.
   */
  sinceMidnight?: boolean;
}

/** The timestamp a range starts at, or null for "everything". */
export function rangeCutoff(range: RangeOption): number | null {
  if (range.sinceMidnight === true) {
    const midnight = new Date();
    midnight.setHours(0, 0, 0, 0);
    return midnight.getTime();
  }
  return range.days === null ? null : Date.now() - range.days * 86_400_000;
}

/**
 * `All` leads because the ledger is young. A 90-day window over two tickets
 * renders the same single bar as a 7-day one while implying the other 88 days
 * were flat rather than unrecorded.
 */
export const RANGES: RangeOption[] = [
  { key: "All", days: null },
  { key: "Today", days: null, sinceMidnight: true },
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

export interface AccrualPointView {
  ts: string;
  total: number;
  settled: number;
}

export interface Dashboard {
  netProfit: number | null;
  /** Settled net BEFORE fees, i.e. netProfit + feesPaid. The pair is what
   *  makes fee drag legible: net alone cannot show what it cost to earn. */
  grossProfit: number | null;
  /** Fees on settled tickets only, so it reconciles against netProfit rather
   *  than against a different population. */
  feesPaid: number;
  /** Fees as a share of gross. Measured at 62% over the full ledger — the
   *  dominant fact about this strategy. */
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
  /** Submission-side tallies over every ticket in the window, rejections
   *  included. These are the ~83% of rows that never traded, and they are a
   *  rate signal rather than ledger entries. */
  byStatus: Record<string, number>;
  rejectionReasons: { reason: string; count: number }[];
  /** Bounds of the data actually present, so the page can state what it holds
   *  instead of implying a 90-day window contains 90 days. */
  firstSubmittedAt: string | null;
  lastSubmittedAt: string | null;
  /** Cumulative guaranteed value from matched pairs. `total` is the whole
   *  window's; `curve` is the series, already downsampled by the server. */
  accrualTotal: number;
  accrualCurve: AccrualPointView[];
}

/** Decimal string to number, preserving null as *unknown*. */
function num(value: string | null): number | null {
  return value === null ? null : Number(value);
}

/**
 * The shape to render before the aggregate has arrived.
 *
 * Every money figure is null — *unknown* — rather than zero, so a page that is
 * still loading never shows "$0.00 net profit" as if it were a measurement.
 * The counts are genuinely zero: nothing has been counted yet.
 */
export function emptyDashboard(): Dashboard {
  return {
    netProfit: null,
    grossProfit: null,
    feesPaid: 0,
    feeDragPct: null,
    capitalDeployed: null,
    capitalReturned: null,
    returnOnCapital: null,
    hitRate: null,
    settledCount: 0,
    wonCount: 0,
    openExposure: 0,
    openCount: 0,
    attempted: 0,
    filled: 0,
    meanEdgeCents: null,
    byLeague: [],
    byMarketType: [],
    byVenue: [],
    edgeBuckets: [],
    outcomeMix: [],
    bothLegsFilled: null,
    medianSlippageCents: null,
    byStatus: {},
    rejectionReasons: [],
    firstSubmittedAt: null,
    lastSubmittedAt: null,
    accrualTotal: 0,
    accrualCurve: [],
  };
}

/** The design system has no profit/loss pair, so these are the terminal's own. */
const MIX_COLORS: Record<string, string> = {
  "Settled won": "var(--vt-green)",
  Open: "var(--color-accent)",
  "Settled lost": "var(--vt-red-dark)",
  Flat: "var(--color-neutral-400)",
  "Never filled": "var(--color-neutral-400)",
};

/**
 * Adapt the aggregate the server computed into the page's view model.
 *
 * This used to be a `summarize(tickets, ...)` that did the arithmetic here,
 * over whatever slice of the ledger one request could carry. That slice was
 * 1000 rows, which at the auto-trader's ~1,500 tickets/day covered **9h38m**
 * — so `All`, `7D`, `30D` and `90D` all rendered the same morning. The
 * arithmetic now lives in `arbys/backend/performance.py`, over every ticket in
 * the window, and this is the rename layer between snake_case JSON and the
 * camelCase the components read. Keep it dumb: anything that computes belongs
 * on the server, where it is not bounded by a page size.
 */
export function fromPerformance(p: Performance): Dashboard {
  return {
    netProfit: num(p.net_profit),
    grossProfit: num(p.gross_profit),
    feesPaid: Number(p.fees_paid),
    feeDragPct: p.fee_drag_pct,
    capitalDeployed: num(p.capital_deployed),
    capitalReturned: num(p.capital_returned),
    returnOnCapital: p.return_on_capital_pct,
    hitRate: p.hit_rate_pct,
    settledCount: p.settled_count,
    wonCount: p.won_count,
    openExposure: Number(p.open_exposure),
    openCount: p.open_count,
    attempted: p.attempted,
    filled: p.filled,
    meanEdgeCents: p.mean_edge_cents,
    byLeague: p.by_league.map(toLeagueRow),
    byMarketType: p.by_market_type.map(toLeagueRow),
    byVenue: p.by_venue.map((v) => ({
      name: v.name,
      deployed: Number(v.deployed),
      returned: Number(v.returned),
      net: Number(v.net),
      open: Number(v.open),
    })),
    edgeBuckets: p.edge_buckets,
    outcomeMix: p.outcome_mix.map((o) => ({
      name: o.name,
      count: o.count,
      share: o.share_pct,
      color: MIX_COLORS[o.name] ?? "var(--color-neutral-400)",
    })),
    bothLegsFilled: p.both_legs_filled_pct,
    medianSlippageCents: p.median_slippage_cents,
    byStatus: p.by_status,
    rejectionReasons: p.rejection_reasons,
    firstSubmittedAt: p.first_submitted_at,
    lastSubmittedAt: p.last_submitted_at,
    accrualTotal: Number(p.accrual_total),
    accrualCurve: p.accrual_curve.map((a) => ({
      ts: a.ts,
      total: Number(a.total),
      settled: Number(a.settled),
    })),
  };
}

function toLeagueRow(r: DimensionRow): LeagueRow {
  return {
    name: r.name,
    tickets: r.tickets,
    net: num(r.net),
    capital: num(r.capital),
    roi: r.roi_pct,
  };
}

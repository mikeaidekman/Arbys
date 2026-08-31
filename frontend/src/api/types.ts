export interface EventGroupLeg {
  outcome_id: string;
  venue_id: string;
  is_yes_side: boolean;
}

export interface EventGroup {
  id: string;
  title: string;
  legs: EventGroupLeg[];
}

export interface MonitoredLeg {
  outcome_id: string;
  venue_id: string;
  is_yes_side: boolean;
  bid: string | null;
  ask: string | null;
  /** Size resting at the quoted price. 0/null means the venue reported no depth. */
  bid_size: string | null;
  ask_size: string | null;
  /** Seconds since this leg last updated. */
  quote_age_s: number | null;
  /** True when the last update is older than the book's threshold; bid/ask are
   *  withheld in that case because the price is no longer tradeable. */
  is_stale: boolean;
}

export interface MonitoredGroup {
  id: string;
  title: string;
  /** Scheduled start of the real-world event, ISO-8601 UTC. Null when unknown. */
  start_time: string | null;
  legs: MonitoredLeg[];
  best_yes_ask: string | null;
  best_yes_venue: string | null;
  best_no_ask: string | null;
  best_no_venue: string | null;
  arb_edge: string | null;
  has_arb: boolean;
  fully_quoted: boolean;
  /** Net profit per contract after both legs' fees. Frequently NEGATIVE, and
   *  that is correct and must be displayed — measured 2026-08-22, 12
   *  gross-positive pairs and 0 net-positive. */
  net_edge: string | null;
  /** Tradeable size for the chosen pair, already capped by the ticket budget. */
  max_tradeable_qty: string | null;
  /** net_edge * max_tradeable_qty. */
  net_max_profit: string | null;
  /** Total stake to open that position. */
  capital_required: string | null;
  /** What the book alone would allow on that same pair, ignoring the
   *  ARBYS_MAX_TICKET_STAKE budget, and the capital it would take. The gap
   *  against max_tradeable_qty is what the cap is holding back. null means
   *  *unknown* (neither leg reported depth), never unlimited. */
  uncapped_qty: string | null;
  uncapped_capital: string | null;
  /** The pair the backend chose, by leg outcome_id. */
  best_pair_yes_outcome_id: string | null;
  best_pair_no_outcome_id: string | null;
}

export interface ArbLeg {
  outcome_id: string;
  venue_id: string;
  is_buy: boolean;
  price: string;
  qty: string;
  fee: string;
}

export interface ArbOpportunity {
  event_group_id: string;
  total_stake: string;
  guaranteed_profit: string;
  guaranteed_profit_bps: string;
  legs: ArbLeg[];
}

export interface PaperAccountSummary {
  account_id: string;
  balances: Record<string, string>;
  positions: Record<string, string>;
  realized_pnl: Record<string, string>;
  /** Live mark-to-market, not the 30s PnL snapshot. */
  cash: string;
  position_value: string;
  equity: string;
  unrealized_pnl: string;
  /** Filled tickets with at least one leg still unsettled. */
  open_ticket_count: number;
}

export interface TicketLeg {
  venue_id: string;
  outcome_id: string;
  is_buy: boolean;
  qty: string;
  limit_price: string;
  /** Null for a leg that never filled. */
  fill_price: string | null;
  fee: string;
  /** Settled value of this leg's outcome, null while unresolved. Per leg, not
   *  per ticket, so capital returned can be split by venue. */
  resolved_value: string | null;
  status: string;
  rejection_reason: string | null;
}

export interface Ticket {
  id: string;
  event_group_id: string;
  /** Frozen at submit time — event groups get retired and deleted. */
  title_snapshot: string;
  source: "manual" | "auto";
  status: "filled" | "rejected" | "missed" | "pending";
  rejection_reason: string | null;
  /** Null on a missed ticket: there were no economics to record. */
  total_stake: string | null;
  expected_profit: string | null;
  expected_edge_bps: string | null;
  submitted_at: string;
  /** When the underlying game starts, frozen at submit time — so roughly when
   *  this ticket pays out. Null means *unknown*: a row written before the
   *  column existed, or a venue that reports no start time. Never "now". */
  starts_at: string | null;
  /** Null while any leg is unsettled. */
  realized_profit: string | null;
  legs: TicketLeg[];
}

export interface PaperPosition {
  venue_id: string;
  outcome_id: string;
  title: string;
  /** Exact grouping key for "all legs of one game". Null when the outcome was
   *  never traded through a ticket and its event group is gone — group on
   *  outcome_id in that case, never on the title string. */
  event_group_id: string | null;
  qty: string;
  avg_price: string;
  mark: string | null;
  unrealized: string;
}

export interface PaperOrder {
  id: string;
  venue_id: string;
  outcome_id: string;
  is_buy: boolean;
  qty: string;
  limit_price: string;
  status: string;
  submitted_at: string;
}

export interface PnlSnapshot {
  ts: string;
  cash: string;
  mtm_positions: string;
  total_equity: string;
}

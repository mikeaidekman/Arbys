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

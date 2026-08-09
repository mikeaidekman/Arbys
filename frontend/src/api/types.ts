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

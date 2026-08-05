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

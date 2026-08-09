import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ArbOpportunity, MonitoredGroup, MonitoredLeg } from "../api/types";
import type { PriceMove } from "../hooks/usePriceMoves";
import {
  COMBO_STATE_BADGE,
  COMBO_STATE_LABEL,
  askToCents,
  buildCombos,
  buyOutcomeIds,
  comboState,
  edgeCentsDisplay,
  eventClock,
  findOpportunity,
  type Combo,
} from "../lib/combo";
import { api } from "../api/client";

interface Props {
  group: MonitoredGroup;
  categoryLabel: string;
  opportunities: ArbOpportunity[];
  filledCombo: "comboA" | "comboB" | null;
  onFilled: (groupId: string, combo: "comboA" | "comboB") => void;
  priceMoves: Map<string, PriceMove>;
  /** Shared page ticker, so every card's countdown advances together. */
  now: number;
}

function LegPrice({
  label,
  leg,
  moves,
}: {
  label: string;
  leg: MonitoredLeg | null | undefined;
  moves: Map<string, PriceMove>;
}) {
  const move = leg ? moves.get(leg.outcome_id) : undefined;
  const depth = depthAtAsk(leg);

  // A stale leg has no tradeable price, so say so rather than showing "—",
  // which would read as "no data" when in fact the feed went quiet.
  if (leg?.is_stale) {
    const mins = leg.quote_age_s != null ? Math.round(leg.quote_age_s / 60) : null;
    return (
      <span className="vt-mono vt-stale" title={`no update for ${mins ?? "?"}m — not tradeable`}>
        {label} stale{mins != null ? ` ${mins}m` : ""}
      </span>
    );
  }

  return (
    <span className="vt-mono">
      {label}{" "}
      <span className={move ? "vt-move" : undefined}>
        {askToCents(leg?.ask ?? null)}¢
        {move && (
          <span className="vt-move-delta">
            {move.dir === "up" ? "▲" : "▼"}
            {Math.abs(move.delta * 100).toFixed(0)}
          </span>
        )}
      </span>
      {/* Depth at that price. The quote only holds for this much size. */}
      <span
        className="vt-depth"
        title={
          depth == null
            ? "venue reported no depth for this level"
            : `${depth} available at the quoted ask`
        }
      >
        {depth == null ? "×?" : `×${formatDepth(depth)}`}
      </span>
    </span>
  );
}

/** Size available at the ask, or null when the venue reported none. */
function depthAtAsk(leg: MonitoredLeg | null | undefined): number | null {
  if (!leg || leg.ask_size == null) return null;
  const n = Number(leg.ask_size);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n;
}

function formatDepth(n: number): string {
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1_000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n));
}

export function OpportunityCard({
  group,
  categoryLabel,
  opportunities,
  filledCombo,
  onFilled,
  priceMoves,
  now,
}: Props) {
  const [a, b] = buildCombos(group);
  const isArb = a.favorable || b.favorable;
  const clock = eventClock(group, now);
  const polyLeg = group.legs.find((l) => l.venue_id === "polymarket" && l.is_yes_side);
  const kalshiLeg = group.legs.find((l) => l.venue_id === "kalshi" && l.is_yes_side);

  return (
    <div
      className={`card blueprint vt-card ${isArb ? "vt-arb" : ""}`}
      style={{ padding: "var(--space-3)", gap: 6 }}
    >
      <i className="corner tl" />
      <i className="corner tr" />
      <i className="corner bl" />
      <i className="corner br" />

      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: "var(--color-accent)",
            animation: "vt-pulse 1.8s ease-in-out infinite",
            flex: "none",
          }}
        />
        <span className="tag tag-neutral" style={{ fontSize: 10 }}>
          {categoryLabel}
        </span>
      </div>

      <div
        title={group.title}
        style={{
          fontWeight: 500,
          fontSize: 13,
          lineHeight: 1.25,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {group.title}
      </div>

      <div
        title={
          group.start_time
            ? new Date(group.start_time).toLocaleString()
            : "no scheduled start reported by either venue"
        }
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          fontSize: 11,
          opacity: clock.phase === "unknown" ? 0.45 : 0.75,
          fontWeight: clock.imminent || clock.phase === "live" ? 600 : 400,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {clock.phase === "live" && (
          <span
            style={{
              width: 5,
              height: 5,
              borderRadius: "50%",
              background: "var(--color-accent)",
              animation: "vt-pulse 1.2s ease-in-out infinite",
              flex: "none",
            }}
          />
        )}
        <span className="vt-mono">{clock.text}</span>
      </div>

      <div style={{ display: "flex", gap: "var(--space-3)", fontSize: 11, padding: "2px 0" }}>
        <LegPrice label="Poly" leg={polyLeg} moves={priceMoves} />
        <LegPrice label="Kalshi" leg={kalshiLeg} moves={priceMoves} />
      </div>

      {filledCombo ? (
        <div className="vt-filled">
          ✓ {filledCombo === "comboA" ? "K-Yes / P-No" : "K-No / P-Yes"} filled
        </div>
      ) : (
        <div style={{ display: "flex", gap: 5 }}>
          <ComboButton
            label="K-Yes / P-No"
            group={group}
            combo={a}
            opportunities={opportunities}
            onFilled={() => onFilled(group.id, "comboA")}
          />
          <ComboButton
            label="K-No / P-Yes"
            group={group}
            combo={b}
            opportunities={opportunities}
            onFilled={() => onFilled(group.id, "comboB")}
          />
        </div>
      )}
    </div>
  );
}

function ComboButton({
  label,
  group,
  combo,
  opportunities,
  onFilled,
}: {
  label: string;
  group: MonitoredGroup;
  combo: Combo;
  opportunities: ArbOpportunity[];
  onFilled: () => void;
}) {
  const qc = useQueryClient();
  const opportunity = combo.favorable
    ? findOpportunity(opportunities, group, combo)
    : null;
  const state = comboState(combo, opportunity);
  const executable = state === "ready";

  const exec = useMutation({
    mutationFn: () => {
      if (opportunity == null) throw new Error("no matching opportunity");
      return api.executeArb(opportunity.event_group_id, buyOutcomeIds(opportunity));
    },
    onSuccess: () => {
      onFilled();
      qc.invalidateQueries({ queryKey: ["opps"] });
      qc.invalidateQueries({ queryKey: ["paper"] });
    },
  });

  const badge = COMBO_STATE_BADGE[state];
  const failed = exec.isError;

  return (
    <button
      type="button"
      className={`btn vt-combo ${executable ? "vt-combo-active" : "btn-secondary"}`}
      disabled={!executable || exec.isPending}
      onClick={() => exec.mutate()}
      title={failed ? `execution failed: ${exec.error?.message ?? ""}` : COMBO_STATE_LABEL[state]}
    >
      <span>{label}</span>
      {/* A bare disabled button reads as broken — always say why. */}
      <span className="vt-mono" style={{ fontWeight: 600 }}>
        {failed
          ? "failed"
          : exec.isPending
          ? "…"
          : executable
          ? edgeCentsDisplay(combo.edge)
          : badge}
      </span>
    </button>
  );
}

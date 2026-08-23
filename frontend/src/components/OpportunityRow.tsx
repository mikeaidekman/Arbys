import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ArbOpportunity, MonitoredGroup } from "../api/types";
import type { PriceMove } from "../hooks/usePriceMoves";
import { api } from "../api/client";
import {
  askToCents,
  bestPair,
  buyOutcomeIds,
  categoryOf,
  comboState,
  eventClock,
  findOpportunity,
  KALSHI,
  splitTitle,
} from "../lib/combo";

interface Props {
  group: MonitoredGroup;
  opportunities: ArbOpportunity[];
  filled: boolean;
  onFilled: (groupId: string) => void;
  priceMoves: Map<string, PriceMove>;
  now: number;
}

function fmtQty(n: number | null): string {
  if (n == null) return "?";
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1_000) return `${(n / 1000).toFixed(1)}k`;
  return String(Math.round(n * 100) / 100);
}

function fmtCents(v: string | null): string {
  if (v == null) return "—";
  const n = Number(v) * 100;
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)}¢`;
}

function fmtUsd(v: string | null): string {
  if (v == null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n < 0 ? "−" : ""}$${Math.abs(n).toFixed(2)}`;
}

export function OpportunityRow({
  group,
  opportunities,
  filled,
  onFilled,
  priceMoves,
  now,
}: Props) {
  const qc = useQueryClient();
  const pair = bestPair(group);
  const { matchup, market } = splitTitle(group.title);
  const clock = eventClock(group, now);
  const cat = categoryOf(group);

  // The stripe stays GROSS of fees, matching the card's green outline. It is a
  // venue-divergence signal in its own right and is deliberately not the same
  // test as whether the button is enabled. See CLAUDE.md.
  const grossArb = pair.combo?.favorable ?? false;
  const stale =
    pair.combo?.yesLeg?.is_stale === true || pair.combo?.noLeg?.is_stale === true;
  const noSize = pair.size === 0;

  const opportunity =
    pair.combo && !stale && !noSize
      ? findOpportunity(opportunities, group, pair.combo)
      : null;
  const state = pair.combo ? comboState(pair.combo, opportunity) : "no-quotes";

  const exec = useMutation({
    mutationFn: () => {
      if (opportunity == null) throw new Error("no matching opportunity");
      return api.executeArb(opportunity.event_group_id, buyOutcomeIds(opportunity));
    },
    onSuccess: () => {
      onFilled(group.id);
      qc.invalidateQueries({ queryKey: ["opps"] });
      qc.invalidateQueries({ queryKey: ["paper"] });
    },
  });

  const pairLabel = (() => {
    if (!pair.combo) return "—";
    const yesTag = pair.combo.yesVenue === KALSHI ? "K-Yes" : "P-Yes";
    const noTag = pair.combo.noVenue === KALSHI ? "K-No" : "P-No";
    return `${yesTag} ${askToCents(pair.combo.yesLeg?.ask ?? null)} + ${noTag} ${askToCents(
      pair.combo.noLeg?.ask ?? null,
    )}`;
  })();

  const moved =
    (pair.combo?.yesLeg && priceMoves.get(pair.combo.yesLeg.outcome_id)) ||
    (pair.combo?.noLeg && priceMoves.get(pair.combo.noLeg.outcome_id));

  return (
    <tr className={grossArb ? "vt-row-arb" : undefined}>
      <td>
        <span
          className="vt-dot"
          style={
            clock.phase === "live"
              ? { animation: "vt-pulse 1.2s ease-in-out infinite" }
              : undefined
          }
        />
      </td>
      <td>
        <span className="tag tag-neutral vt-cat">{cat.label}</span>
      </td>
      <td className="vt-ellipsis" title={group.title}>
        {matchup}
      </td>
      <td className="vt-muted">{market ?? "—"}</td>
      <td
        className="vt-mono"
        title={
          group.start_time
            ? new Date(group.start_time).toLocaleString()
            : "no scheduled start reported by either venue"
        }
      >
        {clock.text}
      </td>
      <td className={`vt-mono ${stale ? "vt-stale" : ""} ${moved ? "vt-move" : ""}`}>
        {pairLabel}
      </td>
      <td className={`vt-mono vt-num ${noSize ? "vt-size-zero" : ""}`}>
        {fmtQty(pair.size)}
      </td>
      <td
        className="vt-mono vt-num"
        title={
          pair.combo?.edge != null
            ? `gross ${(pair.combo.edge * 100).toFixed(1)}¢ before fees`
            : undefined
        }
      >
        {stale ? (
          <span className="vt-stale">stale</span>
        ) : (
          <span
            className={
              group.net_edge != null && Number(group.net_edge) > 0
                ? "vt-edge-pos"
                : "vt-muted"
            }
          >
            {fmtCents(group.net_edge)}
            {pair.both && (
              <span
                className="vt-both"
                title="both combos favorable — a venue's own book is crossed"
              >
                *
              </span>
            )}
          </span>
        )}
      </td>
      <td className="vt-mono vt-num" title={`capital ${fmtUsd(group.capital_required)}`}>
        {fmtUsd(group.net_max_profit)}
      </td>
      <td>
        {filled ? (
          <span className="vt-filled-inline">✓ filled</span>
        ) : (
          <button
            type="button"
            className={`btn vt-fill ${state === "ready" ? "vt-fill-active" : "btn-secondary"}`}
            disabled={state !== "ready" || noSize || stale || exec.isPending}
            onClick={() => exec.mutate()}
            title={
              exec.isError
                ? `execution failed: ${exec.error?.message ?? ""}`
                : noSize
                  ? "nothing resting on one leg — the broker would reject this"
                  : undefined
            }
          >
            {exec.isError
              ? "failed"
              : exec.isPending
                ? "…"
                : noSize
                  ? "no size"
                  : state === "ready"
                    ? "Fill"
                    : state === "waiting"
                      ? "waiting"
                      : "—"}
          </button>
        )}
      </td>
    </tr>
  );
}

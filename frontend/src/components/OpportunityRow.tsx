import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ArbOpportunity, MonitoredGroup, MonitoredLeg } from "../api/types";
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

/** Contracts as #,###.## — grouped and always two decimals.
 *
 *  Deliberately not abbreviated. These columns exist to show *magnitude*, and
 *  the old "224k" collapsed 224,111.10 and 224,499 to the same four
 *  characters — exactly the distinction the Book column is there to draw. The
 *  fractional part is load-bearing too: contracts are not whole units on
 *  these venues (DEFAULT_QTY_TICK is 0.01, and both venues report fractional
 *  ask_size), so trailing digits are real size rather than noise. */
function fmtQty(n: number | null): string {
  if (n == null) return "?";
  if (!Number.isFinite(n)) return "?";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
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

function numOrNull(v: string | null | undefined): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * One leg of the best pair: its tag, its ask in cents, and — only if that
 * particular ask just moved — a ▲/▼ glyph with the size of the move.
 *
 * The highlight belongs on the price that moved, not on the whole cell: a cell
 * holds two independent asks and marking both misattributes the move. Direction
 * is readable from the glyph alone, so no red/green pair is needed and the
 * existing --color-accent token carries the flash.
 */
function PairLegPrice({
  tag,
  leg,
  move,
}: {
  tag: string;
  leg: MonitoredLeg | null;
  move: PriceMove | undefined;
}) {
  return (
    <>
      {tag}{" "}
      <span className={move ? "vt-move" : undefined}>
        {askToCents(leg?.ask ?? null)}
        {move && (
          <span
            className="vt-move-delta"
            title={`ask moved ${move.dir === "up" ? "up" : "down"} ${Math.abs(
              move.delta * 100,
            ).toFixed(1)}¢`}
          >
            {move.dir === "up" ? "▲" : "▼"}
            {Math.abs(move.delta * 100).toFixed(0)}
          </span>
        )}
      </span>
    </>
  );
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
  const uncapped = numOrNull(group.uncapped_qty);
  // Highlighted only when the cap is actually binding — otherwise the two
  // columns agree and the second is just noise.
  const capped = uncapped != null && pair.size != null && uncapped > pair.size;

  const opportunity =
    pair.combo && !stale && !noSize
      ? findOpportunity(opportunities, group, pair.combo)
      : null;
  // Net, not gross: a gross-favorable row whose fees eat the edge is
  // permanently "no edge", never "waiting" for a publication that cannot come.
  const netEdge = numOrNull(group.net_edge);
  const state = pair.combo
    ? comboState(pair.combo, opportunity, netEdge)
    : "no-quotes";

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

  const yesTag = pair.combo?.yesVenue === KALSHI ? "K-Yes" : "P-Yes";
  const noTag = pair.combo?.noVenue === KALSHI ? "K-No" : "P-No";
  const yesMove = pair.combo?.yesLeg
    ? priceMoves.get(pair.combo.yesLeg.outcome_id)
    : undefined;
  const noMove = pair.combo?.noLeg
    ? priceMoves.get(pair.combo.noLeg.outcome_id)
    : undefined;

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
      <td className={`vt-mono ${stale ? "vt-stale" : ""}`}>
        {pair.combo ? (
          <>
            <PairLegPrice tag={yesTag} leg={pair.combo.yesLeg} move={yesMove} />
            {" + "}
            <PairLegPrice tag={noTag} leg={pair.combo.noLeg} move={noMove} />
          </>
        ) : (
          "—"
        )}
      </td>
      <td className={`vt-mono vt-num ${noSize ? "vt-size-zero" : ""}`}>
        {fmtQty(pair.size)}
      </td>
      {/* What the book would allow if ARBYS_MAX_TICKET_STAKE were not
          binding. Shown beside Size so the gap between them is the cap. */}
      <td
        className={`vt-mono vt-num ${capped ? "" : "vt-muted"}`}
        title={
          uncapped == null
            ? "neither leg reported depth — size unknown, not unlimited"
            : capped
              ? `book holds ${fmtQty(uncapped)} (${fmtUsd(group.uncapped_capital)}); ` +
                `the ticket cap is limiting this to ${fmtQty(pair.size)}`
              : "the whole book is within the ticket cap"
        }
      >
        {fmtQty(uncapped)}
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

import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ArbOpportunity, MonitoredGroup } from "../api/types";
import {
  askToCents,
  buildCombos,
  edgeCentsDisplay,
  findOpportunityIndex,
  type Combo,
} from "../lib/combo";
import { api } from "../api/client";

interface Props {
  group: MonitoredGroup;
  categoryLabel: string;
  opportunities: ArbOpportunity[];
  filledCombo: "comboA" | "comboB" | null;
  onFilled: (groupId: string, combo: "comboA" | "comboB") => void;
}

export function OpportunityCard({
  group,
  categoryLabel,
  opportunities,
  filledCombo,
  onFilled,
}: Props) {
  const [a, b] = buildCombos(group);
  const isArb = a.favorable || b.favorable;
  const marketLabel = group.legs[0]?.outcome_id ?? group.id;
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
        title={marketLabel}
        style={{
          fontSize: 11,
          opacity: 0.6,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {marketLabel}
      </div>

      <div style={{ display: "flex", gap: "var(--space-3)", fontSize: 11, padding: "2px 0" }}>
        <span className="vt-mono">Poly {askToCents(polyLeg?.ask ?? null)}¢</span>
        <span className="vt-mono">Kalshi {askToCents(kalshiLeg?.ask ?? null)}¢</span>
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
  const oppIndex = combo.favorable
    ? findOpportunityIndex(opportunities, group, combo)
    : null;
  const executable = combo.favorable && oppIndex != null;

  const exec = useMutation({
    mutationFn: () => {
      if (oppIndex == null) throw new Error("no matching opportunity");
      return api.executeArb(oppIndex);
    },
    onSuccess: () => {
      onFilled();
      qc.invalidateQueries({ queryKey: ["opps"] });
      qc.invalidateQueries({ queryKey: ["paper"] });
    },
  });

  return (
    <button
      type="button"
      className={`btn vt-combo ${executable ? "vt-combo-active" : "btn-secondary"}`}
      disabled={!executable || exec.isPending}
      onClick={() => exec.mutate()}
      title={
        !combo.favorable
          ? "no edge"
          : oppIndex == null
          ? "waiting for engine"
          : "execute both legs as a paper order"
      }
    >
      <span>{label}</span>
      <span className="vt-mono" style={{ fontWeight: 600 }}>
        {edgeCentsDisplay(combo.edge)}
      </span>
    </button>
  );
}

import type { MonitoredGroup } from "../api/types";
import { categoryOf } from "../lib/combo";

interface Props {
  groups: MonitoredGroup[];
  sportFilter: string;
  onSportChange: (value: string) => void;
  arbOnly: boolean;
  onToggleArbOnly: () => void;
  venues: string[];
}

/**
 * Category and filter selection, as a horizontal pill row above the table.
 *
 * Was a 190px left sidebar. Horizontal because the opportunity table is the
 * widest thing on the screen and wants every pixel: eleven columns at 12px
 * were being squeezed for a rail whose content is a short list of league
 * names. A pill row costs one line of vertical space and gives the table back
 * 190px.
 *
 * The `venues` prop is still accepted and deliberately not rendered: the nav
 * already carries a connection tag per venue, and the same list twice on one
 * screen is noise. Kept in the signature because the caller computes it and a
 * venue *filter* is the obvious next thing to live here.
 */
export function CategoryRail({
  groups,
  sportFilter,
  onSportChange,
  arbOnly,
  onToggleArbOnly,
}: Props) {
  const counts = new Map<string, { label: string; n: number }>();
  for (const g of groups) {
    const { id, label } = categoryOf(g);
    const seen = counts.get(id);
    counts.set(id, { label, n: (seen?.n ?? 0) + 1 });
  }
  const options = [
    { id: "All", label: "All sports", n: groups.length },
    // Busiest league first: the count is the reason to click, so ordering by
    // it puts the likely target under the cursor. Alphabetical ordering had
    // UFC (7 groups) sitting above NFL (248).
    ...Array.from(counts.entries())
      .map(([id, v]) => ({ id, label: v.label, n: v.n }))
      .sort((a, b) => b.n - a.n || a.label.localeCompare(b.label)),
  ];

  return (
    <div
      className="vt-scroll"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 4,
        padding: "0 var(--space-4) var(--space-3)",
        flex: "none",
        flexWrap: "wrap",
      }}
    >
      {options.map((opt) => {
        const active = sportFilter === opt.id;
        return (
          <button
            key={opt.id}
            type="button"
            className={active ? "vt-pill vt-pill-on" : "vt-pill"}
            onClick={() => onSportChange(opt.id)}
          >
            {opt.label}
            <span className="vt-pill-n">{opt.n}</span>
          </button>
        );
      })}

      <span style={{ flex: 1 }} />

      {/* Separated from the league pills by the spacer: it is a different kind
          of choice — a predicate, not a slice — and sitting it in the same run
          made it look like an eighth league. */}
      <button
        type="button"
        className={arbOnly ? "vt-pill vt-pill-on" : "vt-pill"}
        onClick={onToggleArbOnly}
        title="Only groups whose two best asks sum under a dollar, gross of fees"
      >
        Gross arbs only
      </button>
    </div>
  );
}

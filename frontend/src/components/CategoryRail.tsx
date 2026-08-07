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

export function CategoryRail({
  groups,
  sportFilter,
  onSportChange,
  arbOnly,
  onToggleArbOnly,
  venues,
}: Props) {
  const cats = new Map<string, string>();
  for (const g of groups) {
    const { id, label } = categoryOf(g);
    cats.set(id, label);
  }
  const options = [
    { id: "All", label: "All sports" },
    ...Array.from(cats.entries())
      .map(([id, label]) => ({ id, label }))
      .sort((a, b) => a.label.localeCompare(b.label)),
  ];

  return (
    <aside
      className="vt-scroll"
      style={{
        borderRight: "1px solid var(--color-divider)",
        padding: "var(--space-4)",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-4)",
        overflowY: "auto",
      }}
    >
      <div>
        <div style={sectionLabelStyle}>Category</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {options.map((opt) => {
            const active = sportFilter === opt.id;
            return (
              <button
                key={opt.id}
                type="button"
                className="btn btn-ghost"
                style={{
                  justifyContent: "flex-start",
                  width: "100%",
                  fontWeight: 400,
                  color: active ? "var(--color-bg)" : "var(--color-text)",
                  background: active ? "var(--color-accent)" : "transparent",
                }}
                onClick={() => onSportChange(opt.id)}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="hr" style={{ margin: 0 }} />

      <div>
        <div style={sectionLabelStyle}>Filters</div>
        <label className="radio" style={{ fontSize: 13 }}>
          <input
            type="checkbox"
            checked={arbOnly}
            onChange={onToggleArbOnly}
            style={{ width: 14, height: 14, position: "static", opacity: 1 }}
          />
          Arbs only
        </label>
      </div>

      <div className="hr" style={{ margin: 0 }} />

      <div>
        <div style={sectionLabelStyle}>Venues</div>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
            fontSize: 13,
            opacity: 0.8,
          }}
        >
          {venues.map((v) => (
            <span key={v} style={{ textTransform: "capitalize" }}>
              {v}
            </span>
          ))}
        </div>
      </div>
    </aside>
  );
}

const sectionLabelStyle = {
  fontSize: 11,
  letterSpacing: "0.08em",
  textTransform: "uppercase" as const,
  opacity: 0.55,
  marginBottom: "var(--space-2)",
};

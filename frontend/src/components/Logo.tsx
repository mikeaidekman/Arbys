/**
 * The Arbys lockup: mark plus wordmark.
 *
 * Two offset bars inside a rounded square — the same price quoted on two
 * venues, at different levels. That gap is the whole product, so the mark is
 * literally the thing being detected.
 *
 * Built from divs rather than an SVG because that is how the artwork was
 * authored, and at these sizes there is nothing to gain from re-tracing it as
 * paths. Geometry below is the 42px reference lockup scaled by 24/42, with
 * every value rounded to a whole pixel so nothing lands on a half-pixel and
 * blurs: mark 42→24, bars 16×6→9×3, bar gap 6→3, radius 7→4, wordmark 31→18,
 * tracking 2.2→1.3, lockup gap 13→7.
 *
 * Colours are `--brand-*`, deliberately NOT the UI palette. `--brand-navy`
 * happens to equal `--color-accent-800` today and `--brand-signal` is close to
 * `--vt-red-dark`, but a logo must not move when the theme is re-skinned — the
 * coincidence is that the palette was derived from the same source, not a
 * dependency worth encoding.
 */
export function Logo({ size = 24 }: { size?: number }) {
  const s = size / 24;
  const px = (n: number) => Math.round(n * s);

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: px(7),
      }}
      aria-label="Arbys"
    >
      <span
        aria-hidden="true"
        style={{
          width: size,
          height: size,
          flex: "none",
          borderRadius: px(4),
          background: "var(--brand-navy)",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: px(3),
          padding: `0 ${px(5)}px`,
        }}
      >
        {/* The two bars are the mark. `align-self` is what offsets them —
            same width, opposite edges, which is what reads as a spread
            rather than as two unrelated dashes. */}
        <span
          style={{
            width: px(9),
            height: px(3),
            borderRadius: Math.max(1, px(1)),
            background: "var(--color-surface)",
            alignSelf: "flex-start",
          }}
        />
        <span
          style={{
            width: px(9),
            height: px(3),
            borderRadius: Math.max(1, px(1)),
            background: "var(--brand-signal)",
            alignSelf: "flex-end",
          }}
        />
      </span>
      <span
        style={{
          fontFamily: "var(--font-body)",
          fontSize: px(18),
          fontWeight: 700,
          letterSpacing: `${(1.3 * s).toFixed(2)}px`,
          lineHeight: 1,
          color: "var(--brand-navy)",
        }}
      >
        ARBYS
      </span>
    </span>
  );
}

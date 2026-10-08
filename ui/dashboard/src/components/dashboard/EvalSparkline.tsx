import { relativeTime } from '../../lib/time';

export interface EvalHistoryEntry {
  eval_id: string;
  suite_name: string;
  passed: boolean;
  score: number | null;
  run_at: string;
}

interface EvalSparklineProps {
  history: EvalHistoryEntry[];
  maxBars?: number;
}

export default function EvalSparkline({ history, maxBars = 10 }: EvalSparklineProps) {
  if (history.length === 0) return null;

  // API returns newest-first; slice to maxBars then reverse to oldest-left order
  const recent = history.slice(0, maxBars).reverse();
  // Pad left with null placeholders so chart always fills maxBars slots
  const padded: (EvalHistoryEntry | null)[] = [
    ...Array<null>(Math.max(0, maxBars - recent.length)).fill(null),
    ...recent,
  ];

  const barW = 8;
  const gap = 2;
  const svgW = maxBars * (barW + gap) - gap;
  const svgH = 32;

  return (
    <svg
      width={svgW}
      height={svgH}
      aria-label="Eval trend sparkline"
      style={{ display: 'block', flexShrink: 0 }}
    >
      {padded.map((entry, i) => {
        const x = i * (barW + gap);
        // var(--color-bg-raised) for no-data: matches the neutral surface in the asset table
        let fill = 'var(--color-bg-raised)';
        let titleText = 'No data';
        if (entry) {
          fill = entry.passed
            ? 'var(--color-status-success)'
            : 'var(--color-status-failure)';
          titleText = `Suite: ${entry.suite_name} · ${entry.passed ? 'Pass' : 'Fail'} · ${relativeTime(entry.run_at)}`;
        }
        return (
          <rect key={i} x={x} y={0} width={barW} height={svgH} fill={fill} rx={1}>
            <title>{titleText}</title>
          </rect>
        );
      })}
    </svg>
  );
}

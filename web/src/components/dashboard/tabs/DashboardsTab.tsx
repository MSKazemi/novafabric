/**
 * Dashboards tab — ADR-0235 portable widget/dashboard files and ADR-0236
 * ratio() metrics, with a validate-then-save editor. The view itself lives in ../dashboards/.
 */
import TabShell from './TabShell';
import DashboardsView from '../dashboards/DashboardsView';

export default function DashboardsTab({ refreshTick = 0 }: { refreshTick?: number }) {
  return (
    <TabShell
      title="Dashboards"
      icon="dashboards"
      subtitle="Widgets and dashboards as portable JSON files — experimental (ADR-0235, ADR-0236)."
      cli={[
        'nova dashboard list',
        'nova dashboard show <id>',
        'nova dashboard export <id>',
        'nova dashboard validate <file>',
        'nova dashboard apply <file>',
      ]}
      help={
        <>
          Each widget is a versioned JSON file holding a query and how to draw it; a dashboard
          references widgets by id. Files are validated before use, and a refused file is listed
          with its reason. Values with no measurement — e.g. a ratio over a zero denominator —
          show as &ldquo;no value&rdquo;, never as 0. Add or edit one document here (the server validates
          it, you review a diff, then save — operate scope), or install files in bulk with{' '}
          <code className="font-mono">nova dashboard apply</code>.
        </>
      }
    >
      <DashboardsView refreshTick={refreshTick} />
    </TabShell>
  );
}

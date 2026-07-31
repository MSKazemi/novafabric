/**
 * Semantic icon wrapper over lucide-react (ISC, Tier-A per ADR-0024).
 *
 * All call sites use semantic names, so the icon library is swappable in one
 * file. Lucide tree-shakes to only the icons imported here (~0.5 KB each).
 */
import type { ComponentType, SVGProps } from 'react';
import {
  Activity,
  AlertTriangle,
  Anchor,
  Archive,
  ArrowUpRight,
  BadgeCheck,
  BarChart3,
  Bell,
  Boxes,
  Camera,
  ChartSpline,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  CircleDollarSign,
  ClipboardCopy,
  Database,
  FileCheck2,
  FileDiff,
  FileText,
  Fingerprint,
  GitBranch,
  GitCompareArrows,
  Home,
  Landmark,
  Layers,
  LayoutGrid,
  Loader2,
  Lock,
  LogOut,
  Network,
  Package,
  Play,
  Radar,
  Scale,
  ScrollText,
  Search,
  Server,
  Settings,
  Shield,
  ShieldAlert,
  Siren,
  SlidersHorizontal,
  Sparkles,
  SquareTerminal,
  Stamp,
  Upload,
  Workflow,
  X,
} from 'lucide-react';

export type IconName =
  // navigation (one per tab)
  | 'home' | 'analytics' | 'runs' | 'diff' | 'registry' | 'governance'
  | 'eval' | 'risk' | 'lineage' | 'kg' | 'cost' | 'schema' | 'evidence'
  | 'audit' | 'holds' | 'policy' | 'seal' | 'spine' | 'compliance'
  | 'incidents' | 'capture' | 'infra' | 'storage' | 'ops' | 'alerts'
  | 'admin' | 'commands' | 'reports' | 'export'
  // common UI
  | 'search' | 'settings' | 'close' | 'check' | 'copy' | 'spinner'
  | 'warning' | 'external' | 'chevron-down' | 'chevron-left' | 'chevron-right'
  | 'collapse' | 'expand' | 'filters' | 'disconnect' | 'topology';

const ICONS: Record<IconName, ComponentType<SVGProps<SVGSVGElement>>> = {
  home: Home,
  analytics: ChartSpline,
  runs: Play,
  diff: FileDiff,
  registry: Package,
  governance: Landmark,
  eval: BadgeCheck,
  risk: ShieldAlert,
  lineage: GitBranch,
  kg: Sparkles,
  cost: CircleDollarSign,
  schema: Boxes,
  evidence: FileCheck2,
  audit: ScrollText,
  holds: Lock,
  policy: Scale,
  seal: Stamp,
  spine: Anchor,
  compliance: Shield,
  incidents: Siren,
  capture: Camera,
  infra: Server,
  storage: Database,
  ops: Activity,
  alerts: Bell,
  admin: LayoutGrid,
  commands: SquareTerminal,
  reports: FileText,
  export: Upload,
  search: Search,
  settings: Settings,
  close: X,
  check: Check,
  copy: ClipboardCopy,
  spinner: Loader2,
  warning: AlertTriangle,
  external: ArrowUpRight,
  'chevron-down': ChevronDown,
  'chevron-left': ChevronLeft,
  'chevron-right': ChevronRight,
  collapse: ChevronsLeft,
  expand: ChevronsRight,
  filters: SlidersHorizontal,
  disconnect: LogOut,
  topology: Network,
};

// Referenced so alternates stay importable without lint noise if unused later.
export const _reserved = { Archive, Fingerprint, GitCompareArrows, Layers, Radar, BarChart3, Workflow };

export interface IconProps {
  name: IconName;
  /** Pixel size. 14 for dense rows, 16 for headers/buttons. */
  size?: number;
  className?: string;
  strokeWidth?: number;
  'aria-hidden'?: boolean;
}

export default function Icon({
  name,
  size = 14,
  className,
  strokeWidth = 1.75,
  ...rest
}: IconProps) {
  const Cmp = ICONS[name];
  return (
    <Cmp
      width={size}
      height={size}
      strokeWidth={strokeWidth}
      className={className}
      aria-hidden={rest['aria-hidden'] ?? true}
    />
  );
}

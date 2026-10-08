import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  /** Shown above the reset button. Defaults to a generic message. */
  label?: string;
  /** Called when the user clicks "Reload". */
  onReset?: () => void;
}

interface State {
  error: Error | null;
}

/**
 * Catches render/runtime errors in a subtree so one crashing tab cannot blank
 * the whole dashboard. Reset it from the parent by changing its `key`
 * (e.g. `key={tab}`), which remounts a fresh boundary on navigation.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface to the console for debugging; never swallow silently.
    // eslint-disable-next-line no-console
    console.error('Dashboard tab crashed:', error, info.componentStack);
  }

  private handleReset = (): void => {
    this.setState({ error: null });
    this.props.onReset?.();
  };

  render(): ReactNode {
    const { error } = this.state;
    if (error) {
      return (
        <div
          role="alert"
          className="m-4 rounded-lg border border-[color-mix(in_oklab,var(--color-status-failure)_30%,transparent)] bg-[color-mix(in_oklab,var(--color-status-failure)_8%,var(--color-bg-raised))] p-6 text-sm"
        >
          <p className="font-semibold text-[var(--color-status-failure)]">
            {this.props.label ?? 'This view hit an error.'}
          </p>
          <p className="mt-1 font-mono text-xs text-[var(--color-text-muted)] break-all">
            {error.message}
          </p>
          <button
            onClick={this.handleReset}
            className="mt-4 px-3 py-1.5 rounded border border-[var(--color-border)] text-xs text-[var(--color-text)] hover:border-[var(--color-accent)] hover:bg-[color-mix(in_oklab,var(--color-accent)_8%,transparent)] transition-colors"
          >
            Reload view
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

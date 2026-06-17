import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  name?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Minimal error boundary — wraps a panel so one crash doesn't take the whole dashboard.
 * Shows the error message and a retry button.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  handleRetry = () => this.setState({ hasError: false, error: null });

  render() {
    if (!this.state.hasError) return this.props.children;

    if (this.props.fallback) return this.props.fallback;

    return (
      <div className="rounded-lg border border-red-800/50 bg-red-900/20 p-4">
        <p className="text-sm text-red-400 font-medium">
          {this.props.name ? `${this.props.name} — ` : ""}Error
        </p>
        <p className="text-xs text-red-300/70 mt-1 font-mono">
          {this.state.error?.message ?? "Unknown error"}
        </p>
        <button
          onClick={this.handleRetry}
          className="mt-2 text-xs text-red-300 hover:text-red-200 underline"
        >
          Retry
        </button>
      </div>
    );
  }
}

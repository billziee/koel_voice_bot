import React from 'react';

// App-wide error boundary (T). React 18 has no functional equivalent, so this
// stays a class component. Catches render/runtime errors in the tree and shows a
// recoverable fallback instead of a blank white screen.

interface Props {
  children: React.ReactNode;
}

interface State {
  hasError: boolean;
  message: string;
}

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false, message: '' };

  static getDerivedStateFromError(error: unknown): State {
    return { hasError: true, message: error instanceof Error ? error.message : 'Unexpected error' };
  }

  componentDidCatch(error: unknown): void {
    // Hook a real error tracker (Sentry) here in production.
    // eslint-disable-next-line no-console
    console.error('[KOEL] Uncaught render error:', error);
  }

  private handleReload = (): void => {
    this.setState({ hasError: false, message: '' });
    window.location.reload();
  };

  render(): React.ReactNode {
    if (!this.state.hasError) return this.props.children;
    return (
      <div
        role="alert"
        style={{
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '1rem',
          padding: '2rem',
          fontFamily: '"DM Sans", sans-serif',
          background: '#053BEE',
          color: '#fff',
          textAlign: 'center',
        }}
      >
        <h1 style={{ fontFamily: '"Lora", serif', fontSize: '2rem', margin: 0 }}>Something went wrong</h1>
        <p style={{ maxWidth: 480, opacity: 0.85 }}>
          The dashboard hit an unexpected error and stopped rendering. Your call data is safe on the
          server. Reload to recover.
        </p>
        <code style={{ fontSize: 12, opacity: 0.7, wordBreak: 'break-word', maxWidth: 480 }}>
          {this.state.message}
        </code>
        <button
          type="button"
          onClick={this.handleReload}
          style={{
            padding: '0.65rem 1.4rem',
            borderRadius: 8,
            border: 'none',
            background: '#fff',
            color: '#053BEE',
            fontWeight: 700,
            cursor: 'pointer',
          }}
        >
          Reload dashboard
        </button>
      </div>
    );
  }
}

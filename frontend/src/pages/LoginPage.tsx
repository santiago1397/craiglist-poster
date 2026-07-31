import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { ApiError } from "../lib/api";

export default function LoginPage() {
  const { user, login } = useAuth();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (user) {
    const from = (location.state as any)?.from?.pathname || "/";
    return <Navigate to={from} replace />;
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
    } catch (e) {
      // Only a 401 actually means the credentials were wrong. Reporting a 404,
      // a 500 or an unreachable API as "Invalid credentials" sends you off
      // retyping a correct password while the real fault is the server or the
      // proxy in front of it.
      if (e instanceof ApiError) {
        if (e.status === 429) setError("Too many attempts. Wait a minute and try again.");
        else if (e.status === 401) setError("Invalid credentials.");
        else setError(`Could not reach the server (HTTP ${e.status}). Try again shortly.`);
      } else {
        setError("Could not reach the server. Check your connection and try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex items-center justify-center min-h-[calc(100vh-3.5rem)] px-4">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 bg-surface rounded-lg p-6 border border-border"
      >
        <h1 className="text-lg font-semibold">Sign in</h1>
        <div className="space-y-1.5">
          <label htmlFor="email" className="text-sm text-fg-muted block">
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded bg-surface-2 border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-slate-500"
          />
        </div>
        <div className="space-y-1.5">
          <label htmlFor="password" className="text-sm text-fg-muted block">
            Password
          </label>
          <input
            id="password"
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded bg-surface-2 border border-border-strong px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-slate-500"
          />
        </div>
        {error && <div className="text-sm text-danger-fg">{error}</div>}
        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded bg-primary text-primary-fg font-medium py-2 text-sm hover:bg-primary-hover disabled:opacity-60"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

import { Navigate, Route, Routes, Link, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/auth";
import LoginPage from "./pages/LoginPage";
import DashboardPage from "./pages/DashboardPage";
import PostsPage from "./pages/PostsPage";
import PostDetailPage from "./pages/PostDetailPage";
import { cn } from "./lib/cn";

function Protected({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <div className="p-8 text-slate-400">Loading…</div>;
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />;
  return <>{children}</>;
}

function NavBar() {
  const { user, logout } = useAuth();
  const location = useLocation();
  if (!user) return null;
  const link = (to: string, label: string) => (
    <Link
      to={to}
      className={cn(
        "px-3 py-1.5 rounded text-sm hover:bg-slate-800",
        location.pathname === to || (to !== "/" && location.pathname.startsWith(to))
          ? "bg-slate-800 text-white"
          : "text-slate-300",
      )}
    >
      {label}
    </Link>
  );
  return (
    <nav className="flex items-center justify-between px-4 py-2 border-b border-slate-800 bg-slate-900">
      <div className="flex items-center gap-2">
        <span className="font-semibold mr-4">CL Automation</span>
        {link("/", "Dashboard")}
        {link("/posts", "Posts")}
      </div>
      <div className="flex items-center gap-3 text-sm text-slate-400">
        <span>{user.email}</span>
        <button onClick={logout} className="px-2 py-1 rounded hover:bg-slate-800">
          Log out
        </button>
      </div>
    </nav>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <div className="min-h-full">
        <NavBar />
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/"
            element={
              <Protected>
                <DashboardPage />
              </Protected>
            }
          />
          <Route
            path="/posts"
            element={
              <Protected>
                <PostsPage />
              </Protected>
            }
          />
          <Route
            path="/posts/:postId"
            element={
              <Protected>
                <PostDetailPage />
              </Protected>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </AuthProvider>
  );
}

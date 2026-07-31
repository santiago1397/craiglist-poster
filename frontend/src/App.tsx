import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/auth";
import { AppShell } from "./components/AppShell";
import LoginPage from "./pages/LoginPage";
import DashboardPage from "./pages/DashboardPage";
import PostsPage from "./pages/PostsPage";
import PostDetailPage from "./pages/PostDetailPage";
import ReviewPage from "./pages/ReviewPage";
import ImagesPage from "./pages/ImagesPage";
import PromptsPage from "./pages/PromptsPage";
import SettingsPage from "./pages/SettingsPage";

function Protected({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <div className="p-8 text-fg-muted">Loading…</div>;
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <AuthProvider>
      <AppShell>
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
            path="/review"
            element={
              <Protected>
                <ReviewPage />
              </Protected>
            }
          />
          <Route
            path="/images"
            element={
              <Protected>
                <ImagesPage />
              </Protected>
            }
          />
          <Route
            path="/prompts"
            element={
              <Protected>
                <PromptsPage />
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
          <Route
            path="/settings"
            element={
              <Protected>
                <SettingsPage />
              </Protected>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AppShell>
    </AuthProvider>
  );
}

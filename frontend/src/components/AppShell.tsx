// Application frame: header, navigation, theme control.
//
// The previous NavBar was a single non-wrapping flex row of five links plus an
// email plus a logout button. At 390px it forced the document to 649px, so
// every page scrolled sideways. Below md the links move into a drawer and the
// header keeps only what matters on a phone: where you are, whether the system
// is healthy, and a way out.

import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import * as Dialog from "@radix-ui/react-dialog";
import {
  Images as ImagesIcon,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Menu,
  Monitor,
  Moon,
  ScrollText,
  SlidersHorizontal,
  Sparkles,
  Sun,
  X,
} from "lucide-react";
import { useAuth } from "../lib/auth";
import { useTheme, type Theme } from "../lib/theme";
import { StatusPill } from "./StatusPill";
import { cn } from "../lib/cn";

const NAV = [
  { to: "/", label: "Dashboard", Icon: LayoutDashboard },
  { to: "/review", label: "Review", Icon: ListChecks },
  { to: "/images", label: "Images", Icon: ImagesIcon },
  { to: "/prompts", label: "Prompts", Icon: Sparkles },
  { to: "/posts", label: "Posts", Icon: ScrollText },
  { to: "/settings", label: "Settings", Icon: SlidersHorizontal },
];

function isActive(pathname: string, to: string): boolean {
  return to === "/" ? pathname === "/" : pathname === to || pathname.startsWith(to + "/");
}

function ThemeToggle({ compact }: { compact?: boolean }) {
  const { theme, setTheme } = useTheme();
  const options: { value: Theme; label: string; Icon: typeof Sun }[] = [
    { value: "light", label: "Light", Icon: Sun },
    { value: "dark", label: "Dark", Icon: Moon },
    { value: "system", label: "System", Icon: Monitor },
  ];

  return (
    <div
      role="radiogroup"
      aria-label="Colour theme"
      className={cn("inline-flex rounded-md border border-border p-0.5", compact && "w-full")}
    >
      {options.map(({ value, label, Icon }) => (
        <button
          key={value}
          role="radio"
          aria-checked={theme === value}
          aria-label={label}
          title={label}
          onClick={() => setTheme(value)}
          className={cn(
            "inline-flex items-center justify-center gap-1.5 rounded px-2 py-1 text-xs",
            compact && "flex-1",
            theme === value
              ? "bg-surface-2 text-fg"
              : "text-fg-muted hover:text-fg hover:bg-surface-2/60",
          )}
        >
          <Icon size={14} aria-hidden="true" />
          {compact && <span>{label}</span>}
        </button>
      ))}
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth();
  const location = useLocation();
  const [drawer, setDrawer] = useState(false);

  // Navigating from inside the drawer must dismiss it, otherwise it stays open
  // over the page you just asked for.
  useEffect(() => {
    setDrawer(false);
  }, [location.pathname]);

  if (!user) return <>{children}</>;

  return (
    <div className="min-h-full flex flex-col">
      <header className="sticky top-0 z-30 border-b border-border bg-surface">
        <div className="flex items-center gap-2 px-3 sm:px-4 h-14">
          <Dialog.Root open={drawer} onOpenChange={setDrawer}>
            <Dialog.Trigger
              aria-label="Open navigation menu"
              className="md:hidden rounded p-2 -ml-1 text-fg-muted hover:text-fg hover:bg-surface-2"
            >
              <Menu size={20} aria-hidden="true" />
            </Dialog.Trigger>

            <Dialog.Portal>
              <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40 md:hidden" />
              <Dialog.Content
                className={cn(
                  "fixed z-50 inset-y-0 left-0 w-72 max-w-[85vw] md:hidden",
                  "bg-surface border-r border-border flex flex-col focus:outline-none",
                )}
              >
                <div className="flex items-center justify-between px-3 h-14 border-b border-border">
                  <Dialog.Title className="font-semibold">CL Automation</Dialog.Title>
                  <Dialog.Close
                    aria-label="Close navigation menu"
                    className="rounded p-2 text-fg-muted hover:text-fg hover:bg-surface-2"
                  >
                    <X size={18} aria-hidden="true" />
                  </Dialog.Close>
                </div>
                <Dialog.Description className="sr-only">
                  Main navigation and account controls
                </Dialog.Description>

                <nav className="flex-1 overflow-auto p-2">
                  {NAV.map(({ to, label, Icon }) => (
                    <Link
                      key={to}
                      to={to}
                      aria-current={isActive(location.pathname, to) ? "page" : undefined}
                      className={cn(
                        // 44px min height — a comfortable touch target.
                        "flex items-center gap-3 rounded px-3 py-2.5 text-sm",
                        isActive(location.pathname, to)
                          ? "bg-surface-2 text-fg font-medium"
                          : "text-fg-muted hover:bg-surface-2/60 hover:text-fg",
                      )}
                    >
                      <Icon size={18} aria-hidden="true" />
                      {label}
                    </Link>
                  ))}
                </nav>

                <div className="border-t border-border p-3 space-y-3">
                  <ThemeToggle compact />
                  <p className="text-xs text-fg-subtle break-all">{user.email}</p>
                  <button
                    onClick={logout}
                    className="flex w-full items-center gap-2 rounded px-3 py-2.5 text-sm text-fg-muted hover:bg-surface-2 hover:text-fg"
                  >
                    <LogOut size={16} aria-hidden="true" />
                    Log out
                  </button>
                </div>
              </Dialog.Content>
            </Dialog.Portal>
          </Dialog.Root>

          <Link to="/" className="font-semibold whitespace-nowrap mr-2">
            <span className="hidden sm:inline">CL Automation</span>
            <span className="sm:hidden">CL</span>
          </Link>

          <nav className="hidden md:flex items-center gap-1">
            {NAV.map(({ to, label }) => (
              <Link
                key={to}
                to={to}
                aria-current={isActive(location.pathname, to) ? "page" : undefined}
                className={cn(
                  "px-3 py-1.5 rounded text-sm",
                  isActive(location.pathname, to)
                    ? "bg-surface-2 text-fg"
                    : "text-fg-muted hover:bg-surface-2 hover:text-fg",
                )}
              >
                {label}
              </Link>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2 sm:gap-3 min-w-0">
            <StatusPill />
            <div className="hidden md:block">
              <ThemeToggle />
            </div>
            <span className="hidden lg:inline text-sm text-fg-muted truncate max-w-[14rem]">
              {user.email}
            </span>
            <button
              onClick={logout}
              aria-label="Log out"
              className="hidden md:inline-flex items-center gap-1.5 rounded px-2 py-1.5 text-sm text-fg-muted hover:bg-surface-2 hover:text-fg"
            >
              <LogOut size={16} aria-hidden="true" />
              <span className="hidden xl:inline">Log out</span>
            </button>
          </div>
        </div>
      </header>

      <main className="flex-1">{children}</main>
    </div>
  );
}

import type { Config } from "tailwindcss";

/**
 * Colours resolve to the CSS variables defined in index.css, so a component
 * never names a shade. `<alpha-value>` keeps opacity modifiers working, e.g.
 * `bg-surface/50`.
 *
 * darkMode: "class" rather than "media" — the theme is a user choice persisted
 * to localStorage, defaulting to the system preference.
 */
const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: token("bg"),
        surface: {
          DEFAULT: token("surface"),
          2: token("surface-2"),
        },
        border: {
          DEFAULT: token("border"),
          strong: token("border-strong"),
        },
        fg: {
          DEFAULT: token("fg"),
          muted: token("fg-muted"),
          subtle: token("fg-subtle"),
        },
        primary: {
          DEFAULT: token("primary"),
          hover: token("primary-hover"),
          fg: token("primary-fg"),
        },
        accent: {
          DEFAULT: token("accent"),
          hover: token("accent-hover"),
          fg: token("accent-fg"),
          soft: token("accent-soft"),
          "soft-fg": token("accent-soft-fg"),
        },
        info: {
          DEFAULT: token("info"),
          fg: token("info-fg"),
          border: token("info-border"),
        },
        ok: {
          DEFAULT: token("ok"),
          fg: token("ok-fg"),
          border: token("ok-border"),
          solid: token("ok-solid"),
        },
        warn: {
          DEFAULT: token("warn"),
          fg: token("warn-fg"),
          border: token("warn-border"),
          solid: token("warn-solid"),
        },
        danger: {
          DEFAULT: token("danger"),
          fg: token("danger-fg"),
          border: token("danger-border"),
          solid: token("danger-solid"),
        },
        "on-solid": token("on-solid"),
        ring: token("ring"),
      },
    },
  },
  plugins: [],
} satisfies Config;

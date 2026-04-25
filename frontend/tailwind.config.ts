import type { Config } from "tailwindcss";
import tailwindcssAnimate from "tailwindcss-animate";

export default {
  darkMode: ["class", '[data-theme="dark"]'],
  content: [
    "./pages/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./app/**/*.{ts,tsx}",
    "./src/**/*.{ts,tsx}",
    "./index.html",
  ],
  prefix: "",
  theme: {
    container: {
      center: true,
      padding: { DEFAULT: "1rem", md: "1.5rem", lg: "2rem" },
      screens: { "2xl": "1280px" },
    },
    extend: {
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
        // Aliased for any historic component that imports `font-serif`.
        serif: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
      },
      fontSize: {
        // Tighter scale, snapped to ladder
        xs:   ["0.75rem",  { lineHeight: "1.25rem" }],
        sm:   ["0.875rem", { lineHeight: "1.375rem" }],
        base: ["0.9375rem", { lineHeight: "1.5rem" }],
        md:   ["1rem",     { lineHeight: "1.625rem" }],
        lg:   ["1.125rem", { lineHeight: "1.75rem" }],
        xl:   ["1.25rem",  { lineHeight: "1.875rem", letterSpacing: "-0.01em" }],
        "2xl":["1.5rem",   { lineHeight: "2rem", letterSpacing: "-0.015em" }],
        "3xl":["1.875rem", { lineHeight: "2.25rem", letterSpacing: "-0.02em" }],
        "4xl":["2.25rem",  { lineHeight: "2.5rem", letterSpacing: "-0.025em" }],
        "5xl":["3rem",     { lineHeight: "3.25rem", letterSpacing: "-0.03em" }],
        "6xl":["3.75rem",  { lineHeight: "4rem", letterSpacing: "-0.035em" }],
      },
      spacing: {
        // 4px ladder convenience aliases
        "0.5": "2px",
        "1": "4px",
        "2": "8px",
        "3": "12px",
        "4": "16px",
        "5": "20px",
        "6": "24px",
        "7": "28px",
        "8": "32px",
        "10": "40px",
        "12": "48px",
        "14": "56px",
        "16": "64px",
        "20": "80px",
        "24": "96px",
      },
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",

        // Surfaces (semantic)
        surface: {
          0: "hsl(var(--bg-0))",
          1: "hsl(var(--bg-1))",
          2: "hsl(var(--bg-2))",
          3: "hsl(var(--bg-3))",
          4: "hsl(var(--bg-4))",
        },
        ink: {
          0: "hsl(var(--fg-0))",
          1: "hsl(var(--fg-1))",
          2: "hsl(var(--fg-2))",
          3: "hsl(var(--fg-3))",
        },

        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        success: {
          DEFAULT: "hsl(var(--success))",
          foreground: "hsl(0 0% 100%)",
        },
        // Deft v2 — phosphor green deploy accent.
        deploy: {
          DEFAULT: "hsl(var(--deploy))",
          foreground: "hsl(var(--deploy-foreground))",
          muted: "hsl(var(--deploy-muted))",
        },
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(240 10% 8%)",
        },
        info: {
          DEFAULT: "hsl(var(--info))",
          foreground: "hsl(0 0% 100%)",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
          muted: "hsl(var(--accent-muted))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        sidebar: {
          DEFAULT: "hsl(var(--sidebar-background))",
          foreground: "hsl(var(--sidebar-foreground))",
          primary: "hsl(var(--sidebar-primary))",
          "primary-foreground": "hsl(var(--sidebar-primary-foreground))",
          accent: "hsl(var(--sidebar-accent))",
          "accent-foreground": "hsl(var(--sidebar-accent-foreground))",
          border: "hsl(var(--sidebar-border))",
          ring: "hsl(var(--sidebar-ring))",
        },
      },
      borderRadius: {
        sm: "var(--radius-sm)",
        DEFAULT: "var(--radius)",
        md: "var(--radius-md)",
        lg: "var(--radius-lg)",
        xl: "var(--radius-xl)",
      },
      transitionTimingFunction: {
        "out-expo": "cubic-bezier(0.16, 1, 0.3, 1)",
        "in-out-expo": "cubic-bezier(0.87, 0, 0.13, 1)",
      },
      transitionDuration: {
        "instant": "100ms",
        "fast": "150ms",
        "base": "200ms",
        "slow": "250ms",
      },
      boxShadow: {
        "elev-1": "var(--shadow-1)",
        "elev-2": "var(--shadow-2)",
        "elev-3": "var(--shadow-3)",
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        blink: { "0%, 100%": { opacity: "1" }, "50%": { opacity: "0" } },
        "fade-in": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "slide-up": {
          from: { opacity: "0", transform: "translateY(16px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in-slow": { from: { opacity: "0" }, to: { opacity: "1" } },
        "scale-in": {
          from: { opacity: "0", transform: "scale(0.98)" },
          to: { opacity: "1", transform: "scale(1)" },
        },
        "slide-in-right": {
          from: { transform: "translateX(100%)" },
          to: { transform: "translateX(0)" },
        },
        "credit-pulse": {
          "0%": { color: "hsl(var(--fg-0))" },
          "25%": { color: "hsl(var(--warning))" },
          "100%": { color: "hsl(var(--fg-0))" },
        },
        "shimmer": {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 200ms cubic-bezier(0.16, 1, 0.3, 1)",
        "accordion-up": "accordion-up 200ms cubic-bezier(0.16, 1, 0.3, 1)",
        blink: "blink 1s step-end infinite",
        "fade-in": "fade-in 250ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "slide-up": "slide-up 250ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "fade-in-slow": "fade-in-slow 600ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "scale-in": "scale-in 200ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "slide-in-right": "slide-in-right 200ms cubic-bezier(0.16, 1, 0.3, 1)",
        "credit-pulse": "credit-pulse 1.5s cubic-bezier(0.16, 1, 0.3, 1)",
        "shimmer": "shimmer 1.6s linear infinite",
      },
    },
  },
  plugins: [tailwindcssAnimate],
} satisfies Config;

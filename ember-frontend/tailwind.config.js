/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#0A0D12",
        surface: "#10151D",
        surfaceRaised: "#151B24",
        border: "#1C2430",
        borderLight: "#242D3B",
        textPrimary: "#E9EDF2",
        textSecondary: "#7C8798",
        textMuted: "#4E5765",
        ember: {
          DEFAULT: "#E1712F",
          soft: "#F2A65A",
          dim: "#7A4526",
        },
        statusCloud: "#5FBF8B",
        statusFallback: "#E1B14A",
        statusLocal: "#D9534F",
        statusOffline: "#5A6270",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      keyframes: {
        pulseDot: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
        emberFlicker: {
          "0%, 100%": { boxShadow: "0 0 0 rgba(225,113,47,0)" },
          "50%": { boxShadow: "0 0 12px rgba(225,113,47,0.35)" },
        },
        riseIn: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        pulseDot: "pulseDot 1.6s ease-in-out infinite",
        emberFlicker: "emberFlicker 1.4s ease-in-out infinite",
        riseIn: "riseIn 0.18s ease-out",
      },
    },
  },
  plugins: [],
};

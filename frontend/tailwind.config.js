/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#090C11", panel: "#0E131B", panel2: "#131C28", inset: "#0B0F16",
        line: "#1B2432", line2: "#26313f",
        txt: "#EAF0F7", dim: "#8494A7", faint: "#525E6D",
        brand: "#F6A93B", info: "#4FB0FF", warn: "#FFC94D",
        long: "#35D6A0", short: "#FF5D6E",
      },
      fontFamily: {
        mono: ['ui-monospace', 'SF Mono', 'JetBrains Mono', 'Menlo', 'monospace'],
        sans: ['SF Pro Text', '-apple-system', 'Segoe UI', 'system-ui', 'sans-serif'],
      },
      borderRadius: { xl: "12px", md: "8px" },
    },
  },
  plugins: [],
};

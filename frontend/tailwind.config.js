/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0E1512",        // near-black with a green undertone, not pure black
        graphite: "#161D1A",
        slate: "#1F2A26",
        mist: "#8FA39C",       // muted sage-grey for secondary text
        paper: "#EDEFEC",
        signal: {
          buy: "#3FD97F",      // phosphor green
          prepare: "#4FB8E0",  // cool cyan-blue
          hold: "#E0B84F",     // amber
          avoid: "#5C6864",    // muted grey — deliberately unexciting
          sell: "#E05C4F",     // muted red-orange
        },
      },
      fontFamily: {
        display: ["'Fraunces'", "serif"],
        mono: ["'IBM Plex Mono'", "monospace"],
        body: ["'Inter'", "sans-serif"],
      },
    },
  },
  plugins: [],
};

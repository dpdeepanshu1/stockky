import { useEffect, useState } from "react";

const STAGES = [
  "Market data collection",
  "Validation",
  "Fundamental analysis",
  "Technical analysis",
  "Decision synthesis",
];

export default function Pipeline({ running }: { running: boolean }) {
  const [activeIndex, setActiveIndex] = useState(-1);

  useEffect(() => {
    if (!running) {
      setActiveIndex(-1);
      return;
    }
    setActiveIndex(0);
    const interval = setInterval(() => {
      setActiveIndex((i) => {
        if (i >= STAGES.length - 1) {
          clearInterval(interval);
          return i;
        }
        return i + 1;
      });
    }, 380);
    return () => clearInterval(interval);
  }, [running]);

  return (
    <div className="flex flex-col gap-0">
      {STAGES.map((stage, i) => {
        const state = !running ? "idle" : i < activeIndex ? "done" : i === activeIndex ? "running" : "waiting";
        return (
          <div key={stage} className="flex items-center gap-3 py-1.5">
            <div className="flex flex-col items-center">
              <span
                className={
                  "h-2 w-2 rounded-full transition-all duration-300 " +
                  (state === "done"
                    ? "bg-signal-buy scale-100"
                    : state === "running"
                    ? "bg-signal-prepare scale-125 animate-pulse"
                    : "bg-slate scale-90")
                }
              />
              {i < STAGES.length - 1 && (
                <span
                  className={
                    "w-px h-6 transition-colors duration-300 " +
                    (state === "done" ? "bg-signal-buy/50" : "bg-slate")
                  }
                />
              )}
            </div>
            <span
              className={
                "font-mono text-xs tracking-wide transition-colors duration-300 " +
                (state === "done"
                  ? "text-mist"
                  : state === "running"
                  ? "text-paper"
                  : "text-mist/40")
              }
            >
              {stage}
            </span>
          </div>
        );
      })}
    </div>
  );
}

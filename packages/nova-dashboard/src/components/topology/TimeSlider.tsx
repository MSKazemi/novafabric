/**
 * TimeSlider — Animated 3-phase time slider for TV-5 temporal navigation.
 *
 * Animation phases (from ADR-0068 build prompt):
 *   1. Fade-out  (200 ms CSS transition — opacity 1 → 0)
 *   2. Morph     (requestAnimationFrame topology swap via onWindowSelect)
 *   3. Fade-in   (200 ms CSS transition — opacity 0 → 1)
 *
 * Controls:
 *   - Previous / next step buttons
 *   - Play / pause (2 s auto-advance interval)
 *   - Live mode badge: clicking resumes WebSocket live feed
 *
 * No external state management dependency — all animation state is local.
 * Cross-panel selection is coordinated through tv5Store (caller's responsibility
 * to pass the store values in via props and wire onWindowSelect accordingly).
 */
import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

const FADE_DURATION_MS = 200;
const PLAY_INTERVAL_MS = 2000;

export interface WindowRecord {
  windowId: string;
  timestamp: number; // Unix epoch seconds
  tier: string; // "fine" | "coarse"
}

export interface TimeSliderProps {
  /** Fine-tier snapshots (5-min intervals, up to 288 over 24 h). */
  fineWindows: WindowRecord[];
  /** Coarse-tier snapshots (1-hour intervals, up to 168 over 7 d). */
  coarseWindows: WindowRecord[];
  /** Currently displayed window ID, or null (= live). */
  selectedWindowId: string | null;
  /** True when the WebSocket live feed is active. */
  isLive: boolean;
  /** Called with new windowId after the morph phase completes. */
  onWindowSelect: (windowId: string) => void;
  /** Called when the user clicks the LIVE badge to resume live mode. */
  onLiveModeResume: () => void;
}

function formatTimestamp(unixSecs: number): string {
  const d = new Date(unixSecs * 1000);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export function TimeSlider({
  fineWindows,
  coarseWindows,
  selectedWindowId,
  isLive,
  onWindowSelect,
  onLiveModeResume,
}: TimeSliderProps): React.ReactElement {
  // Combine windows into one sorted list (most recent last for slider ordering)
  const allWindows: WindowRecord[] = React.useMemo(() => {
    return [...fineWindows, ...coarseWindows].sort(
      (a, b) => a.timestamp - b.timestamp,
    );
  }, [fineWindows, coarseWindows]);

  // Current slider index (index into allWindows)
  const currentIdx = React.useMemo(() => {
    if (selectedWindowId === null) return allWindows.length - 1;
    const idx = allWindows.findIndex((w) => w.windowId === selectedWindowId);
    return idx >= 0 ? idx : allWindows.length - 1;
  }, [allWindows, selectedWindowId]);

  const [isPlaying, setIsPlaying] = useState(false);
  // Opacity for the 3-phase animation (controls a wrapper div)
  const [opacity, setOpacity] = useState(1);
  const animatingRef = useRef(false);
  const playTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  /**
   * Perform a 3-phase animated transition to the given window index.
   *   Phase 1: fade-out (200 ms)
   *   Phase 2: morph — call onWindowSelect (rAF, next paint)
   *   Phase 3: fade-in (200 ms)
   */
  const transitionTo = useCallback(
    (idx: number) => {
      if (animatingRef.current) return;
      const target = allWindows[idx];
      if (!target) return;
      if (target.windowId === selectedWindowId) return;

      animatingRef.current = true;

      // Phase 1: fade-out
      setOpacity(0);

      setTimeout(() => {
        // Phase 2: morph — swap snapshot on next animation frame
        requestAnimationFrame(() => {
          onWindowSelect(target.windowId);

          // Phase 3: fade-in
          setTimeout(() => {
            setOpacity(1);
            animatingRef.current = false;
          }, FADE_DURATION_MS);
        });
      }, FADE_DURATION_MS);
    },
    [allWindows, selectedWindowId, onWindowSelect],
  );

  const stepBack = useCallback(() => {
    const nextIdx = Math.max(0, currentIdx - 1);
    transitionTo(nextIdx);
  }, [currentIdx, transitionTo]);

  const stepForward = useCallback(() => {
    const nextIdx = Math.min(allWindows.length - 1, currentIdx + 1);
    transitionTo(nextIdx);
  }, [currentIdx, allWindows.length, transitionTo]);

  // Play / pause auto-advance
  useEffect(() => {
    if (isPlaying) {
      playTimerRef.current = setInterval(() => {
        const nextIdx = currentIdx + 1;
        if (nextIdx >= allWindows.length) {
          setIsPlaying(false);
        } else {
          transitionTo(nextIdx);
        }
      }, PLAY_INTERVAL_MS);
    } else if (playTimerRef.current !== null) {
      clearInterval(playTimerRef.current);
      playTimerRef.current = null;
    }
    return () => {
      if (playTimerRef.current !== null) {
        clearInterval(playTimerRef.current);
        playTimerRef.current = null;
      }
    };
  }, [isPlaying, currentIdx, allWindows.length, transitionTo]);

  const selectedWindow = allWindows[currentIdx];

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 12px",
        background: "#0f172a",
        borderTop: "1px solid #1e293b",
        userSelect: "none",
        flexShrink: 0,
      }}
    >
      {/* Live badge */}
      {isLive ? (
        <span
          style={{
            background: "#22c55e",
            color: "#052e16",
            fontSize: 10,
            fontWeight: 700,
            padding: "2px 6px",
            borderRadius: 3,
            letterSpacing: "0.05em",
            cursor: "default",
            flexShrink: 0,
          }}
        >
          LIVE
        </span>
      ) : (
        <button
          onClick={onLiveModeResume}
          title="Resume live feed"
          style={{
            background: "#334155",
            color: "#94a3b8",
            fontSize: 10,
            fontWeight: 700,
            padding: "2px 6px",
            borderRadius: 3,
            border: "none",
            cursor: "pointer",
            letterSpacing: "0.05em",
            flexShrink: 0,
          }}
        >
          LIVE
        </button>
      )}

      {/* Prev */}
      <button
        onClick={stepBack}
        disabled={currentIdx <= 0 || animatingRef.current}
        aria-label="Previous snapshot"
        style={{
          background: "transparent",
          border: "none",
          color: "#94a3b8",
          cursor: currentIdx > 0 ? "pointer" : "not-allowed",
          fontSize: 14,
          padding: "0 4px",
          flexShrink: 0,
        }}
      >
        &#9664;
      </button>

      {/* Range slider + animated viewport */}
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          gap: 2,
          opacity,
          transition: `opacity ${FADE_DURATION_MS}ms ease`,
          minWidth: 0,
        }}
      >
        <input
          type="range"
          min={0}
          max={Math.max(0, allWindows.length - 1)}
          value={currentIdx}
          onChange={(e) => {
            const idx = Number(e.target.value);
            transitionTo(idx);
          }}
          style={{ width: "100%", accentColor: "#3b82f6" }}
          aria-label="Timeline"
        />
        {selectedWindow && (
          <span
            style={{
              fontSize: 10,
              color: "#64748b",
              textAlign: "center",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {formatTimestamp(selectedWindow.timestamp)} ({selectedWindow.tier})
          </span>
        )}
      </div>

      {/* Next */}
      <button
        onClick={stepForward}
        disabled={currentIdx >= allWindows.length - 1 || animatingRef.current}
        aria-label="Next snapshot"
        style={{
          background: "transparent",
          border: "none",
          color: "#94a3b8",
          cursor: currentIdx < allWindows.length - 1 ? "pointer" : "not-allowed",
          fontSize: 14,
          padding: "0 4px",
          flexShrink: 0,
        }}
      >
        &#9654;
      </button>

      {/* Play / Pause */}
      <button
        onClick={() => setIsPlaying((p) => !p)}
        disabled={allWindows.length === 0}
        aria-label={isPlaying ? "Pause" : "Play"}
        style={{
          background: isPlaying ? "#3b82f6" : "#1e293b",
          border: "1px solid #334155",
          color: isPlaying ? "#fff" : "#94a3b8",
          fontSize: 11,
          padding: "2px 8px",
          borderRadius: 4,
          cursor: allWindows.length > 0 ? "pointer" : "not-allowed",
          flexShrink: 0,
        }}
      >
        {isPlaying ? "⏸" : "▶"}
      </button>
    </div>
  );
}

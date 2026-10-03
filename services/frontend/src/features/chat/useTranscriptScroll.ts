import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent, RefObject, TouchEvent, WheelEvent } from "react";
const SCROLL_END_TOLERANCE = 4;
export interface TranscriptScroll {
  list: RefObject<HTMLDivElement | null>;
  away: boolean;
  sync: () => void;
  pin: () => void;
  reset: () => void;
  jumpToLatest: () => void;
  handlers: {
    onWheel: (event: WheelEvent) => void;
    onTouchStart: (event: TouchEvent) => void;
    onTouchMove: (event: TouchEvent) => void;
    onKeyDown: (event: KeyboardEvent) => void;
    onScroll: () => void;
  };
}
// Follows new content while the reader is at the end and detaches on any upward intent.
export function useTranscriptScroll(): TranscriptScroll {
  const [away, setAway] = useState(false);
  const list = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const previousScrollTop = useRef(0);
  const touchY = useRef<number | null>(null);
  const sync = useCallback(() => {
    const element = list.current;
    if (!element) return;
    if (pinned.current) {
      element.scrollTop = element.scrollHeight;
      previousScrollTop.current = element.scrollTop;
    }
    setAway(
      element.scrollHeight - element.scrollTop - element.clientHeight >
        SCROLL_END_TOLERANCE,
    );
  }, []);
  useEffect(() => {
    const element = list.current;
    if (!element) return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(sync);
    });
    observer.observe(element);
    if (element.firstElementChild) observer.observe(element.firstElementChild);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [sync]);
  const pin = useCallback(() => {
    pinned.current = true;
    setAway(false);
  }, []);
  const reset = useCallback(() => {
    pin();
    previousScrollTop.current = 0;
  }, [pin]);
  function detach(): void {
    pinned.current = false;
  }
  function jumpToLatest(): void {
    pin();
    list.current?.scrollTo({
      top: list.current.scrollHeight,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
    });
  }
  return {
    list,
    away,
    sync,
    pin,
    reset,
    jumpToLatest,
    handlers: {
      onWheel: (event) => {
        if (event.deltaY < 0) detach();
      },
      onTouchStart: (event) => {
        touchY.current = event.touches[0]?.clientY ?? null;
      },
      onTouchMove: (event) => {
        const nextY = event.touches[0]?.clientY;
        if (
          nextY !== undefined &&
          touchY.current !== null &&
          nextY > touchY.current
        )
          detach();
        touchY.current = nextY ?? null;
      },
      onKeyDown: (event) => {
        if (event.target !== event.currentTarget) return;
        if (
          ["ArrowUp", "PageUp", "Home"].includes(event.key) ||
          (event.key === " " && event.shiftKey)
        )
          detach();
      },
      onScroll: () => {
        const element = list.current!;
        const distance =
          element.scrollHeight - element.scrollTop - element.clientHeight;
        if (element.scrollTop < previousScrollTop.current)
          pinned.current = false;
        else if (
          element.scrollTop > previousScrollTop.current &&
          distance <= SCROLL_END_TOLERANCE
        )
          pinned.current = true;
        previousScrollTop.current = element.scrollTop;
        setAway(distance > SCROLL_END_TOLERANCE);
      },
    },
  };
}

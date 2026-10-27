import { useEffect, useState, type RefObject } from "react";

/**
 * Whether any of `target` shows inside `root`'s viewport. True while there is no target,
 * and where IntersectionObserver isn't available, so nothing waits on it there.
 */
export function useInView(target: Element | null, root: RefObject<Element | null>): boolean {
  const [inView, setInView] = useState(true);
  useEffect(() => {
    if (target === null || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries.at(-1);
        if (entry !== undefined) setInView(entry.isIntersecting);
      },
      { root: root.current },
    );
    observer.observe(target);
    return () => {
      observer.disconnect();
    };
  }, [target, root]);
  return target === null || typeof IntersectionObserver === "undefined" || inView;
}

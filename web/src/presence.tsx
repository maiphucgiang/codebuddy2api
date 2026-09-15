import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

const Exit = createContext(false);
export const useExiting = () => useContext(Exit);

// Keep the modal in the top layer until its exit finishes; route unmounts still clean up immediately.
export function DrawerPresence({ children }: { children: ReactNode }) {
  const present = !!children;
  const [retained, setRetained] = useState(children);
  if (present && retained !== children) setRetained(children);
  useEffect(() => {
    if (present || !retained) return;
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const finish = () => setRetained(null);
    if (!media || media.matches) {
      finish();
      return;
    }
    const timer = window.setTimeout(finish, 180);
    const changed = () => {
      if (media.matches) finish();
    };
    media.addEventListener("change", changed);
    return () => {
      clearTimeout(timer);
      media.removeEventListener("change", changed);
    };
  }, [present, retained]);
  return <Exit.Provider value={!present}>{present ? children : retained}</Exit.Provider>;
}

import { createStore, useStore } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

interface SidebarState {
  /** The member's choice: collapsed to the 48px icon rail. */
  collapsed: boolean;
  /** A page that shows the icon rail while it's open (Customize); never stored. */
  rail: boolean;
}

export const sidebarStore = createStore<SidebarState>()(
  persist((): SidebarState => ({ collapsed: false, rail: false }), {
    name: "harmony.sidebar",
    // createJSONStorage swallows a storage that throws (private windows); state then
    // lives in memory only.
    storage: createJSONStorage(() => localStorage),
    partialize: ({ collapsed }) => ({ collapsed }),
  }),
);

export function useSidebarCollapsed(): boolean {
  return useStore(sidebarStore, (state) => state.collapsed || state.rail);
}

/** Opening the sidebar on a page that shows the rail opens it until the page closes. */
export function toggleSidebar(): void {
  sidebarStore.setState((state) =>
    state.rail ? { rail: false, collapsed: false } : { collapsed: !state.collapsed },
  );
}

/** Show the icon rail while the calling page is open; returns the cleanup. */
export function showRail(): () => void {
  sidebarStore.setState({ rail: true });
  return () => {
    sidebarStore.setState({ rail: false });
  };
}

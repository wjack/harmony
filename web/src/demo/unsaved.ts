/**
 * Unsaved work in this tab that a demo reset would drop, so the reset can warn first.
 * Module state, not storage: each tab answers only for its own work.
 */
export type UnsavedSource = "customize";

const sources = new Set<UnsavedSource>();

export function setUnsaved(source: UnsavedSource, dirty: boolean): void {
  if (dirty) sources.add(source);
  else sources.delete(source);
}

export function unsavedSources(): readonly UnsavedSource[] {
  return [...sources];
}

export function clearUnsaved(): void {
  sources.clear();
}

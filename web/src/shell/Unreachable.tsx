import { Button, Spark } from "./ui";

/**
 * Over the app while the dispatcher can't get a session snapshot (the API is down or
 * erroring). It keeps retrying with backoff, so this goes away by itself.
 */
export function Unreachable() {
  return (
    <div
      role="alert"
      className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-canvas/85 px-6 pb-[12vh] text-center"
    >
      <Spark size={28} animated />
      <h1 className="font-serif text-[26px] leading-9 tracking-[-0.015em] text-ink">Can&apos;t reach Harmony</h1>
      <p className="max-w-[440px] text-sm leading-6 text-muted">
        Retrying… This usually clears up in a few seconds. Nothing you&apos;ve done is lost.
      </p>
      <Button
        variant="secondary"
        className="mt-1"
        onClick={() => {
          window.location.reload();
        }}
      >
        Reload
      </Button>
    </div>
  );
}

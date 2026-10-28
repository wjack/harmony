import { openDemoControls } from "../demo/store";
import { Button, Spark } from "./ui";

/** Before the visitor has a session: nothing is created until they choose to start. */
export function Landing() {
  return (
    <div className="flex h-full flex-col items-center justify-center px-6 pb-[12vh]">
      <h1 className="flex items-center gap-3 font-serif text-[40px] leading-tight font-normal tracking-[-0.02em] text-ink">
        <Spark size={34} />
        Welcome to Harmony
      </h1>
      <p className="mt-3 max-w-[520px] text-center text-[15px] leading-6 text-muted">
        Claude, working with every other organization&apos;s Claude. Starting creates your own copy of
        the demo network; nothing is shared with other visitors.
      </p>
      <Button
        variant="primary"
        size="lg"
        className="mt-6"
        onClick={() => {
          openDemoControls();
        }}
      >
        Start the demo
      </Button>
    </div>
  );
}

import { Button, Modal } from "../shell/ui";
import { useResetDemo } from "./accounts";
import { closeResetConfirm, useDemoState } from "./store";

/** "Reset demo" asks first: it forks a fresh world and leaves this one behind (§11.1). */
export function ResetDemoDialog() {
  const open = useDemoState((state) => state.resetConfirmOpen);
  const reset = useResetDemo();

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          closeResetConfirm();
          reset.reset();
        }
      }}
      width={440}
      title="Reset the demo?"
      description="You'll get a fresh copy of the network, back at the start of the story. Your chats, requests and settings changes in this one are left behind."
      footer={
        <>
          <Button variant="secondary" onClick={closeResetConfirm}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={reset.isPending}
            onClick={() => {
              reset.mutate();
            }}
          >
            {reset.isPending ? "Resetting…" : "Reset demo"}
          </Button>
        </>
      }
    >
      {reset.error !== null && (
        <p role="alert" className="text-[13px] text-danger">
          {reset.error.message}
        </p>
      )}
    </Modal>
  );
}

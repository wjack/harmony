import { Button, Modal, Spark } from "./ui";
import { closeUnavailable, useUnavailableFeature } from "./unavailable";

/** The one "not available in this prototype" dialog, mounted once at the app root. */
export function NotAvailableDialog() {
  const feature = useUnavailableFeature();
  return (
    <Modal
      open={feature !== null}
      onOpenChange={(open) => {
        if (!open) closeUnavailable();
      }}
      width={440}
      icon={<Spark size={28} />}
      title="This isn't part of the Harmony prototype"
      description={
        <>
          {feature !== null && <span className="text-secondary">{feature} isn&apos;t included. </span>}
          Harmony covers chats with Claude, the Requests inbox, the network view and Customize →
          Harmony.
        </>
      }
      footer={
        <Button variant="primary" onClick={closeUnavailable} autoFocus>
          OK
        </Button>
      }
    />
  );
}

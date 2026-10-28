import { SUMMARY_MAX } from "./draft";
import { CharCount, Field, Section, TextArea } from "./fields";
import { DIRECTORY, DIRECTORY_ORDER } from "./labels";
import type { PaneProps } from "./pane";
import { RadioList } from "./RadioList";

/** Public profile: the summary other organizations read, and who can find it (§9). */
export function ProfilePane({ draft, edit, editable }: PaneProps) {
  const disabled = !editable;
  return (
    <>
      <Section
        title="About"
        description="Who you represent, what you do and what to come to you for, in your own words."
      >
        <Field label="Summary" aside={<CharCount value={draft.publicSummary} max={SUMMARY_MAX} />}>
          {(id) => (
            <TextArea
              id={id}
              rows={7}
              value={draft.publicSummary}
              disabled={disabled}
              onChange={(event) => {
                edit({ publicSummary: event.target.value });
              }}
              placeholder="A university characterization core running micro-CT and mechanical testing for academic and industry groups. Typical turnaround is a week; priority by arrangement."
            />
          )}
        </Field>
      </Section>

      <Section title="Directory listing" description="Who can find your representative on the network.">
        <RadioList
          name="directory-visibility"
          label="Directory listing"
          value={draft.directoryVisibility}
          options={DIRECTORY_ORDER.map((value) => ({ value, ...DIRECTORY[value] }))}
          onChange={(directoryVisibility) => {
            edit({ directoryVisibility });
          }}
          disabled={disabled}
        />
      </Section>
    </>
  );
}

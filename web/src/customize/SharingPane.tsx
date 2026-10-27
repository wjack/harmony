import { Plus, UserRoundPlus, X } from "lucide-react";
import { useEffect, useRef } from "react";

import { type Calendar, useCalendar } from "../demo/worldClock";
import { Button, cx, IconButton, Menu, MenuContent, MenuItem, MenuLabel, MenuTrigger, OrgAvatar, Tooltip } from "../shell/ui";
import { byName, useCounterparties } from "./counterparties";
import { PROSE_MAX, type TrustedDraft } from "./draft";
import { Callout, CharCount, Count, Field, Section, TextArea } from "./fields";
import { MODE_INFO, MODES } from "./modes";
import type { PaneProps } from "./pane";
import { Select, type SelectOption } from "./Select";
import type { Autonomy, OrgRef, TrustedOrganizationView } from "./types";

const DEFAULT = "default";
type RowMode = Autonomy | typeof DEFAULT;

/** A trusted row's mode choices: the org default, then each mode with what it means for this one org. */
function modeOptions(defaultMode: Autonomy): SelectOption<RowMode>[] {
  return [
    {
      value: DEFAULT,
      label: "Same as default",
      description: `${MODE_INFO[defaultMode].label}, like every other organization.`,
    },
    ...MODES.map(({ value, label, Icon, description }) => ({
      value,
      label,
      description,
      icon: <Icon size={15} strokeWidth={1.75} aria-hidden />,
    })),
  ];
}

const CHANGED_ON = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });

function changedBy(row: TrustedOrganizationView | undefined, calendar: Calendar): string | null {
  if (row === undefined) return null;
  const on = CHANGED_ON.format(calendar(row.updated_at));
  return row.updated_by === null ? `Changed ${on}` : `Changed by ${row.updated_by.name}, ${on}`;
}

interface TrustedRowProps {
  org: OrgRef;
  row: TrustedDraft;
  saved: TrustedOrganizationView | undefined;
  defaultMode: Autonomy;
  editable: boolean;
  focused: boolean;
  onChange: (row: TrustedDraft) => void;
  onRemove: () => void;
}

function TrustedRow({ org, row, saved, defaultMode, editable, focused, onChange, onRemove }: TrustedRowProps) {
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "center" });
  }, [focused]);
  const provenance = changedBy(saved, useCalendar());

  return (
    <li ref={ref} className={cx("flex gap-3 px-4 py-4", focused && "bg-accent/[0.06]")}>
      <OrgAvatar name={org.name} seed={org.slug} size={28} className="mt-1" />
      <div className="min-w-0 flex-1">
        <div className="flex min-h-9 items-center gap-3">
          <span className="min-w-0 flex-1 truncate text-sm leading-5 font-medium text-ink">{org.name}</span>
          <div className="w-[176px] shrink-0">
            <Select<RowMode>
              value={row.autonomy ?? DEFAULT}
              options={modeOptions(defaultMode)}
              menuWidth={400}
              aria-label={`Mode for ${org.name}`}
              onChange={(value) => {
                onChange({ ...row, autonomy: value === DEFAULT ? null : value });
              }}
              disabled={!editable}
            />
          </div>
          {editable && (
            <Tooltip side="top" content="Remove">
              <IconButton label={`Stop trusting ${org.name}`} size="sm" onClick={onRemove}>
                <X size={15} strokeWidth={1.75} />
              </IconButton>
            </Tooltip>
          )}
        </div>
        <Field
          label="Also share with them"
          aside={<CharCount value={row.sharingDescription} max={PROSE_MAX} />}
          className="mt-3"
        >
          {(id) => (
            <TextArea
              id={id}
              rows={2}
              value={row.sharingDescription}
              disabled={!editable}
              onChange={(event) => {
                onChange({ ...row, sharingDescription: event.target.value });
              }}
              placeholder="Nothing beyond the default."
            />
          )}
        </Field>
        {provenance !== null && <p className="mt-1.5 text-xs text-muted">{provenance}</p>}
      </div>
    </li>
  );
}

interface SharingPaneProps extends PaneProps {
  /** The organization a link asked about ("Edit what you share with Henderson Lab"). */
  focusOrgId: string | null;
}

/** Sharing: what the representative may pass on, by default and per trusted organization (§3, §9). */
export function SharingPane({ view, draft, edit, editable, focusOrgId }: SharingPaneProps) {
  const orgs = useCounterparties(view);
  const saved = new Map(view.trusted_organizations.map((row) => [row.counterparty.id, row]));
  const rows = Object.entries(draft.trusted)
    .map(([orgId, row]) => ({ org: orgs.get(orgId) ?? { id: orgId, slug: orgId, name: "Another organization" }, row }))
    .sort((a, b) => byName(a.org, b.org));
  const addable = [...orgs.values()].filter((org) => !(org.id in draft.trusted)).sort(byName);
  // The org a link named, when it isn't trusted yet: an admin is invited to add it. A
  // viewer can't, so nothing suggests it.
  const focusOrg =
    !editable || focusOrgId === null || focusOrgId in draft.trusted ? null : (orgs.get(focusOrgId) ?? null);

  const setRow = (orgId: string, row: TrustedDraft | null) => {
    const rest = Object.fromEntries(Object.entries(draft.trusted).filter(([id]) => id !== orgId));
    edit({ trusted: row === null ? rest : { ...rest, [orgId]: row } });
  };
  const add = (orgId: string) => {
    setRow(orgId, { autonomy: null, sharingDescription: "" });
  };

  return (
    <>
      <Section
        title="Shared with everyone"
        description="The kinds of information your representative may pass on to any organization, and what it may not. Describe them; don't list files. Claude looks up the current records when they're needed."
        aside={<CharCount value={draft.sharingDescription} max={PROSE_MAX} />}
      >
        <TextArea
          aria-label="Shared with everyone"
          rows={6}
          value={draft.sharingDescription}
          disabled={!editable}
          onChange={(event) => {
            edit({ sharingDescription: event.target.value });
          }}
          placeholder="Our price list, what's open on the public calendar and typical turnaround. Not: other clients, internal schedules or discounts."
        />
      </Section>

      <Section
        title={
          <span className="inline-flex items-center gap-2">
            Trusted organizations <Count>{rows.length}</Count>
          </span>
        }
        description="Organizations you treat differently: their own mode, and more you share with them."
        aside={
          editable ? (
            <Menu>
              <MenuTrigger asChild>
                <Button size="sm" icon={<Plus size={14} strokeWidth={2} />} disabled={addable.length === 0}>
                  Add organization
                </Button>
              </MenuTrigger>
              <MenuContent align="end" className="max-h-[320px] w-[280px] overflow-y-auto">
                <MenuLabel>Trust an organization</MenuLabel>
                {addable.map((org) => (
                  <MenuItem
                    key={org.id}
                    icon={<OrgAvatar name={org.name} seed={org.slug} size={20} />}
                    onSelect={() => {
                      add(org.id);
                    }}
                  >
                    <span className="block truncate">{org.name}</span>
                  </MenuItem>
                ))}
              </MenuContent>
            </Menu>
          ) : undefined
        }
      >
        {focusOrg !== null && (
          <Callout icon={<UserRoundPlus size={15} strokeWidth={1.75} />} className="mb-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <span className="min-w-0 flex-1">
                To share more with {focusOrg.name} than with everyone, add them as a trusted organization.
              </span>
              <Button
                size="sm"
                onClick={() => {
                  add(focusOrg.id);
                }}
              >
                Add {focusOrg.name}
              </Button>
            </div>
          </Callout>
        )}
        {rows.length === 0 ? (
          <p className="rounded-menu border border-dashed border-border-strong px-4 py-5 text-center text-[13px] text-muted">
            Every organization gets your default mode and what you share with everyone.
          </p>
        ) : (
          <ul className="divide-y divide-border overflow-hidden rounded-menu border border-border bg-surface">
            {rows.map(({ org, row }) => (
              <TrustedRow
                key={org.id}
                org={org}
                row={row}
                saved={saved.get(org.id)}
                defaultMode={draft.autonomy}
                editable={editable}
                focused={org.id === focusOrgId}
                onChange={(next) => {
                  setRow(org.id, next);
                }}
                onRemove={() => {
                  setRow(org.id, null);
                }}
              />
            ))}
          </ul>
        )}
      </Section>
    </>
  );
}

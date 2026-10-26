import { firstName, roleLabel } from "../shell/names";
import { Avatar } from "../shell/ui";
import { PROSE_MAX } from "./draft";
import { CharCount, Count, Field, Section, Tag, TextArea } from "./fields";
import type { PaneProps } from "./pane";
import { Select } from "./Select";
import type { MemberProfileView } from "./types";

const NO_ASSIGNEE = "none";

interface MemberRowProps {
  member: MemberProfileView;
  /** The org's slug: members' avatars take its color, as everywhere else. */
  orgSlug: string;
  profile: string;
  editable: boolean;
  onProfile: (profile: string) => void;
}

function MemberRow({ member, orgSlug, profile, editable, onProfile }: MemberRowProps) {
  const name = member.member.name;
  return (
    <li className="flex gap-3 py-3">
      <Avatar name={name} seed={orgSlug} size={32} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className="text-sm leading-5 font-medium text-ink">{name}</span>
          {member.role === "admin" && <Tag>{roleLabel(member.role)}</Tag>}
        </div>
        {member.title !== null && <div className="text-[13px] leading-5 text-muted">{member.title}</div>}
        {editable ? (
          <>
            <TextArea
              aria-label={`${name}'s profile`}
              rows={3}
              className="mt-2"
              value={profile}
              onChange={(event) => {
                onProfile(event.target.value);
              }}
              placeholder={`What ${firstName(name)} handles, in a sentence or two.`}
            />
            {profile.length > PROSE_MAX && (
              <div className="mt-1 text-right">
                <CharCount value={profile} max={PROSE_MAX} />
              </div>
            )}
          </>
        ) : (
          profile !== "" && <p className="mt-1 text-[13px] leading-5 text-secondary">{profile}</p>
        )}
      </div>
    </li>
  );
}

/** Team and routing: the routing prompt, the default assignee and the members' profiles (§9). */
export function TeamPane({ view, draft, edit, editable }: PaneProps) {
  const assignees = [
    { value: NO_ASSIGNEE, label: "Nobody", description: "Unrouted items wait for an admin." },
    ...view.members.map((member) => ({
      value: member.member.id,
      label: member.member.name,
      description: member.title ?? roleLabel(member.role),
      icon: <Avatar name={member.member.name} seed={view.org.slug} size={20} />,
    })),
  ];

  return (
    <>
      <Section
        title="Routing prompt"
        description="Claude reads this, with the profiles below, to decide who answers when your representative checks with the team."
        aside={<CharCount value={draft.routingPrompt} max={PROSE_MAX} />}
      >
        <TextArea
          aria-label="Routing prompt"
          rows={6}
          value={draft.routingPrompt}
          disabled={!editable}
          onChange={(event) => {
            edit({ routingPrompt: event.target.value });
          }}
          placeholder="Scheduling and priority requests go to Helen. Instrument questions go to Raj."
        />
        <Field label="Default assignee" hint="Gets anything routing can't place." className="mt-5 max-w-[360px]">
          {(id) => (
            <Select
              id={id}
              value={draft.defaultAssigneeId ?? NO_ASSIGNEE}
              options={assignees}
              onChange={(value) => {
                edit({ defaultAssigneeId: value === NO_ASSIGNEE ? null : value });
              }}
              disabled={!editable}
            />
          )}
        </Field>
      </Section>

      <Section
        title={
          <span className="inline-flex items-center gap-2">
            Members <Count>{view.members.length}</Count>
          </span>
        }
        description="What each person handles. Claude reads these with the routing prompt."
      >
        <ul className="divide-y divide-border">
          {view.members.map((member) => (
            <MemberRow
              key={member.member.id}
              member={member}
              orgSlug={view.org.slug}
              profile={draft.profiles[member.member.id] ?? member.profile}
              editable={editable}
              onProfile={(profile) => {
                edit({ profiles: { [member.member.id]: profile } });
              }}
            />
          ))}
        </ul>
      </Section>
    </>
  );
}

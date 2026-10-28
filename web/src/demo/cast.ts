import type { DemoStory, MemberDirectory, MemberView } from "../api/types";

/** "Try the suggested request" acts as the scenario's default member (scenario.yaml). */
export const STORY_MEMBER_SLUG = "alex";

export function allMembers(directory: MemberDirectory | undefined): MemberView[] {
  return directory === undefined ? [] : [...directory.featured, ...directory.all];
}

export function findMember(directory: MemberDirectory | undefined, slug: string, orgSlug?: string): MemberView | null {
  return (
    allMembers(directory).find(
      (member) => member.slug === slug && (orgSlug === undefined || member.org.slug === orgSlug),
    ) ?? null
  );
}

/**
 * The account menu's people: the ones the walkthrough acts as, and the acting member,
 * at the top level (`shown`), and everyone else under "All network members" (`more`).
 * All seven of the featured cast and Claude's own items don't fit a laptop screen
 * below the demo bar. Without a walkthrough, the featured cast are shown.
 */
export function menuAccounts(
  directory: MemberDirectory | undefined,
  story: DemoStory | undefined,
  actingId: string,
): { shown: MemberView[]; more: MemberView[] } {
  const actsAs = (story?.steps ?? []).flatMap((step) => (step.act_as === null ? [] : [step.act_as.id]));
  if (actsAs.length === 0) return { shown: directory?.featured ?? [], more: directory?.all ?? [] };
  const walkthrough = new Set([...actsAs, actingId]);
  const everyone = allMembers(directory);
  return {
    shown: everyone.filter((member) => walkthrough.has(member.id)),
    more: everyone.filter((member) => !walkthrough.has(member.id)),
  };
}

/** An org's name from any of its members, else the slug, capitalized. */
export function orgName(directory: MemberDirectory | undefined, orgSlug: string): string {
  const member = allMembers(directory).find((candidate) => candidate.org.slug === orgSlug);
  if (member !== undefined) return member.org.name;
  return orgSlug.charAt(0).toUpperCase() + orgSlug.slice(1);
}

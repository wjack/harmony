import { ChevronDown, FolderPlus, Pencil, Star, Trash2 } from "lucide-react";

import { Button } from "../shell/ui/Button";
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from "../shell/ui/Menu";

const ICON = { size: 16, strokeWidth: 1.6 } as const;

/** A chat's top bar: the title with its menu on the left, Share on the right. */
export function ChatHeader({ title, onUnavailable }: { title: string; onUnavailable: (feature: string) => void }) {
  const unavailable = (feature: string) => () => {
    onUnavailable(feature);
  };
  return (
    <header className="flex h-12 shrink-0 items-center justify-between gap-3 pr-3 pl-4">
      <Menu modal={false}>
        <MenuTrigger className="inline-flex h-8 min-w-0 items-center gap-1.5 rounded-control px-2 text-[14px] text-ink transition-colors outline-none hover:bg-hover focus-visible:bg-hover data-[state=open]:bg-hover">
          <span className="truncate">{title}</span>
          <ChevronDown size={15} strokeWidth={1.6} className="shrink-0 text-muted" />
        </MenuTrigger>
        <MenuContent align="start" sideOffset={4} className="min-w-[200px]">
          <MenuItem icon={<Star {...ICON} />} onSelect={unavailable("Starring chats")}>
            Star
          </MenuItem>
          <MenuItem icon={<Pencil {...ICON} />} onSelect={unavailable("Renaming chats")}>
            Rename
          </MenuItem>
          <MenuItem icon={<FolderPlus {...ICON} />} onSelect={unavailable("Projects")}>
            Add to project
          </MenuItem>
          <MenuSeparator />
          <MenuItem
            icon={<Trash2 {...ICON} className="text-danger" />}
            className="text-danger"
            onSelect={unavailable("Deleting chats")}
          >
            Delete
          </MenuItem>
        </MenuContent>
      </Menu>
      <Button size="sm" onClick={unavailable("Sharing chats")} className="h-8 px-3">
        Share
      </Button>
    </header>
  );
}

import { Blocks, Camera, Check, Feather, FolderPlus, Globe, Paperclip, Plus, SearchCheck, Waypoints } from "lucide-react";

import {
  Menu,
  MenuCheckItem,
  MenuContent,
  MenuItem,
  MenuSeparator,
  MenuSub,
  MenuSubContent,
  MenuSubTrigger,
  MenuTrigger,
} from "../shell/ui/Menu";

const ICON = { size: 16, strokeWidth: 1.6 } as const;
const STYLES = ["Learning", "Concise", "Explanatory", "Formal"] as const;

/**
 * The composer's `+` menu, as in Claude. Harmony is the one connector that works here;
 * every other entry opens the not-available dialog.
 */
export function PlusMenu({ onUnavailable }: { onUnavailable: (feature: string) => void }) {
  const unavailable = (feature: string) => () => {
    onUnavailable(feature);
  };
  return (
    <Menu modal={false}>
      <MenuTrigger
        aria-label="Add files, connectors and more"
        className="inline-flex size-8 items-center justify-center rounded-control text-secondary transition-colors outline-none hover:bg-hover hover:text-ink focus-visible:bg-hover data-[state=open]:bg-hover"
      >
        <Plus size={20} strokeWidth={1.5} />
      </MenuTrigger>
      <MenuContent align="start">
        <MenuItem icon={<Paperclip {...ICON} />} onSelect={unavailable("Adding files")}>
          Add files or photos
        </MenuItem>
        <MenuItem icon={<Camera {...ICON} />} onSelect={unavailable("Screenshots")}>
          Take a screenshot
        </MenuItem>
        <MenuSub>
          <MenuSubTrigger icon={<FolderPlus {...ICON} />}>Add to project</MenuSubTrigger>
          <MenuSubContent>
            <MenuItem icon={<FolderPlus {...ICON} />} onSelect={unavailable("Projects")}>
              Start a new project
            </MenuItem>
          </MenuSubContent>
        </MenuSub>
        <MenuSeparator />
        <MenuSub>
          <MenuSubTrigger icon={<Blocks {...ICON} />}>Connectors</MenuSubTrigger>
          <MenuSubContent>
            <MenuCheckItem checked icon={<Waypoints {...ICON} className="text-secondary" />}>
              Harmony
            </MenuCheckItem>
            <MenuSeparator />
            <MenuItem icon={<Blocks {...ICON} />} onSelect={unavailable("Managing connectors")}>
              Manage connectors
            </MenuItem>
          </MenuSubContent>
        </MenuSub>
        <MenuSeparator />
        <MenuItem icon={<SearchCheck {...ICON} />} onSelect={unavailable("Research")}>
          Research
        </MenuItem>
        <MenuItem
          icon={<Globe {...ICON} className="text-link" />}
          className="text-link"
          onSelect={unavailable("Web search")}
          shortcut={<Check size={15} strokeWidth={2} className="text-link" />}
        >
          Web search
        </MenuItem>
        <MenuSub>
          <MenuSubTrigger icon={<Feather {...ICON} />}>Use style</MenuSubTrigger>
          <MenuSubContent>
            <MenuItem
              icon={<Feather {...ICON} className="text-link" />}
              className="text-link"
              onSelect={unavailable("Styles")}
              shortcut={<Check size={15} strokeWidth={2} className="text-link" />}
            >
              Normal
            </MenuItem>
            {STYLES.map((style) => (
              <MenuItem key={style} icon={<Feather {...ICON} />} onSelect={unavailable("Styles")}>
                {style}
              </MenuItem>
            ))}
            <MenuSeparator />
            <MenuItem icon={<Plus {...ICON} />} onSelect={unavailable("Styles")}>
              Create &amp; edit styles
            </MenuItem>
          </MenuSubContent>
        </MenuSub>
      </MenuContent>
    </Menu>
  );
}

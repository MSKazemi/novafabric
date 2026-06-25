import { useState } from 'react';
import CommandList from '../commands/CommandList';
import CommandBuilder from '../commands/CommandBuilder';
import { COMMANDS, type CommandDef } from '../commands/commandRegistry';

export default function CommandsTab() {
  // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
  const [selected, setSelected] = useState<CommandDef>(COMMANDS[0]!);

  return (
    <div className="flex h-full overflow-hidden -m-4">
      <CommandList selectedId={selected.id} onSelect={setSelected} />
      <CommandBuilder key={selected.id} cmd={selected} />
    </div>
  );
}

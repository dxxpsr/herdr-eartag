# herdr-eartag

A Herdr plugin that automatically renames Herdr's top tabs to match what you are doing in each tab.

## Naming rules

The first rule that matches wins. If a tab has several panes, the **focused pane** decides the name.

| Situation | Tab name | Example |
|---|---|---|
| You named the tab yourself | Kept as is | `deploy work` |
| Claude is running | Claude session name (auto title, or the name set with `/rename`) | `Herdr eartag` |
| Another AI agent (codex, gemini, ...) | Title reported by the agent, otherwise `agent · folder` | `codex · herdr-eartag` |
| Connected via `ssh`/`mosh` | `ssh · server` | `ssh · prod` |
| └ remote shell sends a `user@host:path` title | `server · remote folder` | `prod · app` |
| A program is running in the shell | `program · folder` | `vim · herdr-eartag`, `npm · web` |
| Shell is idle | Current folder name (`~` for the home folder) | `herdr-eartag`, `~` |
| No information available | Current name is kept | — |

- Names wider than 30 columns (CJK characters count as 2) are truncated with `…`.
- The **program name** is the leading process of the command the shell ran. `npm run dev` shows as `npm`, `python3 app.py` as `app.py`.
- The **ssh server name** comes from the command you typed. With an `~/.ssh/config` alias (`ssh prod`), the alias is shown.
  The remote folder is shown only if the remote shell sets the terminal title (e.g. default bash on Ubuntu). Otherwise the name stays `ssh · server`.
- No external dependencies: only the standard library of the system `python3` (3.9+).

## Install / uninstall

```bash
herdr plugin link ~/projects/herdr-eartag           # link (path to the plugin folder)
herdr plugin action invoke herdr-eartag.start       # start the daemon now (afterwards it starts with Herdr)

herdr plugin action invoke herdr-eartag.stop        # stop the daemon before unlinking
herdr plugin unlink herdr-eartag                    # unregister
```

## Actions

```bash
herdr plugin action invoke herdr-eartag.status   # daemon state + recent log lines
herdr plugin action invoke herdr-eartag.resync   # recompute all tab names once
herdr plugin action invoke herdr-eartag.stop     # stop the daemon (tab names stay as they are)
herdr plugin log list --plugin herdr-eartag      # show action output
```

After changing the code, restart the daemon with `stop` → `start` to load the new code.

## Manual names vs. automatic names

- Once you rename a tab yourself, the plugin leaves it alone.
- **To hand a tab back to the plugin, rename it to its position number** (e.g. `2` for the second tab in the workspace).
- Note: **names made only of digits** act as the "manage automatically" signal. If an earlier tab is closed and a tab's position becomes equal to that number, it may get an automatic name.

How it tells them apart: Herdr's tab info has no "renamed by the user" flag, so the plugin remembers, per tab, the last 2 names it set.
If the current name is neither one of those nor the tab's position number (Herdr's default name), it is treated as a manual name.
After a Herdr restart, tab IDs change, so the plugin falls back once to a per-workspace list of the names it set (last 50) to recognize its own tabs again.

## How it works

1. When Herdr starts, `[[startup]]` runs `main.py start`, which launches a background daemon.
2. The daemon subscribes to pane and tab events through the Herdr socket (`events.subscribe`).
3. On events (batched over 0.15 s), or after 5 s of silence, it reads a snapshot and updates tab names.
   Right before renaming, it checks once more that the tab name has not changed in the meantime.
4. If the connection drops, it retries for 10 s and exits if that fails. It starts again with the next Herdr launch.
   A lock file prevents duplicate daemons, and each Herdr session (`--session`) keeps its own records.

State and logs live in Herdr's plugin state folder: `labels-*.json` (remembered names) and `eartag.log` (log, rotated at 1 MB).
Recent log lines are shown by the `status` action.

## Development

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_no_personal_paths.py` checks that no machine-specific information (user name, host name, home path, etc.) ends up in the repository.

# herdr-eartag

A Herdr plugin that automatically renames Herdr's top tabs to match what you are doing in each tab.

## Naming rules

The first rule that matches wins. If a tab has several panes, the **focused pane** decides the name.

| Situation | Tab name | Example |
|---|---|---|
| You pinned the tab, or named it yourself | Kept as is | `deploy work` |
| Claude is running | Claude session name (auto title, or the name set with `/rename`) | `Herdr eartag` |
| Another AI agent (codex, gemini, ...) | Title reported by the agent, otherwise `agent · folder` | `codex · herdr-eartag` |
| Connected via `ssh`/`mosh` | `ssh · server` | `ssh · prod` |
| └ remote shell sends a `user@host:path` title | `server · remote folder` | `prod · app` |
| A program is running in the shell | `program · folder` | `vim · herdr-eartag`, `npm · web` |
| Shell is idle | Current folder name (`~` for the home folder) | `herdr-eartag`, `~` |
| No information available | Current name is kept | — |

- **AI agent status** is shown as one mark in front of the name, on every tab with an agent (pinned and hand-named tabs too).
  Herdr shows agent status only in its sidebar, so this is what tells agent tabs apart in the tab bar.

  The marks are the ones Herdr's sidebar uses with `status_indicators = "symbols"`:

  | Mark | Status | Example |
  |---|---|---|
  | `◐` | working | `◐ Herdr eartag` |
  | `×` | waiting for you (approval, question) | `× Herdr eartag` |
  | `✓` | finished, not looked at yet | `✓ Herdr eartag` |
  | `○` | idle | `○ Herdr eartag` |
  | — | no agent in the tab | `herdr-eartag` |

  The mark is never counted as part of the name: changing it does not make the plugin think you renamed the tab,
  and a hand-named tab only gets the mark added or removed, never shortened.
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
herdr plugin action invoke herdr-eartag.pin      # keep the current tab's name as is
herdr plugin action invoke herdr-eartag.release  # hand the current tab back to automatic naming
herdr plugin log list --plugin herdr-eartag      # show action output
```

`pin` and `release` act on the tab the action was invoked from (from the CLI, the focused tab).
To use them from the keyboard, bind them in Herdr's `config.toml` and run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+p"
type = "plugin_action"
command = "herdr-eartag.pin"
description = "pin tab name"

[[keys.command]]
key = "prefix+shift+p"
type = "plugin_action"
command = "herdr-eartag.release"
description = "release tab name"
```

After changing the code, restart the daemon with `stop` → `start` to load the new code.

## Manual names vs. automatic names

- Once you rename a tab yourself, the plugin leaves it alone.
- To freeze an automatic name where it is, run **`pin`**. A pinned tab is never renamed, even if its name is a number.
  Pins survive a Herdr restart (the tab is found again by its name; with two same-named tabs, the leftmost one keeps the pin).
- **To hand a tab back to the plugin, run `release`** (works for pinned and hand-named tabs, and renames it right away).
  Renaming a tab to its position number (e.g. `2` for the second tab) still works too.
- Note: an unpinned tab whose name is **made only of digits** counts as "manage automatically" when the number equals its position,
  because that is Herdr's default name for a new tab. Pin a tab if you want a number as its name.

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

State and logs live in Herdr's plugin state folder: `labels-*.json` (remembered names and pins) and `eartag.log` (log, rotated at 1 MB).
Recent log lines are shown by the `status` action.

## Development

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_no_personal_paths.py` checks that no machine-specific information (user name, host name, home path, etc.) ends up in the repository.

# Downloads for Omarchy

A download manager that lives in the Omarchy bar. Paste a link, or right-click the icon to grab the one on your clipboard, then watch, pause, resume or retry it from the panel.

![Downloads panel](preview.png)

Downloads don't run inside the bar. Omarchy's shell reloads its plugins whenever a plugin file changes, and restarts on updates. Anything the bar started would die with it. Instead, a small engine (`dmd`, driving [aria2](https://aria2.github.io/)) runs as a systemd user service, and the bar only shows its state and sends it commands. That's what makes downloads survive:

- shell restarts, plugin reloads and Omarchy updates;
- the engine or aria2 crashing (systemd restarts it; the download resumes where it stopped);
- network drops and suspend: retries wait for NetworkManager to report a connection instead of giving up;
- reboots: unfinished downloads pick up again at login.

## Install

```bash
omarchy plugin add https://github.com/puneethkethanapalli/dm --enable
```

Then click the download icon in the bar and press **Set up**. That installs `aria2`, starts the `dm` user service, and puts the `dm` command in `~/.local/bin`. You can also run `./setup` from the plugin folder yourself. Re-run it after updating the plugin, or press **Restart engine** when the panel says the engine is out of date.

## Use

- **Panel:** paste a link and press Enter, or press **Clipboard**.
- **Bar icon:** left-click opens the panel; right-click downloads the link on the clipboard.
- **Command line:**

  ```bash
  dm add https://example.com/file.iso                    # into your Downloads folder
  dm add URL --dir ~/ISOs --name arch.iso --sha256 <hex> # fails if the checksum differs
  dm add URL --header 'Cookie: session=…'                # for links that need a login
  dm add --clipboard
  dm ls                     # IDs can be shortened to any unique prefix
  dm pause ID|all · dm resume ID|all · dm retry ID · dm cancel ID
  dm remove ID|finished     # drop from the list; the files stay
  dm open ID · dm reveal ID
  dm status
  ```

- **Key bindings** (in `~/.config/hypr/bindings.lua`):

  ```lua
  o.bind("SUPER + ALT + D", "Download clipboard link", "omarchy-shell downloads clipboard")
  o.bind("SUPER + SHIFT + D", "Downloads", "omarchy-shell shell toggle io.github.puneethkethanapalli.dm")
  ```

## Settings

Optional, in `~/.config/dm/config.json`. The engine reads this file when it starts, so restart the engine after editing it (`systemctl --user restart dm`).

| Key | Default | |
|---|---|---|
| `download_dir` | your XDG Downloads folder | where finished files go |
| `max_concurrent` | `3` | downloads running at once |
| `connections_per_server` | `8` | parallel connections per download |
| `max_retries` | `5` | retries after network or server errors (5 s, 15 s, 45 s, 2 min, 5 min apart) |
| `speed_limit` | `"0"` | overall cap in aria2 syntax, e.g. `"2M"`; `"0"` is unlimited |
| `inhibit_sleep` | `false` | hold off idle suspend while downloading |
| `notify` | `true` | notify when a download finishes or fails |
| `keep_finished` | `50` | finished downloads kept in the list |

To hide the bar icon while nothing is downloading, run `omarchy bar set io.github.puneethkethanapalli.dm hideWhenIdle true --json`.

## How it stays reliable

- **One source of truth.** The job list lives in `~/.local/state/dm/jobs.json` and is written atomically. aria2 only holds the transfers currently queued or running; everything else gets resubmitted from the list after a restart.
- **No half-written files.** Each download goes into its own `.dm-partial/<id>/` folder inside the destination. When it completes, it's moved into place without overwriting anything (`name (1).ext` if the name is taken), and the move is safe to repeat after a crash.
- **No silently mixed files.** aria2 resumes interrupted transfers without checking whether the file changed on the server in the meantime. dm records the file's ETag, Last-Modified and size, and checks them before every resume and again at completion. If they changed, it starts over.
- **Links that can't resume are marked.** Some websites can only send a file from the start. dm finds this out as soon as a download begins and labels it "can't resume" in the panel and in `dm ls`. Pausing one warns that it will start over, and every restart begins cleanly from the first byte.
- **Failures you can act on.** Network and server errors (timeouts, 5xx, DNS) retry with backoff, and wait without using up retries while you're offline. A 404, 403, full disk or checksum mismatch fails at once with a plain reason.
- **Locked down.** aria2's control interface listens only on localhost, needs a random secret kept in a 0600 file, and has no on-completion hooks. The bar and CLI talk to the engine over a 0600 Unix socket in `$XDG_RUNTIME_DIR/dm/`. Only http(s), ftp and sftp links are accepted.

## Limits

- Downloads you start in the browser stay in the browser. A browser extension that hands them to dm is planned.
- Links that only work inside a logged-in browser session need their cookie passed with `--header`. Those headers are also sent if the server redirects elsewhere, so only use them with links you trust.

## Tests

`tests/run.py` is a fault-injection suite. It starts a private engine against a misbehaving local server and checks each case. It covers:

- dropped connections, servers that can't resume, and files that change mid-download;
- redirects, UTF-8 file names, 404/403/503 responses and checksum mismatches;
- name clashes, pause and resume, and cancel;
- `kill -9` of the engine and of aria2, network outages, a full disk, and the security settings.

```bash
python -I tests/run.py
```

It needs `aria2c`, and `unshare` for the disk-full case. All files go to a temp directory and `$XDG_RUNTIME_DIR/dm-tests`.

## Uninstall

```bash
./uninstall.sh            # stops and removes the engine; add --purge to drop the job list and settings
omarchy plugin remove io.github.puneethkethanapalli.dm
```

Downloaded files are never deleted.

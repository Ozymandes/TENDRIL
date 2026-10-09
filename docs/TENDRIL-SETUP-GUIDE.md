# TENDRIL Setup Guide for Beginners

Control your AI coding agents (like Claude Code) from your Android phone.
The agents run on your Linux computer. Your phone is only the remote control.

---

## Table of contents

1. [Terms you need to know](#1-terms-you-need-to-know)
2. [How it all fits together](#2-how-it-all-fits-together)
3. [What you need before you start](#3-what-you-need-before-you-start)
4. [Part A: Prepare the computer](#4-part-a-prepare-the-computer)
5. [Part B: Prepare the phone](#5-part-b-prepare-the-phone)
6. [Part C: Run the setup script](#6-part-c-run-the-setup-script)
7. [Part D: Test that everything works](#7-part-d-test-that-everything-works)
8. [Daily use](#8-daily-use)
9. [Notifications (optional)](#9-notifications-optional)
10. [Troubleshooting](#10-troubleshooting)
11. [Cheat sheet](#11-cheat-sheet)

---

## 1. Terms you need to know

| Term | What it is | Why you need it |
|---|---|---|
| **Host** (or "computer") | Your Linux machine. It can be a desktop, a laptop, or a server (VPS). | The agents run here. |
| **Agent** | An AI coding tool, like **Claude Code**, Codex, or Pi. | It does the actual work. |
| **Terminal** | A text window where you type commands. | You use it on both the computer and the phone. |
| **Termux** | A terminal app for Android. | It is your terminal on the phone. |
| **Termux:Widget** | A small add-on app for Termux. | It gives you a one-tap home-screen button. |
| **Tailscale** | A free private network (VPN) between your devices. | It lets the phone reach the computer safely, from anywhere. |
| **SSH** | A secure way to log in to another computer from a terminal. | The phone uses it to log in to the computer. |
| **SSH key** | A pair of files that proves who you are. It replaces typing a password. | You log in without a password, and it is safer. |
| **Mosh** | A smarter version of SSH for mobile. | It survives weak signal and switching networks. |
| **Herdr** | A program that keeps agent sessions alive on the computer. | Your agents keep running when you disconnect. |
| **Workspace** | One session inside Herdr, usually one project with one agent. | You switch between workspaces from the phone. |
| **TENDRIL** | A small set of scripts that connect all the parts. | It gives you the phone menu and notifications. |
| **remote-agents** | TENDRIL's menu program. It runs on the computer. | It shows your workspaces and lets you open one. |
| **tendril** | TENDRIL's command on the phone. | You type it to open the menu. |
| **herdr-notify** | TENDRIL's watcher. It runs on the computer. | It tells your phone when an agent is done or needs you. |
| **ntfy** | A free push-notification service and Android app. | It delivers the alerts to your phone. |
| **ntfy topic** | A long random name, like a private channel. | Only devices that know it get your alerts. |
| **Alias** | A short nickname for your computer, like `home`. | You type `home` instead of a long address. |

---

## 2. How it all fits together

```
 ANDROID PHONE                                 LINUX COMPUTER
┌──────────────────────┐                      ┌───────────────────────────────┐
│ Termux               │                      │ Herdr                         │
│   └─ tendril ────────┼── Tailscale (VPN) ──>│   ├─ workspace 1: claude      │
│        (Mosh / SSH)  │                      │   └─ workspace 2: shell       │
│                      │                      │                               │
│                      │                      │ remote-agents (the menu)      │
│                      │                      │   └─ reads Herdr's state      │
│ ntfy app  <──────────┼──── ntfy.sh ─────────┼── herdr-notify (the watcher)  │
└──────────────────────┘                      └───────────────────────────────┘
```

### Who talks to whom

1. You type `tendril` on the phone. It connects over **Tailscale** with **Mosh** (or **SSH** if Mosh fails).
2. The computer starts **remote-agents**, which lists your **Herdr** workspaces.
3. You pick one and type to the agent. When you leave, Herdr keeps it running.
4. **herdr-notify** sends an alert through **ntfy** when an agent finishes or needs you.

> 💡 **Key idea:** The phone never runs the agent. Closing the phone, losing signal, or turning off Termux does **not** stop your work.

---

## 3. What you need before you start

### On the computer
- [ ] Linux (these steps use Ubuntu/Debian commands)
- [ ] A normal user account, **not root** (example in this guide: `bakrianoo`)
- [ ] Internet access
- [ ] About 30 minutes

### On the phone
- [ ] An Android phone
- [ ] The **Termux** app (from **F-Droid**)
- [ ] The **Tailscale** app (Google Play)
- [ ] Optional: **Termux:Widget** (also from **F-Droid**; it is not on Google Play)
- [ ] Optional: **ntfy** app (Google Play), for notifications

### Files
- [ ] `tendril-phone-setup.sh`: the setup script that comes with this guide

> ⚠️ **Important:** Do every computer step as **your normal user**. Do not use `root` or `sudo` unless a step says so. The phone logs in as your normal user. Programs installed for root are invisible to it.

---

## 4. Part A: Prepare the computer

Do these steps in a terminal **on the computer**.

### Step A1: Install the basic tools

(These steps assume an Ubuntu/Debian computer. For a Mac, see
[MACOS.md](MACOS.md): turn on Remote Login, run `xcode-select --install`,
and `brew install mosh`.)

```sh
sudo apt update
sudo apt install -y openssh-server mosh git python3 curl
sudo systemctl enable --now ssh
```

**Expected:** no errors. The last line prints nothing, or a short "Created symlink" message.

### Step A2: Install Tailscale

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

`tailscale up` prints a link. Open it in a browser and log in. You can use a Google account.

Now get the computer's Tailscale address:

```sh
tailscale ip -4
```

**Expected:** an address that starts with `100.`, for example:

```
100.66.33.109
```

📝 **Write this address down.** You need it later.

### Step A3: Check your username

```sh
whoami
```

**Expected:** your username, for example:

```
bakrianoo
```

📝 **Write this down too.**

### Step A4: Install and log in to Claude Code

```sh
curl -fsSL https://claude.ai/install.sh | bash
claude
```

Follow the login steps on the screen. Then type `/exit` to leave.

> 💡 You do **not** need to install Herdr or TENDRIL by hand. The setup script does it for you in Part C.

### Step A5 (only if you use a firewall)

If you use `ufw`, allow Mosh on the Tailscale network:

```sh
sudo ufw allow in on tailscale0 to any port 60000:61000 proto udp
```

**Expected:**

```
Rule added
```

### Step A6 (only on a server or VPS)

Keep the notification watcher running after you log out:

```sh
sudo loginctl enable-linger $USER
```

---

## 5. Part B: Prepare the phone

Typing on a phone is slow. So we will type only **5 short commands** on the phone. After that, you will control the phone from the computer's keyboard.

### Step B1: Connect Tailscale on the phone

1. Open the **Tailscale** app.
2. Log in with the **same account** you used on the computer.
3. Make sure the switch says **Connected**.
4. Find the phone's address in the app. It starts with `100.`.

📝 **Write down the phone's address.**

### Step B2: Turn on the phone's SSH server (type in Termux)

```sh
pkg update -y
pkg install -y openssh
passwd
whoami
sshd
```

| Command | What it does | What you do |
|---|---|---|
| `pkg update -y` | Refreshes the package list. | Wait. If it asks about a config file, press **Enter**. |
| `pkg install -y openssh` | Installs SSH tools. | Wait until it finishes. |
| `passwd` | Sets a password for Termux. | Type a simple password twice. |
| `whoami` | Shows the Termux username. | 📝 Write it down. It looks like `u0_a123`. |
| `sshd` | Starts the SSH server on port **8022**. | Nothing. It prints nothing. |

✅ **That is all the typing on the phone.**

---

## 6. Part C: Run the setup script

Now go back to the **computer**. Replace `u0_a123` and `100.x.x.x` with **your phone's** username and address.

### Step C1: Send the script to the phone

Run this from the folder that contains the script:

```sh
scp -P 8022 tendril-phone-setup.sh u0_a123@100.x.x.x:
```

Type `yes` if asked "Are you sure…". Then type the **Termux password** from Step B2.

**Expected:** no error. The file is now on the phone.

### Step C2: Log in to the phone from the computer

```sh
ssh -p 8022 u0_a123@100.x.x.x
```

Type the **Termux password** again.

**Expected:** the prompt changes to `~ $`. You are now typing **inside the phone**, with the computer's keyboard.

### Step C3: Run the script

```sh
bash tendril-phone-setup.sh
```

The script asks 4 questions:

| Question | What to type |
|---|---|
| `Computer's Tailscale name or 100.x IP` | The **computer's** address from Step A2. Example: `100.66.33.109` |
| `Your Linux username on that computer` | Your username from Step A3. Example: `bakrianoo` |
| `Short name for this computer [home]` | Press **Enter** to keep `home` |
| `TENDRIL folder on the computer [TENDRIL]` | Press **Enter** to keep `TENDRIL` |

### What the script does

| Step | What happens | What you do |
|---|---|---|
| 1 | Installs SSH and Mosh on the phone. | Wait. |
| 2 | Creates the phone's SSH key. | Wait. |
| 3 | Saves the computer as `home`. | Wait. |
| 4 | Copies the key to the computer. | Type your **computer** password **once**. |
| 5 | Sets up the computer: installs Herdr, downloads TENDRIL, runs the TENDRIL installer. | Press **Enter** for the 3 text questions. Type `y` for **every** yes/no question (ntfy, watcher, keybindings). |
| 6 | Installs the `tendril` and `agent` commands and the deep-link helper on the phone. | Wait. |
| 7 | Creates a home-screen shortcut. | Wait. |
| 8 | Checks everything. | Read the results. |

**Expected output at the end:**

```
== Verifying TENDRIL (alias: home) ==
 Phone:
   [ OK ] tendril launcher installed (~/bin/tendril)
   [ OK ] mosh installed
 Connection:
   [ OK ] SSH key login to 'home' works (as bakrianoo)
   [ OK ] mosh connection works
 Computer:
   [ OK ] herdr installed (herdr 0.9.x)
   [WARN] Herdr server is not running (the menu will be empty)
   [ OK ] remote-agents (the menu) installed
   [ OK ] menu found a compatible herdr (...)
   [ OK ] notifications configured (notify.env)
   [ OK ] notification watcher is running

 Result: 9 ok, 1 warnings, 0 failed
 All required checks passed. Close and reopen Termux, then type: tendril
```

| Label | Meaning |
|---|---|
| `[ OK ]` | Good. |
| `[WARN]` | Works, but something optional is missing. Read the `tip:` line. |
| `[FAIL]` | Must be fixed. Run the command on the `fix:` line, then run the script again. |

> 💡 The "Herdr server is not running" warning is normal right now. You start Herdr in Part D.

> ⚠️ Yes/no questions default to **No**. Pressing Enter skips them, and **ALT then D** will not work.

> 💡 You can run the script again at any time. It is safe. It also updates TENDRIL.

### Step C4: Turn off the phone's SSH server

You do not need it anymore. Still in the same session, type:

```sh
pkill sshd
```

The connection closes. That is expected.

---

## 7. Part D: Test that everything works

### Test 1: Start an agent on the computer

On the **computer**:

```sh
herdr
```

**Expected:** the Herdr screen opens. It has a list of spaces on the left.

Inside Herdr, start Claude in a project folder:

```sh
mkdir -p ~/Projects/my-project && cd ~/Projects/my-project
claude
```

Leave Herdr **without stopping Claude**: press **Ctrl+B**, then **D**.

### Test 2: Open the menu on the phone

Close Termux fully and open it again. Then type:

```sh
tendril
```

**Expected:** a box-style menu. It lists your workspace (for example `my-project`) with a colored dot:

| Dot | Meaning |
|---|---|
| Teal ● | The agent is working. |
| Lime ◆ | The agent is blocked and needs your input. |
| Lime ✓ | The agent is done. |
| Gray ○ | Idle. |

### Test 3: Talk to the agent from the phone

1. Press **↑ / ↓** to pick the workspace.
2. Press **Enter**.
3. You see the same Claude screen as on the computer.
4. Type a task, for example:
   > Create a simple index.html landing page for a coffee shop

### Test 4: Walk away (the main test)

1. While Claude is working, tap **ALT**, then **D**. You are back at the menu.
2. Press **Q** to quit.
3. Close Termux completely.
4. Wait 2–3 minutes.
5. Open Termux and type `tendril`. Open the workspace again.

**Expected:** Claude kept working while you were away. ✅ **This is the whole idea of TENDRIL.**

### Test 5: Check again at any time

```sh
bash tendril-phone-setup.sh --check
```

This only checks. It does not change anything.

---

## 8. Daily use

### Every day

1. Start Herdr and your agents on the computer (or leave them running).
2. On the phone, tap the **tendril** widget, or type `tendril`.
3. Pick a workspace and press **Enter**.
4. Tap **ALT**, then **D** to go back to the menu. The agent keeps running.
5. Press **Q** to quit. Close Termux.

### Menu keys

| Key | Action |
|---|---|
| **↑ / ↓** then **Enter** | Open a workspace |
| **N** | Open a new shell workspace (then start an agent, like `claude`) |
| **I** | Show computer info |
| **R** | Refresh the list |
| **?** | Help |
| **Q** | Quit |

### Inside a workspace

| Keys | Action |
|---|---|
| **ALT**, then **D** | Back to the menu (agent keeps running) |
| **Ctrl+B**, then **D** | Same thing (backup method) |
| **Ctrl+B**, then **1–9** | Jump to workspace 1–9 |
| **Ctrl+B**, then **w** | Workspace picker |
| **Ctrl+B**, then **n** / **p** | Next / previous tab |

> 💡 In Termux, swipe the extra keys bar to find **CTRL**, **ALT**, and the arrows. Tap **ALT** first, then the letter.

### Add the home-screen button

1. Install **Termux:Widget** from F-Droid.
2. Long-press your home screen → **Widgets** → **Termux:Widget**.
3. Tap **tendril** in the widget. The menu opens with no typing.

---

## 9. Notifications (optional)

Get an alert when an agent finishes or needs you.

> Your topic survives updates. Re-running `./install` keeps the existing
> `notify.env` byte-for-byte unless you explicitly choose **Rotate
> notification topic** — subscribed phones never need to resubscribe after
> a normal `git pull && ./install`.

### Step N1: Find your topic (on the computer)

```sh
grep NTFY_TOPIC ~/.config/remote-agents/notify.env
```

**Expected:**

```
NTFY_TOPIC=tendril-AbCdEf123456
```

> ⚠️ Keep this name private. Anyone who knows it can read or send your alerts.

### Step N2: Subscribe on the phone

1. Install the **ntfy** app.
2. Tap **+** (Subscribe to topic).
3. Type the topic **exactly**, for example `tendril-AbCdEf123456`.
4. Tap **Subscribe**.

### Step N3: Send a test (on the computer)

```sh
herdr-notify --test
```

**Expected:** a notification appears on your phone within a few seconds.

### What alerts you get

| Agent changes from… | Alert |
|---|---|
| working → idle or done | **Task complete** |
| anything → blocked | **Input required** |

Alerts contain only these words and the workspace name. They never contain your code or prompts.

### Tap an alert to jump into that session (deep links)

The setup script already copied the deep-link helper to
`~/bin/termux-url-opener`. Android cannot open Termux from a bare
notification tap, so today there are two supported paths: **share** any
TENDRIL URL (like `https://…/.tendril/w15`; `w15` is an example — use a
live id shown by the selector) to the Termux app from any
share sheet (works right away, no extra setup), or set up Tasker or
MacroDroid for real one-tap behavior (needs the ntfy "broadcast
messages" setting). What each path can and cannot do is documented in
[docs/DEEPLINK.md](DEEPLINK.md).

---

## 10. Troubleshooting

### First step for any problem

Run the check on the phone. It tells you what is wrong and how to fix it:

```sh
bash tendril-phone-setup.sh --check
```

### Common problems

| What you see | Why | How to fix |
|---|---|---|
| `key-auth check failed` | Tailscale is off on the phone, or the computer is asleep. | Turn on the Tailscale app. Wake the computer. Try `ssh home echo OK`. |
| It asks for the **root** password | The phone has an old `home` entry with the wrong user. | Run `bash tendril-phone-setup.sh` again. It replaces the old entry. |
| `[mosh is exiting.]` right after connecting | The computer could not start the menu. | Run `tendril ssh` to see the real error. Then run the setup script again. |
| `remote-agents: command not found` | TENDRIL is not installed for your user, or the phone cannot find it. | Run `bash tendril-phone-setup.sh` again. |
| `herdr: command not found` on the computer | Herdr is not installed, or not on your PATH. | Run `source ~/.bashrc`, or log out and back in. If it still fails, run the setup script again. |
| `herder: command not found` | Typo. | The command is `herdr` (no "e" before the "r"). |
| The menu says `cannot reach the Herdr server` | Herdr is not running. | Run `herdr` on the computer. |
| The menu is empty | No workspaces yet. | Run `herdr` on the computer and start an agent. Or press **N** in the menu. |
| Mosh connects, then freezes | The firewall blocks Mosh. | Use `tendril ssh` for now. Then do Step A5. |
| ALT then D does nothing | The key went to the wrong place. | Use **Ctrl+B**, then **D**. |
| No notifications | Wrong topic, or the watcher is stopped. | Check the topic in the ntfy app. On the computer: `tendril service status` (Linux also: `systemctl --user status herdr-notify`) |
| Strange symbols on screen | The font lacks some characters. | In Termux: `pkg install font-firas-mono` |

### Useful check commands

**On the phone:**

```sh
ssh home echo OK                                   # Expected: OK (no password)
ssh -G home | grep -E '^(user|hostname) '          # Expected: your username and computer address
tendril ssh                                        # Shows errors that Mosh hides
```

**On the computer:**

```sh
herdr --version                                    # Expected: herdr 0.9.x
herdr status                                       # Expected: status: running
cd ~/TENDRIL && ./install --doctor                 # Full read-only check
```

---

## 11. Cheat sheet

```
┌─────────────────────────── PHONE ───────────────────────────┐
│ tendril              open the menu                          │
│ tendril ssh          open the menu with SSH (if Mosh fails) │
│ ↑ ↓ Enter            open a workspace                       │
│ ALT then D           back to the menu (agent keeps running) │
│ N / R / Q            new shell / refresh / quit             │
│ bash tendril-phone-setup.sh --check     check everything    │
├────────────────────────── COMPUTER ─────────────────────────┤
│ herdr                start Herdr                            │
│ Ctrl+B then D        leave Herdr (agents keep running)      │
│ herdr-notify --test  send a test notification               │
│ cd ~/TENDRIL && ./install --doctor      check the computer  │
└─────────────────────────────────────────────────────────────┘
```

### Update TENDRIL later

Run the setup script again from the phone:

```sh
bash tendril-phone-setup.sh
```

It updates TENDRIL on the computer and keeps your settings.

### Remove TENDRIL

On the computer:

```sh
cd ~/TENDRIL && ./uninstall
```

It asks before it deletes your settings.

---

*TENDRIL is open source (MIT license): https://github.com/Ozymandes/TENDRIL*

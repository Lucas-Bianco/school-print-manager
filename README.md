# School Print Manager

A 3D print queue for a school lab, running on a Raspberry Pi.

- Students upload sliced files from any computer on the school network.
- Prints of **30 min or less**, and anything sent **after 1:30 PM**, go straight into the queue.
  Everything else waits for a teacher.
- A **TV** shows the live queue. A **7" touch screen** at the printers controls them.
- When a print finishes, someone takes it off and taps **Bed cleared**. The next job then starts on its own.
  Nothing ever starts without that tap.
- Anyone can report a failed print in two taps, which stops the printer.

Works with Prusa printers through **PrusaLink** (MK4, CORE One, CORE One L, XL, MINI) and
Bambu Lab printers through **LAN mode** (X1C, P1S, ...).

| Page | Who | Where |
| --- | --- | --- |
| `/` | Students: upload, see my prints | Any computer |
| `/board` | Everyone: live queue | The TV |
| `/panel` | At the printers: bed cleared, pause, report failure, approvals (PIN) | Pi touch screen |
| `/teacher` | Teachers: approve, reorder, stop, re-queue | Any computer |
| `/admin` | Admin: printers, rules, teachers, CSV export | Any computer |

## Install on the Raspberry Pi

You need Raspberry Pi OS with desktop (Bookworm or newer), the Pi on the school network, and
the touch screen connected.

1. Get the code. The repo is private, so log in to GitHub on the Pi first:

   ```bash
   sudo apt install -y gh git
   gh auth login
   gh repo clone Lucas-Bianco/school-print-manager ~/school-print-manager
   ```

2. Install:

   ```bash
   cd ~/school-print-manager
   ./install.sh
   ```

   This installs everything, starts the server on port 80 at every boot, and sets the
   touch screen to open the control panel. At the end it prints the addresses to use.

3. Check the Pi's time zone is right. The 1:30 PM rule uses the Pi's clock.
   Use `sudo raspi-config` → Localisation Options → Timezone.

4. Reboot: `sudo reboot`. The touch screen opens the panel.

5. On any computer, open `http://<pi-address>/admin`. Choose the admin password, then add
   teachers (name + PIN) and printers.

### The TV

Point the TV's own web browser, or a cheap streaming stick, at `http://<pi-address>/board`.
That's better than plugging the TV into the Pi: a Pi 3B+ has 1 GB of RAM, and two
full-screen browsers on it get slow.

If you do plug the TV into the Pi's HDMI port, open the board with
`deploy/kiosk.sh board` and drag it to the TV.

### Updating

```bash
cd ~/school-print-manager && git pull && ./install.sh
```

The queue lives in `data/printlab.db` and is kept when you update. A copy is saved every day in
`data/backups/`, and the last 14 days are kept.

## Adding printers

On `/admin` → Printers. Give each printer a **fixed IP address** on the network (ask IT to set a
DHCP reservation), or it may change after a power cut.

**Accepted printer models** decides which files go to which printer. It's the model name the
slicer writes into the file. If a student uploads a file no printer accepts, the error message
shows the model the file was sliced for, so you can copy it from there.

| Your printer | Type | Accepted printer models |
| --- | --- | --- |
| Prusa CORE One L (×2) | Prusa (PrusaLink) | `COREONEL` |
| Prusa CORE One | Prusa (PrusaLink) | `COREONE` |
| Prusa MK4 | Prusa (PrusaLink) | `MK4` (or `MK4S` if it was upgraded) |
| Bambu Lab X1 Carbon | Bambu Lab (LAN) | `BL-P001` |

The model codes are my best guess. Check them against the first real file you upload and
correct them if the message shows something different.

### Prusa (PrusaLink)

1. On the printer: Settings → Network. Connect it to the network and turn on PrusaLink.
2. In the PrusaLink menu, note the **IP address** and the **API key**. On older firmware use the
   **username/password** instead.
3. On `/admin`, enter those. Leave Storage as `usb` and put a USB stick in the printer: jobs are
   copied there before they start.

### Bambu Lab X1C (LAN mode)

1. On the printer: Settings → Network. Turn on **LAN Only Mode** and note the **Access Code**
   and **IP address**.
2. On recent firmware, also turn on **Developer Mode** there. Without it, other software can read
   the printer's status but can't start prints.
3. Settings → Device: note the **serial number**.
4. Make sure there's a microSD card in the printer.
5. On `/admin`, enter the IP, serial and access code. Untick **Use AMS** if you don't use the AMS.

## How students slice

| Printer | Slicer | What to upload |
| --- | --- | --- |
| Prusa | PrusaSlicer | The exported **.bgcode**, sliced for the right printer |
| Bambu X1C | Bambu Studio | **File → Export → Export plate sliced file** (`.gcode.3mf`). A normal project `.3mf` is refused. |

The print time and filament weight are read from the file, so students can't type them in wrong.

## Rules and settings

Everything is on `/admin` → Rules and settings:

| Setting | Default |
| --- | --- |
| Auto-approve prints up to | 30 min |
| Auto-approve everything sent after | 13:30 |
| Longest print allowed | 8 h |
| Prints per student per day | 3 |
| Class codes | any (empty) |

## Try it without printers

Add a printer with type **Pretend printer (testing)**. Its "Speed-up factor" makes prints run
faster, e.g. `60` turns a 1-hour print into 1 minute.

Run it on any computer with Python 3.11+:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PRINTLAB_DATA=./data .venv/bin/uvicorn printlab.main:create_app --factory --port 8000
```

Then open http://localhost:8000/admin.

Tests: `.venv/bin/pip install pytest && .venv/bin/python -m pytest`

## Status

- Tested here with pretend printers: uploads, the approval rules, the full print cycle, failure
  reports, re-queue, teacher PIN login and lockout, and every page.
- **Not yet tested on real printers.** The PrusaLink and Bambu connections follow those
  printers' local APIs, but test each printer with a short print first. If a printer won't start,
  the error shows on `/admin` and on the panel.

## Security notes

- Keep it on the school network only. Don't forward it to the internet.
- Teachers log in with a PIN. Five wrong tries locks that device out for 5 minutes.
  The touch panel locks again 60 s after the last teacher action.
- Printer passwords and access codes are stored on the Pi in `data/printlab.db`.

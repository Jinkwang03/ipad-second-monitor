# iPad Display

Use an iPad as a second monitor for a Windows 11 laptop, over Wi-Fi.

Windows extends the desktop onto the iPad like a real monitor. You can move the mouse off
the edge of the laptop screen onto the iPad, drag windows across, and type into them. The
iPad also sends input back:

- **Touch** works like a real Windows touchscreen: tap, scroll, pinch, press-and-hold to right-click.
- **Apple Pencil** works as a real Windows pen, with pressure, tilt and hover.
- **Trackpad or mouse paired with the iPad**: pointer, clicks and scrolling.
- **Keyboard paired with the iPad**: keys go to Windows, so the Korean/English IME works as usual.

The iPad needs no app. It runs in Safari, and *Add to Home Screen* turns it into a full-screen app.

```
 Windows laptop                                             iPad (Safari)
 ┌───────────────────────────┐   JPEG tiles (only what      ┌──────────────────┐
 │ virtual display ─► capture │── changed) over WebSocket ──►│ canvas + cursor  │
 │ (DXGI) ─► diff ─► encode   │                              │                  │
 │ touch/pen/mouse/keys ◄─────│◄── touch, Pencil, keys ──────│ pointer events   │
 └───────────────────────────┘                              └──────────────────┘
```

## Install

**The app (no Python needed):** on this repository's **Releases** page, download
`iPadDisplay-<version>-windows-x64.zip`. Unzip it and double-click **Install.bat**.

- It installs to `%LOCALAPPDATA%\Programs\iPad Display` (no admin rights needed) and adds
  **iPad Display** shortcuts to the desktop and the Start menu.
- `Install.bat -AutoStart` also starts it whenever you log in.
- To remove it: *Start menu → iPad Display → Uninstall iPad Display*.
- The app isn't code-signed. If Windows warns about it, choose *More info → Run anyway*.

**From the source code:** double-click `run.bat` instead. The first run creates a private
Python environment (`.venv`) and installs the requirements. To build the app yourself, see
[Building](#building).

## One-time setup

### 1. Add a virtual display to Windows

Windows only extends the desktop onto monitors it thinks are plugged in, so you need a
virtual display driver. The free, signed [Virtual Display Driver](https://github.com/VirtualDrivers/Virtual-Display-Driver)
works well. Installing it takes two steps, and **the second one is easy to miss**:

1. In **Terminal / PowerShell**, download it:
   ```powershell
   winget install --id=VirtualDrivers.Virtual-Display-Driver -e
   ```
   This only downloads the **VDD Control** app. It does not add a display yet.
2. Open **VDD Control** and click **Install**, then approve the admin prompt. winget doesn't
   add a Start-menu entry, so open the app from:
   `%LOCALAPPDATA%\Microsoft\WinGet\Packages\VirtualDrivers.Virtual-Display-Driver_Microsoft.Winget.Source_8wekyb3d8bbwe\VDD Control.exe`
   (paste that into the File Explorer address bar). iPad Display also prints this path
   when it finds the app downloaded but not installed.

A second display then appears under *Settings → System → Display*. If Windows shows the
same picture on both screens, press **Win+P** and choose **Extend**.

> Without the driver, iPad Display still works, but it **mirrors** your main screen.
> That's handy for a first test. The driver is detected automatically while the program
> is running, so you don't need to restart it.
>
> An HDMI "dummy plug" (a few dollars) also works as a second display, with no driver needed.

### 2. Match the display to the iPad (for a sharp picture)

1. Find your iPad's resolution. It's shown in iPad Display's menu (the tab on the left
   edge of the iPad screen) and printed in the PC console when the iPad connects.
   Examples: 2360 × 1640 for iPad Air 11" or iPad 10th gen, 2732 × 2048 for a 12.9"/13".
2. Make sure the virtual display offers that resolution. If it isn't in the list, add it
   in **VDD Control** or in `C:\VirtualDisplayDriver\vdd_settings.xml` (copy a
   `<resolution>` entry), then reload the driver from VDD Control.
3. In *Settings → System → Display*, select the virtual display and set:
   - **Display resolution** to the iPad's resolution
   - **Scale** to **200%**, so text is the same size as on the iPad
4. Drag the displays in that settings page so they match where the iPad physically sits
   (left or right of the laptop). That edge is where the mouse crosses over.

### 3. Start it on the PC

Start **iPad Display** from its desktop or Start menu shortcut, or with `run.bat` if you're
running from the source code. A console window opens and stays open while it runs.

When Windows Firewall asks, allow it on **private networks**.

The console shows the address to open on the iPad, with a QR code:

```
http://ipad-display.local:8765/?key=k7m2x9qp
```

This name stays the same even when the PC's IP address changes (another Wi-Fi, a hotspot, a
router restart), so a Home Screen icon made from it keeps working. Below it, the console also
lists plain number addresses for each network, such as `Wi-Fi http://192.168.0.23:8765/...`, in
case the name doesn't work on some network.

### 4. Open it on the iPad

1. Make sure the iPad is on the **same Wi-Fi** as the laptop.
2. Scan the QR code with the iPad camera, or type the address into Safari.
3. In Safari, tap **Share → Add to Home Screen**. Launching from that icon gives an app
   without the Safari bars, and it keeps your access key.
4. **Tap the screen once** to go full screen. That hides Safari's tabs and address bar and the
   iPad's status bar (time, battery). While it isn't full screen, a hint says
   *Tap anywhere for full screen*, and that first tap is not sent to Windows, so it won't click
   anything by accident.

**Stop:** press Ctrl+C in the console, or close it.

## Using it

| On the iPad | What Windows gets |
|---|---|
| Tap, drag, two-finger scroll, pinch | Touchscreen input |
| Press and hold | Right-click (standard Windows touch behaviour) |
| Apple Pencil | Pen with pressure and tilt. Hover works on Pencil models that support it. Your palm is ignored while drawing |
| Trackpad / mouse | Mouse pointer, left/right click, scrolling |
| Hardware keyboard | Keys. **⌘ acts as Ctrl** by default, so ⌘C / ⌘V copy and paste |

The **tab on the left edge** opens the menu, which has these options:
- Resolution info and a hint if the PC display doesn't match the iPad.
- Live stats: updates per second, bandwidth, and round-trip time.
- **Touch acts as a mouse:** for apps that don't handle touch well.
- **⌘ key acts as Ctrl:** turn this off to send the Windows key instead.
- **Full screen on first tap:** on by default. Turn it off if you'd rather stay windowed.
- **Full screen / Exit full screen:** switch by hand. After you exit here, taps aren't used
  to go back into full screen until you press *Full screen* again.

If full screen ends by accident (for example a swipe from the top edge), the next tap brings
it back. If iPadOS doesn't allow full screen somewhere, such as in some Home Screen apps,
the hint doesn't appear and taps go straight to Windows. The Home Screen app still has no
Safari bars; only the thin status bar remains.

The laptop's own mouse and keyboard work across both screens as usual. The iPad draws the
real Windows cursor when it's on the iPad display.

## Moving photos and files

There's no need for email or cloud apps. Everything goes directly over your local network
and uses the same access key as the screen. iPadOS doesn't let any app control other iPad
apps, so this works through Safari's own file picker and downloads.

**iPad → PC**
- Open the menu and tap **Send to PC**, then pick from *Photo Library*, *Take Photo*, or
  *Choose Files*. Or **drag photos** from the Photos or Files app (Split View / Stage
  Manager) onto the screen.
- Files are saved in `Downloads\iPad Display` on the PC (change it with `--save-dir`).
  Each file gets the date the iPad reports for it, and photos keep the date they were taken
  inside the file. A file with the same name gets `(2)` added instead of overwriting the old one.
- When a transfer finishes:
  - **Show on PC** opens File Explorer at the files.
  - **Copy on PC** puts them on the Windows clipboard, so **Ctrl+V** pastes them into any
    folder, chat or document.

**PC → iPad**
1. On the PC, select files in File Explorer and press **Ctrl+C**.
2. On the iPad, open the menu and tap **Get from PC**.
3. For photos, touch and hold one, then tap **Save to Photos**. For other files, tap
   **Download**; they go to the Files app, in *Downloads*.

## Options

```
run.bat --list                 show displays and their numbers
run.bat --monitor 2            stream display 2 (default: auto, which picks the virtual/second display)
run.bat --port 8766            use another port
run.bat --name studio-pc       open it as http://studio-pc.local instead of ipad-display.local
run.bat --no-mdns              don't publish the .local name (number addresses only)
run.bat --fps 30               cap the frame rate (default 60)
run.bat --quality 85           JPEG quality for still content (default 90)
run.bat --motion-quality 55    JPEG quality while things move (default 65; lower = faster on weak Wi-Fi)
run.bat --view-only            don't accept input from the iPad
run.bat --save-dir D:\Photos   save photos and files from the iPad there (default Downloads\iPad Display)
run.bat --new-key              make a new access key (old links stop working)
run.bat --capture gdi          force the slower GDI capture if DXGI misbehaves
run.bat --monitor test         stream a synthetic test pattern (checks the Wi-Fi connection only)
```

## Troubleshooting

**The iPad can't connect (page never loads)**
- Check that both devices are on the same Wi-Fi. Guest networks usually block device-to-device traffic.
- Windows Firewall may be blocking it. If your Wi-Fi is set to *Public*, either switch it to
  *Private* (*Settings → Network & internet → Wi-Fi → your network*), or run this once in an
  admin terminal:
  `netsh advfirewall firewall add rule name="iPad Display" dir=in action=allow protocol=TCP localport=8765`
- If `ipad-display.local` doesn't open, use one of the number addresses listed below it in
  the console. Some networks block the multicast traffic `.local` names rely on.

**Switching networks (Wi-Fi, cellular, hotspot)**
- The iPad and the PC must be on the **same network**. The iPad on cellular (LTE/5G) can't
  reach the PC.
- When the PC changes networks, the console prints the addresses that work now. The
  `ipad-display.local` link stays the same.
- **Public Wi-Fi** (cafés, campus, hotels) usually blocks devices from reaching each other.
  Turn on the PC's hotspot (*Settings → Network & internet → Mobile hotspot*), connect the
  iPad to it, and open the same `ipad-display.local` link. A cellular iPad's Personal
  Hotspot works too: connect the PC to it instead.
- A Home Screen icon you made from a number address (like `192.168.0.23`) breaks when that
  address changes. Remove it, then add the icon again from the `ipad-display.local` link.

**"That key was not accepted"**: type the key shown in the PC console. It's saved in
`%USERPROFILE%\.ipad-display-key` and stays the same between runs.

**The iPad shows the same picture as the laptop (mirroring / "duplicate mode")**
- Windows has no second display yet. The PC console and a notice on the iPad say why.
- If the driver was downloaded with winget but VDD Control's **Install** was never
  clicked, there is still no display. Do setup step 1.2.
- If Windows is set to *Duplicate*, press **Win+P** and choose **Extend**.

**The picture is blurry**: set the Windows display to the iPad's exact resolution with 200% scale (setup step 2).

**Laggy or low frame rate**: use 5 GHz Wi-Fi and keep the laptop close to the router.
Try `--motion-quality 50`. Moving content (video, fast scrolling) takes the most
bandwidth, and still content costs almost nothing.

**The iPad screen turns off**: iPadOS doesn't let a plain web page keep the screen on.
Set *Settings → Display & Brightness → Auto-Lock* to *Never* while you use it.

**Typing on the iPad keyboard does nothing**: tap the iPad screen once so the page has
keyboard focus. Some ⌘ shortcuts are reserved by iPadOS and never reach the page.

**Touch/pen don't work in an app running as administrator**: Windows blocks input from
normal programs into elevated windows. Start iPad Display as administrator if you need that
(right-click the shortcut → *Run as administrator*).

**Check that input injection works on this PC**:
`.venv\Scripts\python.exe tools\input_selftest.py`
This opens a small test window and injects touch, pen, mouse, wheel and keyboard input
into that window only.

## Building

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

This builds `dist\iPadDisplay\iPadDisplay.exe` with PyInstaller, plus
`dist\iPadDisplay-<version>-windows-x64.zip` (the app, `Install.bat` and this README).
`packaging\install.ps1` installs a local build on this PC.

**Releases:** on every push to `main`, GitHub Actions (`.github/workflows/release.yml`) runs the
tests on Windows, builds the app and publishes the zip as release `v<VERSION>`. To publish a
new version, change `VERSION` in `server.py`. Otherwise the current release is rebuilt.

## How it works

- **Capture**: DXGI Desktop Duplication (through `dxcam`) reads the chosen display only
  when it changes, and falls back to GDI if that fails. The display is rechecked every
  2 s, so adding, removing or resizing it is handled live.
- **Encoding**: frames are compared in 64 × 64 tiles. Only changed tiles are merged into
  rectangles and JPEG-encoded in parallel bands (libjpeg-turbo). Small changes like typing
  go out sharp right away (quality 90, full colour detail). Big changes like scrolling or
  video go out lighter, then get re-sent sharp once they've been still for 0.3 s.
- **Flow control**: the iPad acknowledges each frame after drawing it, and the server
  keeps at most 2 frames in flight. A slow network means fewer updates rather than growing lag.
- **Cursor**: sent separately as position plus shape, and drawn as an overlay on the iPad,
  so it moves smoothly even when the picture is busy.
- **Input**: touch and Pencil use Windows *synthetic pointer devices*, so apps see a real
  touchscreen and pen. The mouse uses `SendInput`, and keys are sent as physical scan codes.
- **The `.local` name**: a small built-in mDNS responder answers "where is
  ipad-display.local?" on each network. It replies with the PC's address on the network
  that asked, and never with addresses the iPad can't reach, such as WSL's.
- **Security**: every connection needs the access key. Anyone with the key on your
  network can control the PC, so don't share it.

| File | Purpose |
|---|---|
| `server.py` | Web server, sessions, flow control, cursor, startup |
| `capture.py` | Picks the display and grabs frames (DXGI / GDI / test pattern) |
| `tiles.py` | Tile diffing, rectangle merging, JPEG encoding, wire format |
| `win32.py` | Windows APIs: DPI, monitors, GDI capture, cursor image, input injection, network adapters |
| `mdns.py` | Answers for `ipad-display.local` so the address survives IP changes |
| `web/` | The iPad page: `index.html`, `app.js`, `style.css` |
| `tests/` | Unit and end-to-end tests: `python -m unittest discover -s tests` |
| `tools/input_selftest.py` | Checks input injection on this PC |
| `build.ps1`, `packaging/` | Builds the app; `Install.bat` / `install.ps1` / `uninstall.ps1` |
| `.github/workflows/release.yml` | Tests, builds and publishes a release on every push |

# TarkovGym

A Windows script for the gym minigame in Escape from Tarkov. It captures a selected screen region, tracks the shrinking hexagon, and clicks the left mouse button when it aligns with the target border.

Uses screen capture without reading game memory. No logs or screenshots are saved to disk. Accuracy depends on frame rate, system latency, and UI scaling.

## Installation

### Standalone Windows EXE

Build the executable by running `build.bat` on Windows with Python installed. The result is `dist/TarkovGym.exe`; Python and BAT files are not needed on the destination computer.

Double-click the EXE, select the monitor, then choose **Configure capture area** or **Start**. Setup waits for **F1** before capturing the screen. Settings are saved in `config.json` next to the EXE, not inside its temporary extraction folder. Keep the EXE in a writable folder. The config is created automatically; you do not need to ship your personal settings. Closing setup cancels it. After configuration, the launcher opens again.

The EXE also supports command-line options, for example `TarkovGym.exe --configure` and `TarkovGym.exe --click`. Start uses automatic clicks, initially paused until **F1**. The existing Python/BAT launchers remain available for source installations.

### Run from source

Requires **Windows** and **Python 3.11 or later**.

Download the repository and open PowerShell in the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

## Capture area setup

1. Run `configure.bat`.
2. Open the minigame and press **F1**. The screen is captured only after you press the key.
3. Drag to select an area. Keep the minigame roughly centered and leave enough space for the largest outer hexagon.
4. Press **Enter** to save the coordinates to `config.json`.

You can redraw the selection. Press **C** to cancel while the selection window has focus, or **F2** to cancel from any window. Cancelling leaves your existing settings unchanged.

## Usage

Run `run_auto.bat`, switch back to the game, and press **F1**.

| Key | Action |
| --- | --- |
| **F1** | Start / pause |
| **F2** | Exit |

Closing the preview window also exits the program. **Escape** does not control the script.

The preview shows the current status, the number of clicks sent, and detected green/red results for the current session. Result counters rely on image recognition. Keep the preview window outside the capture area so it does not appear in the captured image.

## Command-line options

```powershell
# Preview and timing detection only, without clicking
.\.venv\Scripts\python gym.py

# Automatic clicking without the preview window
.\.venv\Scripts\python gym.py --click --no-preview

# List monitors
.\.venv\Scripts\python gym.py --list-monitors
```

| Option | Description |
| --- | --- |
| `--click` | Enable actual left mouse clicks |
| `--configure` | Select a capture area, save it, and exit |
| `--monitor N` | Select a monitor using its index from `--list-monitors` |
| `--roi X Y W H` | Capture area in pixels relative to the selected monitor: X, Y, width, height |
| `--latency-ms 20` | Expected delay before the game processes mouse input |
| `--click-hold-ms 30` | How long to hold the left mouse button, in milliseconds |
| `--margin 2` | Inset from the target border edges, in pixels; limited by the border width |
| `--no-preview` | Hide the preview window; F1/F2 still work |

`--monitor` and `--roi` override the configuration for a single run. Without a configured monitor, the script uses the Windows primary display. Capture library indices may differ from the display numbers shown in Windows settings.

After moving the game UI or changing resolution, restart the script and reconfigure the capture area if needed.

## Project files

- `gym.py` — screen capture, detection, timing, and preview interface.
- `run_auto.bat` — start automatic clicking.
- `configure.bat` — configure the capture area.
- `requirements.txt` — Python dependencies.
- `config.example.json` — example configuration structure.

`config.json` is created during setup and stores your local display settings. It, the virtual environment, and temporary files are excluded from Git.

# Luxv Tubers

Luxv Tubers is a Python desktop app for analyzing public video links and downloading supported media with a selectable quality and container. It uses a Tkinter interface and `yt-dlp` for site support. Download only media you are authorized to save. The app does not bypass DRM, logins, paywalls, or other access restrictions.

## Requirements

- Python 3.10 or newer
- Tkinter, included with most Python installations (on some Linux distributions it must be installed separately)
- `yt-dlp` (required)
- FFmpeg (required; merges streams and resizes/compresses downloads to the selected resolution)

The app puts `yt-dlp` in the `.python-packages` folder beside the script. This keeps the dependency local to this app. Run installation commands from the `py downloader` folder.

## Setup

### Windows 10/11

1. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/). In the installer, enable **Add Python to PATH**.
2. Open PowerShell and change to the folder (adjust the Windows account name if needed):

   ```powershell
   cd "$HOME\Desktop\py downloader"
   ```

3. Install `yt-dlp` into the app's local dependency folder:

   ```powershell
   py -m pip install --upgrade --target .python-packages yt-dlp
   ```

4. Install FFmpeg from a trusted Windows build and add its `bin` folder to `PATH`. Reopen PowerShell after changing `PATH`. FFmpeg is required for the app's resize/compress step.

### macOS

1. Install Python 3.10 or newer. The installer from [python.org](https://www.python.org/downloads/) includes a Tk-capable Python; Homebrew Python may require a separate Tcl/Tk setup depending on the macOS and Homebrew version.
2. In Terminal:

   ```sh
   cd "$HOME/Desktop/py downloader"
   python3 -m pip install --upgrade --target .python-packages yt-dlp
   ```

3. Install FFmpeg with your chosen package manager (for Homebrew, `brew install ffmpeg`). FFmpeg is required for the app's resize/compress step.

### Linux (Debian, Ubuntu, Kali)

1. Install Python, Tkinter, pip, and FFmpeg with the system package manager:

   ```sh
   sudo apt update
   sudo apt install python3 python3-tk python3-pip ffmpeg
   ```

2. In a terminal, install `yt-dlp` locally for this app:

   ```sh
   cd "$HOME/Desktop/py downloader"
   python3 -m pip install --upgrade --target .python-packages yt-dlp
   ```

   If your distribution blocks pip installs into a system-managed Python, use a virtual environment for pip, then copy/install `yt-dlp` into `.python-packages` with that environment's pip, or install the distribution's `yt-dlp` package and ensure it is importable by the Python used to launch the app. Avoid forcing pip to modify system Python.

### Other Linux distributions

Install Python 3.10+, Tkinter, and FFmpeg through your distribution's package manager. FFmpeg is required for resizing/compression. For example, Fedora commonly provides them as `python3`, `python3-tkinter`, and `ffmpeg` (FFmpeg may require enabling RPM Fusion). Then install `yt-dlp` into `.python-packages` from this folder:

```sh
python3 -m pip install --upgrade --target .python-packages yt-dlp
```

## Launch

From the `py downloader` folder:

```sh
python3 luxv_tubers.py
```

On Windows, use:

```powershell
py luxv_tubers.py
```

You can also open the script with the installed Python interpreter. If the window does not appear on Linux, make sure a graphical desktop session is running and Tkinter is installed.

## Using the app

1. Paste a public HTTP or HTTPS video link into **Paste a video link** (or use **Paste link**), then click **Analyze**. The app displays available media details and formats.
2. Choose an output resolution from 240p to 4K, output container (`mp4`, `mkv`, or `webm`), and destination folder. The quality menu shows an estimated output size based on the selected resolution and video duration. Actual sizes vary with content and source resolution. Click **Browse** to choose the folder.
3. Click **Download Video**. Watch status and progress in the queue. Use **Cancel selected**, **Retry failed**, **Remove selected**, or **Clear finished** to manage jobs.
4. Use **Add URL batch** to enter one link per line. Each valid link is analyzed and queued.
5. **Settings** stores the default destination and performance preferences; **History** shows completed downloads. **Open Download Folder** opens the current destination.

The default destination is the current user's `Downloads` folder. Preferences, logs, and history are saved in the per-user application configuration directory:

- Windows: `%APPDATA%\LuxvTubers`
- macOS: `~/Library/Application Support/LuxvTubers`
- Linux: `$XDG_CONFIG_HOME/LuxvTubers` or `~/.config/LuxvTubers`

## Troubleshooting

- **“yt-dlp missing”**: Repeat the local installation command from this folder and confirm `.python-packages` exists beside `luxv_tubers.py`.
- **FFmpeg required / compression error**: Install FFmpeg and make sure `ffmpeg` is available on `PATH`, then restart the app.
- **Tk interface will not start**: Check that Python includes Tkinter (`python3 -m tkinter` on macOS/Linux; on Windows, rerun the Python installer and include Tcl/Tk support).
- **A link cannot be analyzed**: Check the network and link, update `yt-dlp` with the install command, and retry. Supported sites can change over time.
- **Permission or write error**: Choose a destination folder where your user account can create files.

Optional thumbnail support can be added locally with `python3 -m pip install --target .python-packages pillow` (Windows: `py -m pip ...`).

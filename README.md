# File Organizer

<p align="center">
  <img src="walrus_logo.png" alt="Walrus Dev logo" width="120">
</p>

A Windows desktop app that keeps a folder tidy. It sorts files into sub-folders by file type, can watch a folder and sort new files as they arrive, and lets you undo anything it does.

Built with Python and Tkinter by **Walrus Dev**.

## Screenshots

<p align="center">
  <img src="screenshots/main.png" alt="File Organizer main window with the rules list, options and action buttons" width="560">
</p>

<p align="center">
  <img src="screenshots/add-rule.png" alt="Add rule dialog with folder name, file types and quick fill" width="260">
</p>

## Features

- **Custom rules** - choose the folder name and exactly which file types go into it (for example `.zip`, `.rar`, `.7z` into `zip_files`). A file type can only belong to one folder, and the app tells you if a rule would clash.
- **Watch mode** - leave it running and new files are sorted automatically. Files that were just changed are left alone for a few seconds so nothing is moved while it is still being written.
- **Skips in-progress downloads** - `.crdownload`, `.part`, `.tmp` and similar files are never touched, so a half-finished download is not broken.
- **Preview** - see what would move before anything does.
- **Undo and full history** - undo the last run, or pick any of your last 30 runs and undo it.
- **Duplicate finder** - finds files with identical content (not just identical names). Extra copies go to a `_duplicates` folder instead of being deleted, and can be undone.
- **Import and export rules** - save your rules to a `.json` file and load them on another PC.
- **System tray** - closes to the tray and keeps working in the background.
- **Run at startup** - optionally starts with Windows, straight to the tray.
- **Safe by default** - never overwrites (a clashing name becomes `file (1).zip`), skips files that are in use, and asks once before reusing folders that already exist.

## Download

Go to the [Releases](../../releases) page and download `FileOrganizer.exe`. No install and no Python needed.

> **Windows SmartScreen:** because the app is not code-signed, Windows may show "Windows protected your PC". Click **More info**, then **Run anyway**. The full source code is in this repository if you would like to check it first.

## How to use

1. Click **Browse** and choose the folder you want to keep tidy (for example Downloads).
2. Check the **Rules** list. Add, edit or remove rules to suit you.
3. Click **Preview** to see what would happen, then **Organize**.
4. Optionally tick **Watch folder** to sort new files automatically.

If something goes wrong, click **Undo last run** or open **History**.

## Default folders

| Folder | File types |
|---|---|
| `image_files` | .png .jpg .jpeg .webp |
| `audio_files` | .mp3 .wav |
| `video_files` | .mp4 .mkv .mov .avi .webm |
| `document_files` | .pdf .doc .docx .odt .rtf |
| `spreadsheet_files` | .xlsx .xls .ods |
| `presentation_files` | .ppt .pptx .odp |
| `csv_files` | .csv |
| `text_files` | .txt |
| `code_files` | .py .js .java .html .css .cpp .json |
| `zip_files` | .zip .rar .7z |
| `executable_files` | .exe .msi |
| `disk_images` | .iso .img |
| `custom_skins` | .fantome |

Everything is editable in the app, and **Reset to default** brings these back.

## Where settings are stored

Your folder, rules and history are saved in `%APPDATA%\FileOrganizer\config.json`. Delete that file to start fresh.

## Run from source

Requires Python 3.8+ on Windows (Tkinter is included with the Windows installer).

```bash
git clone https://github.com/marcus2045/<repo-name>.git
cd <repo-name>
pip install pystray pillow   # optional, only needed for the tray icon
python file_organizer.py
```

The app works without `pystray` and `pillow`; the tray option is simply disabled.

## Build the EXE

```bash
pip install pyinstaller pystray pillow
pyinstaller --onefile --noconsole --icon walrus.ico ^
  --add-data "walrus_logo.png;." --add-data "walrus_icon.png;." ^
  --hidden-import pystray._win32 file_organizer.py
```

The finished file is in the `dist` folder. Install `pystray` and `pillow` **before** building, otherwise the tray feature is left out of the EXE.

## Tech

- Python, Tkinter / ttk (custom dark theme)
- `shutil`, `pathlib`, `hashlib` for moving, scanning and hashing files
- `threading` and `queue` to keep the interface responsive during scans and tray actions
- `winreg` for the Windows startup entry
- `pystray` and `Pillow` for the tray icon (optional)
- PyInstaller for the standalone EXE

## What I learned

- Designing safe file operations: collision handling, locked files, and a reversible history.
- Keeping a Tkinter interface responsive while background work runs.
- Validating user input, such as rejecting overlapping rules, and cleaning imported data.
- Packaging a Python app as a standalone Windows EXE.

## Ideas for the future

- Keyword rules (for example anything containing "invoice" goes to `invoices`)
- Sort into date folders such as `image_files/2026-10`
- Ignore list for files that should never be moved
- Watch several folders, each with its own rules

## Contributing

Issues and suggestions are welcome. Open an issue to report a bug or ask for a feature.

## License

Add a `LICENSE` file (the MIT License is a common choice for personal projects).

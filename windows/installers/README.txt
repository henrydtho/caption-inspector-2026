Offline installer cache for Caption Inspector Windows setup

Put these files in this folder to support offline bootstrap mode:

1) python-3.12.10-amd64.exe
   Source: https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe

2) msys2-x86_64-latest.exe
   Source: https://github.com/msys2/msys2-installer/releases/latest/download/msys2-x86_64-latest.exe

Run offline setup:

- Double-click windows/Launch-CaptionInspector-Windows.cmd and pass:
  -OfflineOnly

Example from a command prompt:

windows\Launch-CaptionInspector-Windows.cmd -OfflineOnly

For the Streamlit web app instead of the desktop app, use
windows/Launch-CaptionInspector-Streamlit-Windows.cmd (same -OfflineOnly support).

Important:
- OfflineOnly avoids winget and web downloads for Python/MSYS2 installers.
- The MSYS2 packages required to build Caption Inspector (clang, make, pkg-config, ffmpeg)
  must already be present in C:\msys64\ucrt64. If they are not installed, run setup once
  without -OfflineOnly on a machine with network access, or pre-stage the package repo/mirror.

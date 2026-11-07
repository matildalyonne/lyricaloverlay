"""Creates a Desktop shortcut (and optionally a Startup one) for the lyrics overlay,
so you can launch it without a terminal.

Run once:   python install_shortcuts.py
"""
import base64
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
target = here / "lyrics_overlay.py"
pythonw = Path(sys.executable).with_name("pythonw.exe")   # pythonw = no console window


def q(s):
    """Quote for a PowerShell single-quoted string."""
    return "'" + str(s).replace("'", "''") + "'"


def build_script(autostart):
    lines = [
        "$ws = New-Object -ComObject WScript.Shell",
        "function Make($dir, $name) {",
        "  $l = $ws.CreateShortcut((Join-Path $dir ($name + '.lnk')))",
        f"  $l.TargetPath = {q(pythonw)}",
        f"  $l.Arguments = {q(chr(34) + str(target) + chr(34))}",
        f"  $l.WorkingDirectory = {q(here)}",
        "  $l.Description = 'Spotify lyrics overlay'",
        "  $l.Save()",
        "}",
        "Make ([Environment]::GetFolderPath('Desktop')) 'Lyrics Overlay'",
    ]
    if autostart:
        lines.append("Make ([Environment]::GetFolderPath('Startup')) 'Lyrics Overlay'")
    return "\n".join(lines)


def main():
    if sys.platform != "win32":
        sys.exit("This only works on Windows.")
    if not pythonw.exists():
        sys.exit(f"Couldn't find {pythonw}")
    if not target.exists():
        sys.exit(f"Put this file in the same folder as {target.name}")
    ans = input("Also start the overlay automatically when you log in? [Y/n] ").strip().lower()
    script = build_script(autostart=ans != "n")
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc], check=True)
    print("Done. 'Lyrics Overlay' is on your Desktop" + (" and will start at login." if ans != "n" else "."))


if __name__ == "__main__":
    main()
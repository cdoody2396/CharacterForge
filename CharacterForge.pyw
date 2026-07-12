"""CharacterForge launcher stub (Stage 0).

The double-click target. Re-executes into the app's own virtual environment
under pythonw.exe — no console window, one app window (DECISIONS.md §2).
Stage 7 replaces this with the packaged launcher; the contract (one
double-click → one window) is identical.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PYW = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
CREATE_NO_WINDOW = 0x08000000


def _running_in_venv() -> bool:
    for candidate in (VENV_PYW, VENV_PY):
        try:
            if os.path.samefile(sys.executable, candidate):
                return True
        except OSError:
            continue
    return False


def main() -> None:
    if (
        not _running_in_venv()
        and os.path.exists(VENV_PYW)
        and not os.environ.get("CHARACTERFORGE_RELAUNCHED")
    ):
        env = dict(os.environ, CHARACTERFORGE_RELAUNCHED="1")
        subprocess.Popen(
            [VENV_PYW, os.path.abspath(__file__)],
            cwd=ROOT,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
        return

    sys.path.insert(0, ROOT)
    try:
        from app.main import run

        run()
    except Exception as exc:  # noqa: BLE001
        # Under pythonw there is no console, so an unhandled error would make
        # the app "do nothing" on double-click. Surface it in a dialog.
        _show_fatal(exc)
        raise


def _show_fatal(exc: BaseException) -> None:
    try:
        import ctypes
        import traceback

        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        ctypes.windll.user32.MessageBoxW(
            None,
            f"CharacterForge failed to start:\n\n{detail}",
            "CharacterForge",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()

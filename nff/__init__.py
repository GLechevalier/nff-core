"""nff — Claude Code IoT Bridge."""

__version__ = "0.2.38"


def run() -> None:
    """Console-script entry point."""
    # Windows consoles default to a legacy codepage (e.g. cp1252) that can't encode
    # the ✓/✗/… glyphs nff prints (doctor, init), which crashes with a
    # UnicodeEncodeError. Force UTF-8 so output never blows up.
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    from nff.cli import cli
    cli(standalone_mode=True)

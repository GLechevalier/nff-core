# Differential parity harness — CliRunner shim.
#
# PlatformIO's pytest suite drives the CLI in-process via Click's CliRunner:
#
#     result = clirunner.invoke(cmd_run, ["-d", project_dir])
#     assert result.exit_code == 0
#
# To run the *same* tests against an arbitrary binary (the Python `pio` for the
# baseline, then the Rust `pio-rs` for parity) we replace CliRunner with a
# subprocess driver keyed on the $PIO_BIN environment variable.
#
# The only non-trivial part is turning a Click command *object* (which is what
# tests import and pass to `.invoke`) into the argv path you'd type on the CLI.
# We discover that once by walking PlatformIO's real Click command tree from the
# root `pio` group, mapping each command object to its full path.
#
# This file is part of the differential harness, not of PlatformIO. PlatformIO
# (Apache-2.0) sources/tests are vendored under ../platformio-core/ with NOTICE.

import io
import os
import shlex
import subprocess
import sys


def _pio_bin():
    """The binary under test. Defaults to the Python `pio` on PATH."""
    raw = os.environ.get("PIO_BIN", "pio")
    # Allow "python -m platformio" style multi-word commands.
    return shlex.split(raw, posix=(os.name != "nt"))


def _build_command_path_index():
    """Map every Click command object -> its argv path under the root `pio`.

    Walks the live PlatformIO command tree. Used so `invoke(cmd_obj, args)` can
    reconstruct `[<path...>, *args]`. Falls back gracefully if PlatformIO can't
    be imported (e.g. when only the Rust binary is present) — in that case we
    key off each command's `.name`.
    """
    index = {}
    try:
        import click

        from platformio.__main__ import cli as root
    except Exception:  # pylint: disable=broad-except
        return index

    def walk(cmd, path, ctx):
        index[id(cmd)] = path
        # Duck-type the group interface (click.Group / MultiCommand) so this works
        # across Click versions without tripping the Click 9 MultiCommand warning.
        if hasattr(cmd, "list_commands") and hasattr(cmd, "get_command"):
            try:
                names = cmd.list_commands(ctx)
            except Exception:  # pylint: disable=broad-except
                names = []
            for name in names:
                try:
                    sub = cmd.get_command(ctx, name)
                except Exception:  # pylint: disable=broad-except
                    sub = None
                if sub is not None:
                    walk(sub, path + [name], ctx)

    ctx = click.Context(root, info_name="pio")
    walk(root, [], ctx)
    return index


_COMMAND_PATHS = _build_command_path_index()


class Result:
    """Mimics the subset of click.testing.Result the PlatformIO tests use."""

    def __init__(self, exit_code, output, exception=None):
        self.exit_code = exit_code
        self.output = output
        self.stdout = output
        self.exception = exception
        # Click exposes exc_info; tests occasionally touch it on failure paths.
        self.exc_info = (type(exception), exception, None) if exception else None

    def __repr__(self):
        return f"<Result exit_code={self.exit_code} output={self.output!r}>"


def _resolve_path(cmd):
    """argv path for a Click command object passed to invoke()."""
    if cmd is None:
        return []
    path = _COMMAND_PATHS.get(id(cmd))
    if path is not None:
        return list(path)
    # Fallbacks: the root command -> no prefix; otherwise use its name.
    name = getattr(cmd, "name", None)
    if name in (None, "pio", "platformio", "cli", "__main__"):
        return []
    return [name]


class CliRunner:
    """Drop-in for click.testing.CliRunner backed by a subprocess."""

    def __init__(self, *args, **kwargs):  # accept & ignore CliRunner's kwargs
        pass

    def invoke(self, cmd, args=None, input=None, env=None, catch_exceptions=True, **_kwargs):
        # pylint: disable=redefined-builtin
        argv = _pio_bin() + _resolve_path(cmd) + [str(a) for a in (args or [])]

        run_env = os.environ.copy()
        if env:
            for key, value in env.items():
                if value is None:
                    run_env.pop(key, None)
                else:
                    run_env[key] = str(value)
        # Keep output deterministic and ANSI-free for stable comparisons.
        run_env.setdefault("PLATFORMIO_NO_ANSI", "true")

        stdin_data = None
        if input is not None:
            stdin_data = input if isinstance(input, str) else input.decode("utf-8", "replace")

        try:
            proc = subprocess.run(
                argv,
                input=stdin_data,
                capture_output=True,
                text=True,
                env=run_env,
                check=False,
            )
        except FileNotFoundError as exc:
            if not catch_exceptions:
                raise
            return Result(2, f"{exc}\n", exception=exc)

        # Click's CliRunner merges stderr into output by default (mix_stderr).
        output = (proc.stdout or "") + (proc.stderr or "")
        exception = None
        if proc.returncode != 0:
            # validate_cliresult asserts `not result.exception`; only populate on
            # failures so success paths stay clean.
            exception = SystemExit(proc.returncode)
        return Result(proc.returncode, output, exception=exception)


def isolated_filesystem(*args, **kwargs):
    # Some tests use CliRunner().isolated_filesystem(); delegate to the real one.
    from click.testing import CliRunner as _ClickCliRunner

    return _ClickCliRunner().isolated_filesystem(*args, **kwargs)


if __name__ == "__main__":
    # Tiny self-check: print the discovered path for `pio run`.
    try:
        from platformio.run.cli import cli as cmd_run

        print("pio run ->", _resolve_path(cmd_run), file=sys.stderr)
    except Exception as exc:  # pylint: disable=broad-except
        print("platformio not importable:", exc, file=sys.stderr)
    io.StringIO()

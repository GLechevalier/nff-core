"""The self-contained virtual firmware store.

A mock device holds *fictional source code* (a pseudo-`.ino`) in Supabase, versioned. The
``compile`` step turns that source into a sketch the mock downloads over the normal OTA path
plus a fault declaration derived from the source's *content* — a bug in the code makes the
device crash-loop, and removing the bug (the agent's fix) cures it. ``ops`` is the store +
build + deploy surface (shared by the harness seed and the agent fix); ``repair`` builds the
prompt that points the agent at the read/fix tools.
"""

from nff_ota.source.compile import compile_sketch
from nff_ota.source.repair import build_crash_repair_prompt

__all__ = ["compile_sketch", "build_crash_repair_prompt"]

"""Durable file writes: after an atomic os.replace, fsync the directory too."""
import os
from pathlib import Path


def fsync_dir(directory: Path) -> None:
    """Make a rename in `directory` survive a power cut.

    Skipped where a directory cannot be opened (Windows): the write itself already
    succeeded, so this never raises.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

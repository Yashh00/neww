"""Detection of OneDrive "online-only" placeholder files without hydrating them.

Opening or reading a cloud-only placeholder makes OneDrive download it, which
needs network access. The processor must run offline, so placeholders are
detected from file-system metadata only and reported instead of being read.

Windows (OneDrive Files On-Demand / Cloud Files API):
  * FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (0x00400000): content is remote.
  * FILE_ATTRIBUTE_RECALL_ON_OPEN        (0x00040000): opening recalls the file.
  * FILE_ATTRIBUTE_OFFLINE               (0x00001000): data not immediately available.
  * FILE_ATTRIBUTE_PINNED (0x00080000) = "Always keep on this device".
  * FILE_ATTRIBUTE_UNPINNED (0x00100000) = "Free up space" requested.
macOS (File Provider): SF_DATALESS flag (0x40000000) in st_flags.
Other/fallback: a non-empty file with zero allocated blocks is treated as a
placeholder (configurable) because its content is not stored locally.
"""

from __future__ import annotations

import os
import stat as _stat
from dataclasses import dataclass

FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
SF_DATALESS = 0x40000000

_REMOTE_MASK = FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_OFFLINE


@dataclass
class PlaceholderInfo:
    is_placeholder: bool
    reason: str = ""
    pinned: bool = False
    unpinned: bool = False
    attributes: int = 0


def detect_placeholder(st: os.stat_result, zero_blocks_rule: bool = True) -> PlaceholderInfo:
    """Classify a stat result. Never opens the file."""
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    pinned = bool(attrs & FILE_ATTRIBUTE_PINNED)
    unpinned = bool(attrs & FILE_ATTRIBUTE_UNPINNED)
    if attrs & _REMOTE_MASK:
        flags = []
        if attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
            flags.append("RECALL_ON_DATA_ACCESS")
        if attrs & FILE_ATTRIBUTE_RECALL_ON_OPEN:
            flags.append("RECALL_ON_OPEN")
        if attrs & FILE_ATTRIBUTE_OFFLINE:
            flags.append("OFFLINE")
        return PlaceholderInfo(True, "Windows cloud-file attributes: " + "|".join(flags), pinned, unpinned, attrs)
    st_flags = int(getattr(st, "st_flags", 0) or 0)
    if st_flags & SF_DATALESS:
        return PlaceholderInfo(True, "macOS dataless (File Provider) file", pinned, unpinned, attrs)
    blocks = getattr(st, "st_blocks", None)
    if (zero_blocks_rule and blocks is not None and blocks == 0 and st.st_size > 0
            and _stat.S_ISREG(st.st_mode)):
        return PlaceholderInfo(True, "non-empty file with zero allocated blocks (content not stored locally)",
                               pinned, unpinned, attrs)
    return PlaceholderInfo(False, "", pinned, unpinned, attrs)


HYDRATE_HINT = ("In File Explorer right-click the file or folder and choose 'Always keep on this device', "
                "wait for OneDrive to finish syncing, then re-run 'inventory'.")

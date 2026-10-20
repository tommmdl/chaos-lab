#!/usr/bin/env python3
"""Compress PCAP evidence reproducibly while preserving partial-capture records."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import packets

result = packets.archive_report()
print(json.dumps(result, indent=2), flush=True)
if not result["complete"]:
    raise SystemExit(1)

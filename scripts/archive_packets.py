#!/usr/bin/env python3
"""Compress PCAP evidence reproducibly while preserving partial-capture records."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import packets

parser = argparse.ArgumentParser()
parser.add_argument("--report", type=Path)
parser.add_argument("--output", type=Path)
args = parser.parse_args()
result = packets.archive_report(args.report, args.output)
print(json.dumps(result, indent=2), flush=True)
if not result["complete"]:
    raise SystemExit(1)

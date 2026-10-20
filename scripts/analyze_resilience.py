#!/usr/bin/env python3
"""Reproduce resilience timeout observations from preserved, hash-checked PCAPs."""
import argparse, json
from pathlib import Path
import resilience_packets
parser=argparse.ArgumentParser()
parser.add_argument('report',type=Path)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
try:
    result=resilience_packets.analyze(json.loads(args.report.read_text()))
except Exception as error:
    result={'analysis_integrity_passed':False,'error':str(error)}
args.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='trials'},indent=2))
if not result['analysis_integrity_passed']:raise SystemExit(1)

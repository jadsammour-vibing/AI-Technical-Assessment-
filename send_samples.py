"""Simulate inbound traffic: POST each sample message to the intake webhook.

    python send_samples.py                         # the 5 assessment samples
    python send_samples.py data/extra_edge_cases.json
    python send_samples.py --url http://localhost:5678/webhook/arcvault-intake --delay 20   # n8n version
"""
import argparse
import json
import time
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("file", nargs="?", default="data/samples.json")
ap.add_argument("--url", default="http://localhost:8080/intake")
ap.add_argument("--delay", type=float, default=0, help="seconds between messages (free-tier rate limits)")
args = ap.parse_args()

for item in json.load(open(args.file, encoding="utf-8")):
    req = urllib.request.Request(args.url, data=json.dumps(item).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        print(item["id"], "->", r.status, r.read().decode()[:200])
    time.sleep(args.delay)

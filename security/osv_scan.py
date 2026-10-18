#!/usr/bin/env python3
"""Scan dependency lockfiles against OSV (https://osv.dev). Stdlib only.

Usage: python3 security/osv_scan.py [--date YYYY-MM-DD] [--out PATH] [LOCKFILE ...]
Without LOCKFILE arguments it scans every uv.lock, package-lock.json and
npm-shrinkwrap.json in the repo (git-tracked files plus untracked or ignored
local locks; node_modules is skipped). Writes the raw batch response plus
the advisory details to the output file and prints a summary.
"""
import argparse, datetime, json, os, re, subprocess, sys, urllib.request

API = "https://api.osv.dev/v1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_locks():
    names = ("uv.lock", "package-lock.json", "npm-shrinkwrap.json")
    def ls(*args):
        return subprocess.run(["git", "ls-files", *args], cwd=ROOT,
                              capture_output=True, text=True).stdout.split("\n")
    paths = ls() + ls("-o") + ls("-o", "-i", "--exclude-standard")
    return sorted({p for p in paths if os.path.basename(p) in names
                   and "node_modules" not in p})


def parse_uv(path):
    text = open(path, encoding="utf-8").read()
    pkgs = []
    for block in text.split("[[package]]")[1:]:
        n = re.search(r'^name = "([^"]+)"', block, re.M)
        v = re.search(r'^version = "([^"]+)"', block, re.M)
        src = re.search(r'^source = \{ (\w+)', block, re.M)
        if n and v and (not src or src.group(1) == "registry"):
            pkgs.append((n.group(1), v.group(1), "PyPI"))
    return pkgs


def parse_npm(path):
    data = json.load(open(path, encoding="utf-8"))
    pkgs = []
    for key, meta in data.get("packages", {}).items():
        if key and meta.get("version") and "node_modules/" in key:
            pkgs.append((key.rsplit("node_modules/", 1)[1], meta["version"], "npm"))
    return pkgs


def post(path, body):
    req = urllib.request.Request(API + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def get(path):
    return json.load(urllib.request.urlopen(API + path, timeout=60))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("locks", nargs="*")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--out")
    a = ap.parse_args()
    locks = a.locks or find_locks()
    entries = []  # (lock, name, version, eco)
    for lk in locks:
        p = lk if os.path.isabs(lk) else os.path.join(ROOT, lk)
        parser = parse_uv if lk.endswith(".lock") else parse_npm
        entries += [(lk,) + e for e in parser(p)]
    uniq = sorted({e[1:] for e in entries})
    results = []
    for i in range(0, len(uniq), 500):
        chunk = uniq[i:i + 500]
        q = [{"package": {"name": n, "ecosystem": e}, "version": v} for n, v, e in chunk]
        results += post("/querybatch", {"queries": q})["results"]
    hits = {}
    for (n, v, e), r in zip(uniq, results):
        for vuln in r.get("vulns", []):
            hits.setdefault(vuln["id"], []).append((n, v, e))
    details = {vid: get("/vulns/" + vid) for vid in hits}
    out = a.out or os.path.join(ROOT, "security", "osv", f"{a.date}-osv.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    doc = {"scan_date": a.date, "api": API + "/querybatch", "lockfiles": locks,
           "package_count": len(uniq),
           "packages": [{"name": n, "version": v, "ecosystem": e} for n, v, e in uniq],
           "raw_results": results,
           "hits": {k: [list(x) for x in v] for k, v in hits.items()},
           "advisories": details}
    json.dump(doc, open(out, "w"), indent=1, sort_keys=True)
    print(f"lockfiles={locks} packages={len(uniq)} hits={len(hits)} -> {out}")
    for vid, pk in hits.items():
        print(vid, pk)


if __name__ == "__main__":
    sys.exit(main())

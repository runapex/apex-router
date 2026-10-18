# Security scan: OSV and OpenVEX

Scan date: 2026-10-17. Commit scanned: `ce08fb94d641fa9461ab34852f1f0eb28ba42ce8`.

## Result

| Item | Value |
|---|---|
| Lockfiles | `uv.lock` (1 file) |
| Packages queried | 52 (PyPI) |
| OSV hits | 22 OSV ids = 11 distinct advisories (each GHSA also has a PYSEC id) |
| Affected | 4 (pypdf, optional `[books]` extra) |
| Not affected | 7 (4 pypdf, 3 urllib3) |

No `package-lock.json` or `yarn.lock` file exists. `integrations/pi/package.json` has no dependencies.

Note: `uv.lock` is listed in `.gitignore`, so git does not track it. The scan reads the local file.
Create it with `uv lock` if it is missing.

## Files

| Path | Content |
|---|---|
| `security/osv_scan.py` | Scanner. Python standard library only. |
| `security/osv/2026-10-17-osv.json` | Raw OSV batch response, package list and advisory details. |
| `security/vex/apex-router.openvex.json` | OpenVEX v0.2.0 triage, one statement per advisory. |

## How the scan was run

```
python3 security/osv_scan.py --date 2026-10-17
```

The script finds each lockfile, reads each pinned name and version, sends them to `POST https://api.osv.dev/v1/querybatch`,
then fetches each hit from `GET https://api.osv.dev/v1/vulns/<id>`.
It does not run any exploit and does not change any package.

## How to re-run

1. Run `python3 security/osv_scan.py` from any directory in the repo. The date defaults to today.
2. Read the new file `security/osv/<date>-osv.json`.
3. Compare the `hits` list with the statements in `security/vex/apex-router.openvex.json`.
4. Triage each new advisory against the code. Add a statement with `status` and `justification`.
5. Update `timestamp`, `version` and the product commit in the VEX file.

To scan a chosen file, pass it as an argument: `python3 security/osv_scan.py path/to/uv.lock`.
If the scan has zero hits, keep `statements` as an empty list and record the date, the lockfiles and the package count here.

## Triage summary

| Advisory | Package | Status | Reason |
|---|---|---|---|
| GHSA-5jq2-8x83-x246, GHSA-fp3h-c4fm-7vvf, GHSA-g9cg-prrw-2r8q, GHSA-jw7q-gvrg-4vj3 | pypdf 6.16.2 | affected | `scripts/booksearch.py:197,205` parses PDFs with `PdfReader` and `extract_text`. DoS only. Fix: pypdf 6.19.0 or later. |
| GHSA-qv6h-rv94-w285, GHSA-w23x-9jrw-r45c, GHSA-php9-fj8v-98fj, GHSA-v247-6f48-mgcj | pypdf 6.16.2 | not_affected | Page labels, form flatten and embedded files are never used. |
| GHSA-vxq7-64xx-v4gw, GHSA-gh4c-6fx4-qh6g | urllib3 2.7.0 | not_affected | Only reached through tiktoken download from a fixed host. |
| GHSA-8988-9cw3-xx77 | urllib3 2.7.0 | not_affected | The repo uses no HTTPS proxy TLS configuration. |

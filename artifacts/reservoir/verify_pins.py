"""Check that every pinned revision in `corpus-registry.json` still resolves upstream.

Metadata only — tree listings, never payload. Safe to run from anywhere, costs seconds.

WHY IT EXISTS. A pin is a claim about a remote repository, and this package's rule is that a claim
nothing recomputes is decoration. `build_registry.py` can only assert the sha is 40 hex characters;
whether it names a real commit, in a real repo, that still serves files, is a fact about Hugging
Face that only a request can settle.

It earned its place immediately: the first run rejected **seven of seventeen rows**. Those named
`common-pile/raw_v0.1_parquet` with the subset as a `config`, and the pinned tree showed that repo
holds `peS2o/`, `stackv2/`, `ubuntu_irc/` — the RAW subsets — while every `<name>_filtered` path
404s. The filtered variants are standalone repos shipping `.json.gz` at the root. Those rows had the
wrong repo, the wrong `file_format`, and a config that does not exist, so the build would have read
nothing at all.

Run: `python3 artifacts/reservoir/verify_pins.py [--deep]`

`--deep` additionally fetches the first 16 bytes of one real file per repo and checks the magic
number, which is the difference between "the commit exists" and "the commit serves the bytes we
think it does". One Range request per repo.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.error
import urllib.request

REGISTRY = pathlib.Path(__file__).with_name("corpus-registry.json")
HEADERS = {"User-Agent": "edullm-data/verify-pins"}

#: First bytes of a healthy file, per format. `.json.gz` is gzip (`1f 8b`); parquet starts `PAR1`.
#: Checking the magic rather than the status code is the recompute: a 200 only says something was
#: served, not that it was the format the reader will try to parse.
MAGIC = {"parquet": b"PAR1", "json.gz": b"\x1f\x8b", "jsonl.zst": b"\x28\xb5\x2f\xfd"}

#: Files that are never payload. Extension-based, and deliberately explicit rather than a
#: "descend into the first directory" heuristic: `finemath`'s tree starts with `assets/`, whose
#: first file is a PNG, so a naive descent reports a format mismatch on a perfectly healthy repo.
_NOT_PAYLOAD = (".md", ".gitattributes", ".csv", ".json", ".png", ".jpg", ".svg", ".txt", ".yaml")


def _get(url: str, timeout: int = 45, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={**HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _tree(repo: str, sha: str, path: str = "", limit: int = 20) -> list[dict]:
    return json.loads(
        _get(f"https://huggingface.co/api/datasets/{repo}/tree/{sha}/{path}?limit={limit}")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify-pins", description=__doc__)
    ap.add_argument("--deep", action="store_true", help="also fetch 16 bytes and check the magic")
    args = ap.parse_args(argv)

    doc = json.loads(REGISTRY.read_text())
    # One check per (repo, sha), not per row: the four FinePhrase configs share a repo, and
    # re-listing it four times would report one fact four times.
    pairs: dict[tuple[str, str], dict] = {}
    for row in doc["corpora"]:
        pairs.setdefault((row["repo"], row["revision"]), row)

    bad: list[str] = []
    for (repo, sha), row in sorted(pairs.items()):
        fmt = row["file_format"]
        try:
            entries = _tree(repo, sha)
        except urllib.error.HTTPError as exc:
            bad.append(f"{repo}@{sha[:10]}: tree HTTP {exc.code}")
            print(f"FAIL {repo:38} {sha[:10]} tree HTTP {exc.code}")
            continue
        note = f"{len(entries)} entries"

        if args.deep:
            want = MAGIC.get(fmt)
            # Start inside the row's own config/subdir when it has one. Descending blindly from the
            # root finds `assets/` in finemath and reports a PNG as a parquet failure — a false
            # alarm on a healthy repo, which is worse than no check.
            path, depth = (row.get("config") or ""), 0
            if path:
                try:
                    entries = _tree(repo, sha, path)
                except urllib.error.HTTPError:
                    # A config that is an HF config NAME rather than a directory. Not an error by
                    # itself — fall back to the root listing rather than reporting a broken pin.
                    path = ""
            found = None
            while depth < 4 and found is None:
                files = [e for e in entries
                         if e["type"] == "file" and not e["path"].endswith(_NOT_PAYLOAD)]
                if files:
                    found = files[0]
                    break
                dirs = [e for e in entries if e["type"] != "file"]
                if not dirs:
                    break
                path = dirs[0]["path"]
                entries = _tree(repo, sha, path)
                depth += 1
            if found is None:
                bad.append(f"{repo}@{sha[:10]}: no payload file within 4 levels")
                print(f"FAIL {repo:38} {sha[:10]} no payload file found")
                continue
            url = f"https://huggingface.co/datasets/{repo}/resolve/{sha}/{found['path']}"
            try:
                head = _get(url, timeout=60, headers={"Range": "bytes=0-15"})
            except urllib.error.HTTPError as exc:
                bad.append(f"{repo}@{sha[:10]}: resolve HTTP {exc.code}")
                print(f"FAIL {repo:38} {sha[:10]} resolve HTTP {exc.code}")
                continue
            if want and not head.startswith(want):
                bad.append(f"{repo}@{sha[:10]}: expected {fmt} magic {want!r}, got {head[:4]!r}")
                print(f"FAIL {repo:38} {sha[:10]} magic {head[:4]!r} != {want!r} ({fmt})")
                continue
            note = f"{found['path'].split('/')[-1]} magic ok ({fmt})"

        print(f"OK   {repo:38} {sha[:10]} {note}")

    print(f"\n{len(pairs) - len(bad)}/{len(pairs)} pinned (repo, sha) pairs resolve"
          f"{' with correct magic' if args.deep else ''}")
    if bad:
        print("\nFAILURES — a pin that does not resolve is a build that cannot run:")
        for b in bad:
            print(f"  {b}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

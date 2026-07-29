"""wu-fsck — Gate B, the post-publish decay sweep (§7). Owner: Eric Wu.

Every failure in the audit happened *after* publication — a source bucket deleted, a parent
dataset removed, a catalog drifted from reality, an ECR image expired. No publish-time gate
can catch those, because at publish time they were fine. So this runs nightly over the whole
published catalog and re-checks the references that rot.

It reads only metadata — LIST and HEAD, never a payload byte — so it costs cents per run
regardless of corpus size. It emits a report; it does not delete or quarantine (a false
alarm must never destroy data). Findings are for a human, and the job needs a named owner or
it gets muted after its first noisy night and becomes decoration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .manifest import ManifestEntry
from .s3 import S3, NotFound

DATA_BUCKET = "edullm-data"


@dataclass
class Finding:
    """One decayed reference. ``severity`` is coarse: ``error`` = a dataset is now
    unreadable or unreproducible; ``warn`` = drift that should be looked at but isn't
    breaking. ``dataset`` locates it; ``detail`` is the actionable specifics."""

    code: str
    severity: str  # "error" | "warn"
    dataset: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code} {self.dataset}: {self.detail}"


@dataclass
class FsckReport:
    checked: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "edullm-fsck/v1",
            "owner": "eric.wu",
            "checked": self.checked,
            "ok": self.ok,
            "findings": [
                {"code": f.code, "severity": f.severity, "dataset": f.dataset, "detail": f.detail}
                for f in self.findings
            ],
        }


def _load_json(s3: S3, bucket: str, key: str) -> Any:
    return json.loads(s3.get(bucket, key).decode("utf-8"))


def _catalog_datasets(s3: S3, data_bucket: str) -> list[tuple[str, str]]:
    """(dataset_id, version) for every catalog entry. dataset_id may contain '/', so parse
    from the right: _catalog/<family>/<name>/<version>.json."""
    out: list[tuple[str, str]] = []
    for obj in s3.list(data_bucket, "_catalog/"):
        key = obj["key"]
        if not key.endswith(".json"):
            continue
        rel = key[len("_catalog/") :]
        parts = rel[:-5].rsplit("/", 1)  # strip .json, split off version
        if len(parts) != 2:
            continue
        dataset_id, version = parts
        out.append((dataset_id, version))
    return out


# --------------------------------------------------------------------------------------
# per-dataset checks (each reads only metadata)
# --------------------------------------------------------------------------------------


def _check_objects_present(s3, data_bucket, dataset_id, version, ds, findings) -> tuple[int, int]:
    """Every manifest object still exists at its declared size. Catches an object deleted or
    truncated after publish. Returns (recomputed_objects, recomputed_bytes) for the catalog
    cross-check."""
    prefix = f"{dataset_id}/{version}"
    total_obj = 0
    total_bytes = 0
    for group in ds.get("groups", []):
        gname = group.get("name", "?")
        try:
            man = _load_json(s3, data_bucket, f"{prefix}/{group.get('manifest', f'{gname}/manifest.json')}")
        except (NotFound, ValueError):
            findings.append(Finding("manifest-gone", "error", prefix, f"group {gname!r} manifest missing or unreadable"))
            continue
        for e in man.get("entries", []):
            total_obj += 1
            total_bytes += int(e.get("bytes", 0))
            try:
                head = s3.head(data_bucket, f"{prefix}/{e['path']}")
            except NotFound:
                findings.append(Finding("object-gone", "error", prefix, f"{e['path']} no longer exists"))
                continue
            if head["size"] != e.get("bytes"):
                findings.append(Finding("object-resized", "error", prefix, f"{e['path']} size {head['size']} != manifest {e.get('bytes')}"))
    return total_obj, total_bytes


def _check_catalog_matches(s3, data_bucket, dataset_id, version, ds, recomputed, findings) -> None:
    """The catalog's object/byte counts equal reality — the inventory.json failure, checked
    continuously rather than only at publish."""
    inv = ds.get("inventory", {})
    r_obj, r_bytes = recomputed
    if inv.get("objects") != r_obj:
        findings.append(Finding("catalog-object-drift", "warn", f"{dataset_id}/{version}", f"inventory.objects={inv.get('objects')} but {r_obj} present"))
    if inv.get("bytes") != r_bytes:
        findings.append(Finding("catalog-byte-drift", "warn", f"{dataset_id}/{version}", f"inventory.bytes={inv.get('bytes')} but {r_bytes} present"))


def _check_depends_on(s3, data_bucket, dataset_id, version, ds, findings) -> None:
    """Every depends_on parent still exists and its manifest hash still matches what the
    child pinned. Catches a parent deleted or republished out from under a view."""
    prefix = f"{dataset_id}/{version}"
    for group in ds.get("groups", []):
        for dep in group.get("depends_on", []) or []:
            pid, pver = dep.get("dataset_id"), dep.get("version")
            pprefix = f"{pid}/{pver}"
            try:
                _load_json(s3, data_bucket, f"{pprefix}/dataset.json")
            except (NotFound, ValueError):
                findings.append(Finding("dangling-parent", "error", prefix, f"depends_on {pprefix} no longer exists"))
                continue
            pinned = dep.get("manifest_sha256")
            if pinned:
                # cheap: compare against the parent group's declared hash (no re-hash of bytes)
                try:
                    pds = _load_json(s3, data_bucket, f"{pprefix}/dataset.json")
                    live = {pg.get("manifest_sha256") for pg in pds.get("groups", [])}
                    if pinned not in live:
                        findings.append(Finding("parent-changed", "error", prefix, f"depends_on {pprefix} manifest_sha256 pin no longer matches parent"))
                except (NotFound, ValueError):
                    pass


def _check_image_digest(s3, ecr_client, dataset_id, version, ds, findings) -> None:
    """An aws-batch build's image digest still resolves in ECR. ECR lifecycle policies expire
    untagged images silently, which kills reproducibility without any other symptom. Skipped
    when no ecr_client is supplied (metadata-only test runs)."""
    if ecr_client is None:
        return
    ex = ds.get("build", {}).get("executor", {})
    if ex.get("kind") != "aws-batch":
        return
    digest = ex.get("image_digest")
    if not digest:
        return
    # image is <repo>@sha256:... — we only have the digest; a real impl would carry the repo.
    repo = ex.get("image_repo")
    if not repo:
        findings.append(Finding("image-repo-unknown", "warn", f"{dataset_id}/{version}", "aws-batch build records no image_repo; cannot verify digest still in ECR"))
        return
    try:
        ecr_client.describe_images(repositoryName=repo, imageIds=[{"imageDigest": digest}])
    except Exception:  # noqa: BLE001
        findings.append(Finding("image-digest-gone", "error", f"{dataset_id}/{version}", f"image {repo}@{digest} no longer in ECR — build not reproducible"))


CHECKS: list[Callable] = [_check_objects_present, _check_catalog_matches, _check_depends_on]


def fsck(s3: S3, *, data_bucket: str = DATA_BUCKET, ecr_client: Any = None) -> FsckReport:
    """Sweep the published catalog. Metadata only."""
    report = FsckReport()
    for dataset_id, version in _catalog_datasets(s3, data_bucket):
        report.checked += 1
        prefix = f"{dataset_id}/{version}"
        try:
            ds = _load_json(s3, data_bucket, f"{prefix}/dataset.json")
        except (NotFound, ValueError):
            report.findings.append(Finding("dataset-json-gone", "error", prefix, "catalog entry exists but dataset.json is missing/unreadable"))
            continue
        recomputed = _check_objects_present(s3, data_bucket, dataset_id, version, ds, report.findings)
        _check_catalog_matches(s3, data_bucket, dataset_id, version, ds, recomputed, report.findings)
        _check_depends_on(s3, data_bucket, dataset_id, version, ds, report.findings)
        _check_image_digest(s3, ecr_client, dataset_id, version, ds, report.findings)
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="wu-fsck", description="Gate B post-publish decay sweep")
    ap.add_argument("--data-bucket", default=DATA_BUCKET)
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = ap.parse_args(argv)

    from .s3 import Boto3S3

    s3 = Boto3S3.default()
    try:
        import boto3

        ecr = boto3.client("ecr", region_name="us-east-1")
    except Exception:  # noqa: BLE001
        ecr = None

    report = fsck(s3, data_bucket=args.data_bucket, ecr_client=ecr)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"wu-fsck: checked {report.checked} datasets, {len(report.findings)} findings, ok={report.ok}")
        for f in report.findings:
            print(f"  {f}")
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["fsck", "FsckReport", "Finding", "main"]

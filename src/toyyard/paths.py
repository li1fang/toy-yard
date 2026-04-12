from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from toyyard.constants import DEFAULT_ROOT, DEFAULT_RULE_FILES, DIRECTORY_LAYOUT
from toyyard.util import slugify


@dataclass(frozen=True)
class ProjectPaths:
    root: Path = DEFAULT_ROOT

    @property
    def db_path(self) -> Path:
        return self.root / "_db" / "toyyard.sqlite"

    @property
    def rules_dir(self) -> Path:
        return self.root / "_rules"

    @property
    def manifests_dir(self) -> Path:
        return self.root / "04_registry" / "manifests"

    @property
    def canonical_dir(self) -> Path:
        return self.root / "04_registry" / "canonical"

    @property
    def intake_pkg_dir(self) -> Path:
        return self.root / "01_intake" / "pkg"

    @property
    def aiue_pmx_publish_dir(self) -> Path:
        return self.root / "05_publish" / "aiue_pmx"

    @property
    def aiue_roundtrip_dir(self) -> Path:
        return self.canonical_dir / "aiue_roundtrip"

    @property
    def motion_inspect_root(self) -> Path:
        return self.root / "03_workbench" / "inspect" / "motion"

    def ensure_layout(self) -> None:
        for rel in DIRECTORY_LAYOUT:
            (self.root / rel).mkdir(parents=True, exist_ok=True)
        for name, content in DEFAULT_RULE_FILES.items():
            path = self.rules_dir / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")

    def managed_package_dir(self, sha256: str, display_name: str) -> Path:
        return self.intake_pkg_dir / f"{sha256}__{slugify(display_name)}"

    def package_manifest_path(self, package_id: int) -> Path:
        return self.manifests_dir / f"package_{package_id}.json"

    def asset_manifest_path(self, asset_id: int) -> Path:
        return self.manifests_dir / f"asset_{asset_id}.json"

    def canonical_manifest_path(self, kind: str, canonical_id: str) -> Path:
        return self.canonical_dir / f"{kind}_{slugify(canonical_id)}.json"

    def aiue_pmx_profile_dir(self, profile: str) -> Path:
        return self.aiue_pmx_publish_dir / slugify(profile)

    def aiue_pmx_conversion_dir(self, profile: str) -> Path:
        return self.aiue_pmx_profile_dir(profile) / "conversion"

    def aiue_pmx_summary_dir(self, profile: str) -> Path:
        return self.aiue_pmx_profile_dir(profile) / "summary"

    def aiue_pmx_workspace_views_dir(self, profile: str) -> Path:
        return self.aiue_pmx_profile_dir(profile) / "workspace_views"

    def aiue_pmx_package_dir(self, profile: str, package_id: str) -> Path:
        return self.aiue_pmx_conversion_dir(profile) / package_id

    def aiue_pmx_package_manifest_path(self, profile: str, package_id: str) -> Path:
        return self.aiue_pmx_package_dir(profile, package_id) / "manifest.json"

    def aiue_pmx_package_consumer_contract_path(self, profile: str, package_id: str) -> Path:
        return self.aiue_pmx_package_dir(profile, package_id) / "ue_consumer_contract.json"

    def aiue_pmx_summary_path(self, profile: str) -> Path:
        return self.aiue_pmx_summary_dir(profile) / "ue_suite_summary.json"

    def aiue_pmx_registry_path(self, profile: str) -> Path:
        return self.aiue_pmx_summary_dir(profile) / "ue_equipment_registry.json"

    def aiue_pmx_manifest_artifact_check_path(self, profile: str) -> Path:
        return self.aiue_pmx_summary_dir(profile) / "manifest_artifact_check.json"

    def aiue_pmx_workspace_view_path(self, profile: str) -> Path:
        return self.aiue_pmx_workspace_views_dir(profile) / "pmx_pipeline_workspace_view.json"

    def aiue_pmx_trial_workspace_path(self, profile: str) -> Path:
        return self.aiue_pmx_workspace_views_dir(profile) / "pipeline_workspace.toy-yard.example.json"

    def aiue_roundtrip_sample_dir(self, sample_id: str) -> Path:
        return self.aiue_roundtrip_dir / sample_id

    def aiue_roundtrip_sample_artifact_dir(self, sample_id: str) -> Path:
        return self.aiue_roundtrip_sample_dir(sample_id) / "sample"

    def aiue_roundtrip_package_dir(self, sample_id: str, package_id: str) -> Path:
        return self.aiue_roundtrip_sample_dir(sample_id) / package_id

    def motion_inspect_dir(self, package_id: int, display_name: str) -> Path:
        return self.motion_inspect_root / f"pkg_{package_id}__{slugify(display_name)}"

    def triage_marker_path(self, bucket: str, package_id: int) -> Path:
        return self.root / "02_triage" / bucket / f"package_{package_id}.json"

    def reject_marker_path(self, bucket: str, package_id: int) -> Path:
        return self.root / "90_reject" / bucket / f"package_{package_id}.json"

    def cleanup_triage_markers(self, package_id: int) -> None:
        for bucket in ("pending", "review_needed", "blocked_by_dependency"):
            path = self.triage_marker_path(bucket, package_id)
            if path.exists():
                path.unlink()

    def cleanup_reject_markers(self, package_id: int) -> None:
        for bucket in ("classify_failed", "extract_failed", "test_failed", "publish_failed"):
            path = self.reject_marker_path(bucket, package_id)
            if path.exists():
                path.unlink()

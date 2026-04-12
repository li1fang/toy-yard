from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("TOYYARD_ROOT", r"C:\Projects\toy-yard"))
DEFAULT_VORTEX_SKYRIMSE = Path(
    os.environ.get(
        "TOYYARD_VORTEX_SKYRIMSE",
        r"C:\Users\garro\AppData\Roaming\Vortex\downloads\skyrimse",
    )
)

SOURCE_KINDS = (
    "manual_drop",
    "vortex",
    "taobao",
    "mozhiwu",
    "ai_generated",
    "unknown",
)
ROOT_KINDS = (
    "canonical_warehouse",
    "legacy_workspace",
    "external_source",
    "unknown",
)
ASSET_KINDS = (
    "character",
    "outfit_set",
    "weapon",
    "accessory",
    "texture_pack",
    "animation",
    "mixed_pack",
    "unknown",
)
DEP_KINDS = ("asset", "runtime", "tool", "skeleton", "body", "physics")
REQUIREMENT_LEVELS = ("required", "optional", "unknown")
COMPAT_STATUSES = ("native", "compatible", "requires_adapter", "incompatible", "untested")
READINESS_VALUES = ("unknown", "blocked", "component_only", "candidate", "ue_ready", "rejected")
WAREHOUSE_STATUSES = ("observed", "cataloged", "classified", "ready_for_pipeline", "blocked", "published", "rejected")
STAGE_STATES = ("source", "preflight", "conversion", "verification", "session", "export")

DIRECTORY_LAYOUT = (
    "00_inbox/manual_drop",
    "00_inbox/vortex",
    "00_inbox/taobao",
    "00_inbox/mozhiwu",
    "00_inbox/ai_generated",
    "01_intake/pkg",
    "02_triage/pending",
    "02_triage/review_needed",
    "02_triage/blocked_by_dependency",
    "03_workbench/unpack",
    "03_workbench/inspect",
    "03_workbench/extract",
    "03_workbench/convert",
    "03_workbench/test",
    "04_registry/raw",
    "04_registry/unpacked",
    "04_registry/normalized",
    "04_registry/previews",
    "04_registry/manifests",
    "04_registry/canonical",
    "05_publish/ue_ready",
    "05_publish/blender_ready",
    "05_publish/source_exports",
    "05_publish/aiue_pmx",
    "90_reject/classify_failed",
    "90_reject/extract_failed",
    "90_reject/test_failed",
    "90_reject/publish_failed",
    "_db",
    "_rules",
    "_reports",
    "_fixtures",
)

DEFAULT_RULE_FILES = {
    "generic.toml": """\
[signals]
mesh_extensions = [".nif", ".fbx", ".obj", ".gltf", ".glb", ".mesh", ".msh"]
texture_extensions = [".dds", ".png", ".jpg", ".jpeg", ".tga", ".bmp"]
plugin_extensions = [".esp", ".esm", ".esl", ".ini", ".json", ".xml"]
animation_extensions = [".hkx", ".anim", ".vmd", ".vpd", ".motion"]
archive_extensions = [".7z", ".zip", ".rar"]
body_keywords = ["3ba", "cbbe", "bhunp", "unp", "uunp"]
skeleton_keywords = ["xpmsse", "skeleton"]
physics_keywords = ["smp", "fsmp", "hdt", "cloth"]
character_keywords = ["follower", "standalone", "npc", "player", "character"]
outfit_keywords = ["outfit", "armor", "armour", "dress", "suit", "swimsuit", "clothes", "costume"]
weapon_keywords = ["sword", "blade", "gun", "weapon", "bow", "staff", "spear", "katana"]
accessory_keywords = ["hat", "cap", "ring", "necklace", "glove", "gloves", "heel", "heels", "shoe", "shoes", "earring"]
mixed_pack_threshold = 3
""",
    "skyrimse.toml": """\
[signals]
game_extensions = [".nif", ".dds", ".esp", ".esm", ".hkx"]
bodyslide_markers = ["calientetools/bodyslide"]
body_keywords = ["3ba", "cbbe", "bhunp", "unp", "uunp"]
skeleton_keywords = ["xpmsse"]
physics_keywords = ["smp", "fsmp", "hdt"]
plugin_extensions = [".esp", ".esm", ".esl"]
""",
}

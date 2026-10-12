#!/usr/bin/env python3
"""
Build the merged LiteLLM config.yaml for a given region.

Replicates the `yq` merge logic in the upstream deploy.sh:
  combined model_list = base.model_list + region.model_list
  all other top-level keys (router_settings, litellm_settings, ...) come from base.

Output: cdk/assets/config.yaml  (deployed to the config S3 bucket by DataStack)

Usage:
  python build_config.py            # uses CONFIG.region from config.py
  python build_config.py ap-southeast-2
"""

import sys
from pathlib import Path

import yaml

from config import CONFIG

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def build(region: str) -> Path:
    base_path = CONFIG_DIR / "default-config-base.yaml"
    region_path = CONFIG_DIR / f"default-config-{region}.yaml"

    if not base_path.exists():
        raise SystemExit(f"Base config not found: {base_path}")
    if not region_path.exists():
        raise SystemExit(
            f"Region config not found: {region_path}\n"
            f"Region {region} is not supported by this fork."
        )

    with base_path.open() as f:
        base = yaml.safe_load(f) or {}
    with region_path.open() as f:
        region_cfg = yaml.safe_load(f) or {}

    base_models = base.get("model_list", []) or []
    region_models = region_cfg.get("model_list", []) or []

    # Merge: base model_list + region model_list (deploy.sh semantics).
    merged = dict(base)
    merged["model_list"] = base_models + region_models

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ASSETS_DIR / "config.yaml"
    with out_path.open("w") as f:
        yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False)

    print(
        f"Wrote {out_path} "
        f"({len(base_models)} base + {len(region_models)} {region} "
        f"= {len(merged['model_list'])} models)"
    )
    return out_path


if __name__ == "__main__":
    region = sys.argv[1] if len(sys.argv) > 1 else CONFIG.region
    build(region)

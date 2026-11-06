"""Shared constants/helpers for the non-private baseline streaming system."""
import json
import os
import re

MANIFEST_NAME = "manifest.json"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def chunk_path(catalog_dir, video_id, idx):
    """On-disk location of one fixed-size chunk record."""
    return os.path.join(catalog_dir, video_id, f"{idx:06d}.chunk")


def load_manifest(catalog_dir):
    with open(os.path.join(catalog_dir, MANIFEST_NAME)) as f:
        return json.load(f)

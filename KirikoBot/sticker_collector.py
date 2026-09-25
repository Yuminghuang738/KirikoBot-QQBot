from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from PIL import Image
import imagehash

logger = logging.getLogger(__name__)

import os as _os
_DOCKER_DIR = "/app/stickers"
_LOCAL_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "stickers")
try:
    _os.makedirs(_DOCKER_DIR, exist_ok=True)
    STICKER_DIR = _DOCKER_DIR
except (PermissionError, OSError):
    _os.makedirs(_LOCAL_DIR, exist_ok=True)
    STICKER_DIR = _LOCAL_DIR

STICKER_CATEGORIES = {"可爱", "搞笑", "生气", "惊讶", "悲伤", "打招呼", "鼓励", "庆祝", "动物", "动漫", "其他", "未分类"}

# Perceptual hash threshold — images with hamming distance <= this are duplicates
PHASH_THRESHOLD = 8


class StickerCollector:
    """表情库索引：读取已有表情、维护 MD5/pHash 索引、找出重复项。

    这里原本还有一条「监听群消息、自动把群友发的图收进表情库」的路径
    （`collect` 及其下载/复制/自动分类辅助）。官方平台收不到非 @ 的群
    消息，这条路径不可能再触发，已整体删除；表情库的读取与 pHash 索引
    保留，供相似表情和重复清理使用。
    """

    def __init__(self, db: Any = None) -> None:
        os.makedirs(STICKER_DIR, exist_ok=True)
        self._hashes: set[str] | None = None  # lazy init (MD5)
        self._phashes: dict[str, str] | None = None  # lazy init (pHash hex → filename)
        self._db = db

    def _build_index(self) -> set[str]:
        """Scan all existing stickers and compute MD5 hashes."""
        hashes: set[str] = set()
        if not os.path.isdir(STICKER_DIR):
            return hashes
        for fname in os.listdir(STICKER_DIR):
            fpath = os.path.join(STICKER_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                h = self._md5_file(fpath)
                if h:
                    hashes.add(h)
            except Exception:
                logger.debug("sticker_collector._build_index 忽略了异常", exc_info=True)
        logger.debug("Sticker index built: %d hashes", len(hashes))
        return hashes

    def _build_phash_index(self) -> dict[str, str]:
        """Scan all existing stickers and compute perceptual hashes.
        Returns dict of {phash_hex: filename} for the first occurrence of each hash."""
        phashes: dict[str, str] = {}
        if not os.path.isdir(STICKER_DIR):
            return phashes
        for fname in sorted(os.listdir(STICKER_DIR)):
            fpath = os.path.join(STICKER_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                ph = self._phash_file(fpath)
                if ph and ph not in phashes:
                    phashes[ph] = fname
            except Exception:
                logger.debug("sticker_collector._build_phash_index 忽略了异常", exc_info=True)
        logger.debug("pHash index built: %d unique hashes", len(phashes))
        return phashes

    @property
    def hashes(self) -> set[str]:
        if self._hashes is None:
            self._hashes = self._build_index()
        return self._hashes

    @property
    def phashes(self) -> dict[str, str]:
        if self._phashes is None:
            self._phashes = self._build_phash_index()
        return self._phashes

    @staticmethod
    def _md5_file(filepath: str) -> str | None:
        try:
            h = hashlib.md5()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    @staticmethod
    def _phash_file(filepath: str) -> str | None:
        """Compute perceptual hash (pHash) for an image file.
        Returns hex string of the hash, or None on failure."""
        try:
            img = Image.open(filepath)
            # Convert to RGB if necessary (e.g., RGBA, P mode)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            ph = imagehash.phash(img)
            return str(ph)
        except Exception:
            logger.debug("pHash computation failed for %s", os.path.basename(filepath))
            return None

    @staticmethod
    def _phash_data(data: bytes) -> str | None:
        """Compute perceptual hash from raw image bytes."""
        import io
        try:
            img = Image.open(io.BytesIO(data))
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            ph = imagehash.phash(img)
            return str(ph)
        except Exception:
            return None

    def find_duplicates(self) -> list[list[dict[str, Any]]]:
        """Scan all stickers and group visually similar ones.
        Returns list of groups, each group containing 2+ similar stickers.
        Each sticker dict has: filename, file_size, phash."""
        all_files = []
        if not os.path.isdir(STICKER_DIR):
            return []

        for fname in os.listdir(STICKER_DIR):
            fpath = os.path.join(STICKER_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                ph = self._phash_file(fpath)
                size = os.path.getsize(fpath)
                all_files.append({
                    "filename": fname,
                    "filepath": fpath,
                    "file_size": size,
                    "phash": ph,
                })
            except Exception:
                logger.debug("sticker_collector.find_duplicates 忽略了异常", exc_info=True)

        if not all_files:
            return []

        # Group by perceptual hash proximity using Union-Find
        parent = {f["filename"]: f["filename"] for f in all_files}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x, y):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        # Compare all pairs (O(n²), but N is typically < 1000)
        for i in range(len(all_files)):
            phi = all_files[i]["phash"]
            if not phi:
                continue
            try:
                hi = imagehash.hex_to_hash(phi)
            except Exception:
                logger.debug("sticker_collector.find_duplicates 忽略了异常", exc_info=True)
                continue
            for j in range(i + 1, len(all_files)):
                phj = all_files[j]["phash"]
                if not phj:
                    continue
                try:
                    hj = imagehash.hex_to_hash(phj)
                except Exception:
                    logger.debug("sticker_collector.find_duplicates 忽略了异常", exc_info=True)
                    continue
                if hi - hj <= PHASH_THRESHOLD:
                    union(all_files[i]["filename"], all_files[j]["filename"])

        # Collect groups
        groups_map: dict[str, list[dict[str, Any]]] = {}
        for f in all_files:
            root = find(f["filename"])
            if root not in groups_map:
                groups_map[root] = []
            groups_map[root].append(f)

        # Return groups with 2+ members
        result = [g for g in groups_map.values() if len(g) >= 2]
        result.sort(key=lambda g: -len(g))  # largest groups first

        logger.info("Duplicate scan: %d groups found (%d total stickers)",
                     len(result), len(all_files))
        return result

    def cleanup_duplicates(self, dry_run: bool = True) -> dict[str, Any]:
        """Find and remove duplicate stickers, keeping the best quality one.

        For each group of visually similar stickers, keeps the largest file
        (best quality) and removes rest.

        Returns: {dry_run: bool, groups_cleaned: int, files_removed: int,
                  removed: [filenames], kept: [filenames]}
        """
        groups = self.find_duplicates()
        removed: list[str] = []
        kept: list[str] = []

        for group in groups:
            # Sort by file_size descending — keep the largest
            group.sort(key=lambda x: x["file_size"], reverse=True)
            best = group[0]
            kept.append(best["filename"])

            for dup in group[1:]:
                removed.append(dup["filename"])
                if not dry_run:
                    try:
                        os.remove(dup["filepath"])
                        # Remove from DB if present
                        if self._db:
                            self._db.execute_action(
                                "DELETE FROM stickers WHERE filename = ?",
                                (dup["filename"],),
                            )
                        # Invalidate caches
                        md5 = self._md5_file(dup["filepath"]) if os.path.exists(dup["filepath"]) else None
                        if md5 and md5 in self.hashes:
                            self.hashes.discard(md5)
                        if dup["phash"] and dup["phash"] in self.phashes:
                            del self.phashes[dup["phash"]]
                        logger.info("Removed duplicate: %s (kept %s)", dup["filename"], best["filename"])
                    except Exception:
                        logger.exception("Failed to remove duplicate %s", dup["filename"])

        result = {
            "dry_run": dry_run,
            "groups_cleaned": len(groups),
            "files_removed": len(removed),
            "files_kept": len(kept),
            "kept": kept,
            "removed": removed,
            "total_waste_bytes": sum(
                sum(d["file_size"] for d in group[1:]) for group in groups
            ) if groups else 0,
        }

        logger.info(
            "Cleanup %s: %d groups, %d files removed, %d kept, %d bytes wasted",
            "DRY RUN" if dry_run else "EXECUTED",
            result["groups_cleaned"],
            result["files_removed"],
            result["files_kept"],
            result["total_waste_bytes"],
        )
        return result


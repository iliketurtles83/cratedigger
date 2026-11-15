"""Typed settings schema and layout presets backed by SQLite."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sqlite3
from typing import Any

import config
from cratedigger.db import utc_now_iso

SETTINGS_KEY = "cratedigger_settings_v1"


@dataclass
class Settings:
    """Unified settings across the 5 configuration groups."""

    # 1. Paths
    music_root: str = str(config.MUSIC_ROOT)
    incoming_folder: str = str(config.INCOMING_FOLDER)
    quarantine_folder: str = str(config.MUSIC_ROOT / "0quarantine")
    playlists_folder: str = str(config.MUSIC_ROOT / "Playlists")
    server_sync_dest: str = ""

    # 2. Layout Preset
    preset_name: str = "player_optimal_tier1"
    letter_bucketing: bool = bool(getattr(config, "LETTER_BUCKETING", True))
    article_strip: list[str] = field(
        default_factory=lambda: list(getattr(config, "ARTICLE_STRIP", ["The", "A", "An"]))
    )
    symbol_bucket: str = str(getattr(config, "SYMBOL_BUCKET", "#"))
    album_template: str = "{bucket}/{artist}/{artist} - {album} ({year})/{track_prefix}{artist} - {title}{ext}"
    compilation_template: str = "0compilations/{album} ({year})/{track_prefix}{artist} - {title}{ext}"
    single_template: str = "{bucket}/{artist}/0singles/{artist} - {title}{ext}"
    exempt_mix_folders: list[str] = field(default_factory=lambda: ["0mixes", "0shacks"])
    dissolve_soundtracks: bool = True
    redistribute_random: bool = True

    # 3. Small Releases & Curation
    small_release_threshold: int = 3
    long_track_minutes: int = 15
    auto_update_singles_playlists: bool = True
    auto_update_intake_playlist: bool = True
    intake_playlist_prefix: str = "Intake_"

    # 4. Audio & Artwork
    transcode_extensions: list[str] = field(
        default_factory=lambda: [".wma", ".mpc", ".ogg", ".opus"]
    )
    transcode_bitrate: str = "320k"
    cover_max_dim: int = 1200
    embed_single_covers: bool = True

    # 5. Genre Taxonomy
    genre_remap_rules: dict[str, str] = field(
        default_factory=lambda: dict(getattr(config, "GENRE_REMAP_RULES", {}))
    )
    effnet_auto_threshold: float = 0.35
    effnet_review_threshold: float = 0.18
    consensus_auto_threshold: float = 0.75

    def music_root_path(self) -> Path:
        return Path(self.music_root)

    def quarantine_path(self) -> Path:
        return Path(self.quarantine_folder)

    def playlists_path(self) -> Path:
        return Path(self.playlists_folder)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        valid = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in valid}
        return cls(**filtered)


PRESETS: dict[str, dict[str, Any]] = {
    "player_optimal_tier1": {
        "preset_name": "player_optimal_tier1",
        "letter_bucketing": True,
        "article_strip": ["The", "A", "An"],
        "small_release_threshold": 3,
        "dissolve_soundtracks": True,
        "redistribute_random": True,
        "embed_single_covers": True,
        "album_template": "{bucket}/{artist}/{artist} - {album} ({year})/{track_prefix}{artist} - {title}{ext}",
        "compilation_template": "0compilations/{album} ({year})/{track_prefix}{artist} - {title}{ext}",
        "single_template": "{bucket}/{artist}/0singles/{artist} - {title}{ext}",
    },
    "strict_albums_only": {
        "preset_name": "strict_albums_only",
        "letter_bucketing": True,
        "small_release_threshold": 0,
        "dissolve_soundtracks": True,
        "redistribute_random": True,
    },
    "flat_artist_root": {
        "preset_name": "flat_artist_root",
        "letter_bucketing": False,
        "album_template": "{artist}/{artist} - {album} ({year})/{track_prefix}{artist} - {title}{ext}",
        "single_template": "{artist}/0singles/{artist} - {title}{ext}",
    },
}


def default_settings(music_root: Path | None = None) -> Settings:
    """Build default Settings rooted at *music_root* (or config.MUSIC_ROOT)."""
    root = music_root or config.MUSIC_ROOT
    return Settings(
        music_root=str(root),
        incoming_folder=str(root / "incoming"),
        quarantine_folder=str(root / "0quarantine"),
        playlists_folder=str(root / "Playlists"),
    )


def apply_preset(settings: Settings, preset_name: str) -> Settings:
    """Return a copy of *settings* updated with named preset overrides."""
    if preset_name not in PRESETS:
        raise ValueError(f"Unknown preset '{preset_name}'. Available: {sorted(PRESETS)}")
    data = settings.to_dict()
    data.update(PRESETS[preset_name])
    return Settings.from_dict(data)


def load_settings(conn: sqlite3.Connection, *, music_root: Path | None = None) -> Settings:
    """Load persisted Settings from SQLite, merged onto defaults for *music_root*."""
    base = default_settings(music_root=music_root)
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    if row is None:
        return base
    stored = json.loads(row["value_json"])
    merged = base.to_dict()
    merged.update(stored)
    if music_root is not None:
        merged["music_root"] = str(music_root)
        if "quarantine_folder" not in stored:
            merged["quarantine_folder"] = str(music_root / "0quarantine")
        if "playlists_folder" not in stored:
            merged["playlists_folder"] = str(music_root / "Playlists")
        if "incoming_folder" not in stored:
            merged["incoming_folder"] = str(music_root / "incoming")
    return Settings.from_dict(merged)


def save_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    """Persist *settings* to SQLite settings table."""
    conn.execute(
        """
        INSERT INTO settings (key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at
        """,
        (SETTINGS_KEY, json.dumps(settings.to_dict(), indent=2), utc_now_iso()),
    )
    conn.commit()

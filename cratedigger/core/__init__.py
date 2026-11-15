"""Core music-tag, parser, hash, genre, and artist identity helpers."""

from lib.audiohash import audio_hash
from lib.context import artist_letter_bucket, classify_folder, is_disc_subfolder, is_letter_bucket
from lib.genres import is_meaningful_genre, normalise_genre, parent_genre
from lib.parsers import (
    artist_comparison_key,
    build_filename,
    is_suspicious_tag,
    normalise_title,
    parse_filename,
    parse_folder_name,
    sanitise_component,
    sanitise_name,
    title_case_clean,
    validate_albumartist,
    validate_year,
)
from lib.tags import embed_artwork, has_embedded_artwork, read_tags, write_tags

__all__ = [
    "artist_comparison_key",
    "artist_letter_bucket",
    "audio_hash",
    "build_filename",
    "classify_folder",
    "embed_artwork",
    "has_embedded_artwork",
    "is_disc_subfolder",
    "is_letter_bucket",
    "is_meaningful_genre",
    "is_suspicious_tag",
    "normalise_genre",
    "normalise_title",
    "parent_genre",
    "parse_filename",
    "parse_folder_name",
    "read_tags",
    "sanitise_component",
    "sanitise_name",
    "title_case_clean",
    "validate_albumartist",
    "validate_year",
    "write_tags",
]

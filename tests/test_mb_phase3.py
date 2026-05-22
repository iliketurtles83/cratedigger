import types
import unittest
from pathlib import Path
from unittest.mock import patch

import lib.mb as mb


class _WebServiceError(Exception):
    pass


class _NetworkError(Exception):
    pass


class MbPhase3Tests(unittest.TestCase):
    def setUp(self) -> None:
        mb._last_mb_call = 0.0
        mb._adaptive_mb_delay = 0.0

    def test_mb_call_with_retry_uses_backoff(self) -> None:
        sleep_calls: list[float] = []

        def _sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        def _failing_call(*args, **kwargs):
            raise _WebServiceError("throttled")

        fake_client = types.SimpleNamespace(
            WebServiceError=_WebServiceError,
            NetworkError=_NetworkError,
        )

        with (
            patch.object(mb, "_HAS_MB", True),
            patch.object(mb, "musicbrainzngs", fake_client),
            patch.object(mb, "MB_MAX_RETRIES", 2),
            patch.object(mb, "MB_BACKOFF_BASE", 2.0),
            patch.object(mb, "MB_MAX_QUEUE_SECONDS", 60.0),
            patch("lib.mb.time.sleep", side_effect=_sleep),
        ):
            result = mb._mb_call_with_retry(_failing_call, rate_limit_seconds=0.0)

        self.assertIsNone(result)
        # Retries sleep with exponential backoff: 2^0, 2^1.
        self.assertEqual(sleep_calls, [1.0, 2.0])

    def test_mb_call_with_retry_respects_bounded_queue(self) -> None:
        fake_client = types.SimpleNamespace(
            WebServiceError=_WebServiceError,
            NetworkError=_NetworkError,
        )

        with (
            patch.object(mb, "_HAS_MB", True),
            patch.object(mb, "musicbrainzngs", fake_client),
            patch.object(mb, "MB_MAX_QUEUE_SECONDS", 0.1),
            patch.object(mb, "_adaptive_mb_delay", 5.0),
            patch.object(mb, "_last_mb_call", 100.0),
            patch("lib.mb.time.monotonic", return_value=100.0),
        ):
            result = mb._mb_call_with_retry(lambda: {"ok": True}, rate_limit_seconds=0.0)

        self.assertIsNone(result)

    def test_mb_recording_search_uses_tag_votes_as_tiebreaker(self) -> None:
        def _search_recordings(*args, **kwargs):
            return {
                "recording-list": [
                    {
                        "id": "rec_a",
                        "title": "Track",
                        "artist-credit": [{"artist": {"name": "Artist"}}],
                    },
                    {
                        "id": "rec_b",
                        "title": "Track",
                        "artist-credit": [{"artist": {"name": "Artist"}}],
                    },
                ]
            }

        def _get_recording_by_id(recording_id: str, includes=None):
            if recording_id == "rec_a":
                return {
                    "recording": {
                        "tag-list": [
                            {"name": "rock", "count": 5},
                        ]
                    }
                }
            return {
                "recording": {
                    "tag-list": [
                        {"name": "electronic", "count": 2},
                        {"name": "ambient", "count": 4},
                    ]
                }
            }

        fake_client = types.SimpleNamespace(
            WebServiceError=_WebServiceError,
            NetworkError=_NetworkError,
            search_recordings=_search_recordings,
            get_recording_by_id=_get_recording_by_id,
        )

        with (
            patch.object(mb, "_HAS_MB", True),
            patch.object(mb, "musicbrainzngs", fake_client),
            patch.object(mb, "MB_MAX_QUEUE_SECONDS", 60.0),
        ):
            result = mb.mb_recording_search(
                "Artist",
                "Track",
                genre_hints=["Electronic"],
                rate_limit_seconds=0.0,
            )

        self.assertEqual(result["recording_id"], "rec_b")

    def test_fingerprint_lookup_logs_below_threshold_and_returns_empty(self) -> None:
        fake_acoustid = types.SimpleNamespace(
            WebServiceError=_WebServiceError,
            match=lambda *args, **kwargs: {
                "status": "ok",
                "results": [
                    {
                        "score": 0.72,
                        "recordings": [
                            {
                                "id": "r1",
                                "title": "Title",
                                "artists": [{"name": "Artist"}],
                            }
                        ],
                    }
                ],
            },
        )

        with (
            patch.object(mb, "_HAS_ACOUSTID", True),
            patch.object(mb, "ACOUSTID_API_KEY", "dummy"),
            patch.object(mb, "acoustid", fake_acoustid),
            patch.object(mb, "MB_MAX_QUEUE_SECONDS", 60.0),
        ):
            result = mb.fingerprint_lookup(Path("song.mp3"), min_score=0.8, rate_limit_seconds=0.0)

        self.assertEqual(
            result,
            {
                "recording_id": None,
                "title": None,
                "artist": None,
            },
        )


if __name__ == "__main__":
    unittest.main()

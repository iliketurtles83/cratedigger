import unittest

from lib.tags import (
    _m4a_pair_to_text,
    _normalise_bpm,
    _normalise_number_pair,
    _prepare_written_value,
    _text_to_m4a_pair,
)


class TagNormalisationTests(unittest.TestCase):
    def test_normalise_number_pair_strips_padding_and_spaces(self) -> None:
        self.assertEqual(_normalise_number_pair(" 01 / 010 "), "1/10")

    def test_normalise_number_pair_rejects_zero_number(self) -> None:
        self.assertIsNone(_normalise_number_pair("0/12"))

    def test_normalise_number_pair_drops_zero_total(self) -> None:
        self.assertEqual(_normalise_number_pair("03/00"), "3")

    def test_normalise_bpm_rounds_positive_numeric_values(self) -> None:
        self.assertEqual(_normalise_bpm("127.6"), "128")

    def test_normalise_bpm_rejects_invalid_or_non_positive_values(self) -> None:
        self.assertIsNone(_normalise_bpm("abc"))
        self.assertIsNone(_normalise_bpm("0"))

    def test_text_to_m4a_pair_preserves_total(self) -> None:
        self.assertEqual(_text_to_m4a_pair("02/10"), (2, 10))

    def test_m4a_pair_to_text_preserves_total(self) -> None:
        self.assertEqual(_m4a_pair_to_text([(2, 10)]), "2/10")

    def test_prepare_written_value_normalises_track_disc_and_bpm(self) -> None:
        self.assertEqual(_prepare_written_value("track", "04/09"), "4/9")
        self.assertEqual(_prepare_written_value("disc", "02"), "2")
        self.assertEqual(_prepare_written_value("bpm", "099.7"), "100")


if __name__ == "__main__":
    unittest.main()

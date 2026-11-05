import unittest

from lib.genres import (
    CHILD_TO_PARENT,
    ROOT_GENRES,
    is_meaningful_genre,
    merge_genres,
    normalise_genre,
    parent_genre,
)


class TestGenres(unittest.TestCase):
    def test_canonical_casing(self):
        self.assertEqual(normalise_genre("rock"), "Rock")
        self.assertEqual(normalise_genre("ELECTRONIC"), "Electronic")
        self.assertEqual(normalise_genre("ambient"), "Ambient")
        self.assertEqual(normalise_genre("hard rock"), "Hard Rock")
        self.assertEqual(normalise_genre("post-punk"), "Post-Punk")

    def test_chill_and_estonian_roots(self):
        self.assertIn("Chill", ROOT_GENRES)
        self.assertIn("Estonian", ROOT_GENRES)
        self.assertEqual(normalise_genre("chill"), "Chill")
        self.assertEqual(normalise_genre("chillout"), "Chill")
        self.assertEqual(normalise_genre("estonian"), "Estonian")

    def test_aliases(self):
        self.assertEqual(normalise_genre("hip hop"), "Hip-Hop")
        self.assertEqual(normalise_genre("dnb"), "Drum & Bass")
        self.assertEqual(normalise_genre("synthpop"), "Synth-Pop")
        self.assertEqual(normalise_genre("ost"), "Soundtrack")
        self.assertEqual(normalise_genre("rocknroll"), "Rock & Roll")
        self.assertEqual(normalise_genre("rock and roll"), "Rock & Roll")

    def test_parent_lookup(self):
        self.assertEqual(parent_genre("Hard Rock"), "Rock")
        self.assertEqual(parent_genre("Ambient"), "Chill")
        self.assertEqual(parent_genre("Estonian Folk"), "Estonian")

    def test_blacklist(self):
        self.assertFalse(is_meaningful_genre("other"))
        self.assertFalse(is_meaningful_genre("unknown"))
        self.assertFalse(is_meaningful_genre("Audio"))
        self.assertFalse(is_meaningful_genre("music"))
        self.assertFalse(is_meaningful_genre("default"))
        self.assertFalse(is_meaningful_genre("misc"))
        self.assertFalse(is_meaningful_genre("(none)"))
        self.assertTrue(is_meaningful_genre("Rock"))
        self.assertTrue(is_meaningful_genre("Chill"))

    def test_merge_genres(self):
        merged = merge_genres("Chill", ["Ambient", "other", "CHILL", "Downtempo"])
        self.assertEqual(merged, "Chill / Ambient / Downtempo")

    def test_2tier_reconcile(self):
        from scripts.reconcile_genres import reconcile_genre_string

        g1, r1 = reconcile_genre_string("Ambient")
        self.assertEqual(g1, "Chill / Ambient")

        g2, r2 = reconcile_genre_string("Downtempo")
        self.assertEqual(g2, "Chill / Downtempo")

        g3, r3 = reconcile_genre_string("Synth-Pop")
        self.assertEqual(g3, "Electronic / Synth-Pop")

        g4, r4 = reconcile_genre_string("Hip-Hop / Rap")
        self.assertEqual(g4, "Hip-Hop")

        g5, r5 = reconcile_genre_string("70s / Rock / Hard Rock")
        self.assertEqual(g5, "Rock / Hard Rock")
        self.assertIn("70s", r5)

        g6, r6 = reconcile_genre_string("Other")
        self.assertIsNone(g6)
        self.assertIn("Other", r6)

        g7, r7 = reconcile_genre_string("Estonian / Rap")
        self.assertEqual(g7, "Estonian / Hip-Hop")


if __name__ == "__main__":
    unittest.main()

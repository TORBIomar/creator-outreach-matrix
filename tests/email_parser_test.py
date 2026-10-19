import unittest
from live_tiktok_scraper import extract_email, is_in_excluded_region, parse_follower_count


class TestScraperFilters(unittest.TestCase):
    def test_extract_clean_email(self):
        self.assertEqual(extract_email("Contact: creator@gmail.com"), "creator@gmail.com")

    def test_extract_obfuscated_email(self):
        self.assertEqual(extract_email("Collabs: creator (at) outlook (dot) com"), "creator@outlook.com")

    def test_reject_social_handle(self):
        self.assertIsNone(extract_email("Follow my insta@fashion"))

    def test_parse_follower_count(self):
        self.assertEqual(parse_follower_count("1.2K"), 1200)
        self.assertEqual(parse_follower_count("500"), 500)

    def test_excluded_region(self):
        self.assertTrue(is_in_excluded_region("Based in São Paulo 🇧🇷"))
        self.assertFalse(is_in_excluded_region("London, UK"))


if __name__ == "__main__":
    unittest.main()

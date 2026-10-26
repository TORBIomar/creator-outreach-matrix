"""Offline regression tests; no TikTok requests or real webhook deliveries."""
import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests

import live_tiktok_scraper_v2 as app


def record(username="sarah", email="pr-sarah@gmail.com"):
    return {"username": username, "email": email, "profile_url": f"https://www.tiktok.com/@{username}",
            "followers_count": 1200, "bio": "Beauty | London", "email_source": "bio",
            "source_query": "beauty", "found_at": app.utc_now()}


class ParsingTests(unittest.TestCase):
    def test_every_discovery_query_targets_small_accounts(self):
        terms = app.build_search_terms()
        self.assertEqual(len(terms), len(set(terms)))
        for term in terms:
            with self.subTest(term=term):
                self.assertRegex(term, r"small|nano|under ?[125]k|road ?to ?[15]k")
        for broad in ("#grwm", "#jewelrytok", "#beautytok", "everyday makeup routine",
                      "trying new beauty products grwm", "micro influencer pr packages"):
            self.assertNotIn(broad, terms)

    def test_emails_keep_valid_local_parts(self):
        self.assertEqual(app.extract_emails("PR: pr-sarah@gmail.com | collab_emily@outlook.com"),
                         ["pr-sarah@gmail.com", "collab_emily@outlook.com"])

    def test_obfuscated_and_uncommon_tld(self):
        self.assertEqual(app.extract_emails("anna (at) gmail (dot) com"), ["anna@gmail.com"])
        self.assertEqual(app.extract_emails("anna at gmail dot com"), ["anna@gmail.com"])
        self.assertEqual(app.extract_emails("hello@creator.photography"), ["hello@creator.photography"])

    def test_reject_assets_and_invalid_addresses(self):
        for value in ("a..b@gmail.com", "insta@gmail.com", "logo@2x.png", "test@example.com", "foo@localhost"):
            with self.subTest(value=value):
                self.assertEqual(app.extract_emails(value), [])

    def test_creator_handles_and_inquiries_are_allowed(self):
        for handle in ("sarah.grwm", "makeupbyanna", "emily.lifestyle", "olivia.diary"):
            self.assertIsNone(app.exclusion_reason(handle, "Beauty creator | inquiries: anna@gmail.com", "anna@gmail.com"))

    def test_explicit_business_and_ugc_still_excluded(self):
        for bio in ("DM to order", "Etsy boutique | worldwide shipping", "represented by Anna", "mgmt: talent@agency.com", "fan page", "UGC creator"):
            with self.subTest(bio=bio):
                self.assertIsNotNone(app.exclusion_reason("anna", bio))
        self.assertIsNone(app.exclusion_reason("anna", "UGC creator", include_ugc=True))

    def test_region_evidence(self):
        self.assertIsNone(app.excluded_region("Rio | London, UK"))
        self.assertIsNone(app.excluded_region("Beauty tips on https://creator.info"))
        self.assertIsNotNone(app.excluded_region("Based in Brazil"))
        self.assertIsNotNone(app.excluded_region("Rio de Janeiro"))
        self.assertIsNotNone(app.excluded_region("London", "anna@creator.in"))

    def test_urls_normalized_from_video_and_tracking_links(self):
        expected = "https://www.tiktok.com/@sarah"
        for value in ("/@Sarah", "https://www.tiktok.com/@Sarah?lang=en", "https://www.tiktok.com/@Sarah/video/123"):
            self.assertEqual(app.canonical_profile_url(value), expected)
        for value in ("https://evil.test/@sarah", "https://tiktok.com.evil.test/@sarah", "javascript:alert(1)"):
            self.assertIsNone(app.canonical_profile_url(value))

    def test_follower_counts(self):
        for value, expected in (("1.2K", 1200), ("1,2K", 1200), ("1,234", 1234), ("5 000", 5000),
                                (0, 0), (None, None), ("unknown", None), ("Likes 100", None), (float("nan"), None)):
            with self.subTest(value=value):
                self.assertEqual(app.parse_follower_count(value), expected)

    def test_html_email_provenance(self):
        parser = app.ContactHTML()
        parser.feed('''<script>"telemetry":"bad@analytics.com"; {"businessEmail":"contact@anna.com"}</script>
                       <a href="mailto:pr-anna@gmail.com">Email</a><p>work@anna.com</p>
                       <a href="/contact">Contact me</a>''')
        self.assertNotIn("bad@analytics.com", " ".join(parser.text))
        self.assertEqual(parser.structured_emails, ["contact@anna.com"])
        self.assertIn(("/contact", " Contact me"), parser.links)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "leads.sqlite3"
        self.store = app.Store(self.path)
        self.store.add_queries(["beauty", "jewelry"])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def candidate(self, username="sarah"):
        self.store.add_candidate(f"https://www.tiktok.com/@{username}", "beauty")

    def test_claims_are_distinct_and_queue_survives_restart(self):
        for number in range(65):
            self.candidate(f"creator{number}")
        first = self.store.claim_job()
        second = self.store.claim_job()
        self.assertNotEqual(first["username"], second["username"])
        self.store.close()
        self.store = app.Store(self.path)
        self.assertEqual(self.store.backlog(), 65)

    def test_pending_enrichment_survives_restart(self):
        self.candidate()
        self.store.finish_job("sarah", "enrichment_pending", delay=0, data={"bio_link": "https://anna.com"})
        self.store.claim_job(enrichment=True)
        self.store.close()
        self.store = app.Store(self.path)
        job = self.store.claim_job(enrichment=True)
        self.assertEqual(json.loads(job["data"])["bio_link"], "https://anna.com")

    def test_retry_dates_prevent_immediate_reprocessing(self):
        self.candidate()
        self.store.finish_job("sarah", "no_email", delay=86400)
        self.assertIsNone(self.store.claim_job())
        self.store.finish_job("sarah", "no_email", delay=-1)
        self.assertEqual(self.store.claim_job()["username"], "sarah")

    def test_lead_save_is_atomic_and_email_deduplicated(self):
        self.candidate()
        self.candidate("other")
        self.assertTrue(self.store.save_lead(record()))
        self.assertFalse(self.store.save_lead(record("other", "PR-SARAH@gmail.com")))
        self.assertEqual(self.store.summary()["profiles"], {"qualified": 1, "duplicate": 1})
        self.assertEqual(self.store.summary()["deliveries"], {"pending": 1})

    def test_outbox_failure_and_crash_recovery(self):
        self.store.save_lead(record())
        row = self.store.claim_delivery()
        self.assertIsNone(self.store.claim_delivery())
        self.store.delivery_result(row["id"], False, "HTTP 500", 60)
        self.assertIsNone(self.store.claim_delivery())
        with self.store.conn:
            self.store.conn.execute("UPDATE leads SET next_attempt=0")
        self.store.claim_delivery()
        self.store.close()
        self.store = app.Store(self.path)
        row = self.store.claim_delivery()
        self.assertEqual(row["attempts"], 2)
        self.store.delivery_result(row["id"], True)
        self.assertEqual(self.store.summary()["deliveries"], {"sent": 1})

    def test_dry_run_never_claims_delivery(self):
        self.store.save_lead(record(), dry_run=True)
        self.assertIsNone(self.store.claim_delivery())
        self.assertEqual(self.store.summary()["deliveries"], {"held": 1})

    def test_legacy_import_cancels_pending_duplicate_delivery(self):
        self.store.save_lead(record())
        self.store.import_external([record("legacy", "pr-sarah@gmail.com")])
        self.assertIsNone(self.store.claim_delivery())
        self.assertFalse(self.store.add_candidate("/@sarah", "beauty"))

    def test_legacy_partial_line_and_idempotent_tailing(self):
        path = Path(self.temp.name) / "old.jsonl"
        encoded = json.dumps(record())
        path.write_text(encoded[:20])
        importer = app.LegacyImporter(self.store, [path])
        importer.poll(force=True)
        self.assertEqual(self.store.summary()["deliveries"], {})
        with path.open("a") as handle:
            handle.write(encoded[20:] + "\n")
        importer.poll(force=True)
        importer.poll(force=True)
        self.assertEqual(self.store.summary()["deliveries"], {"external": 1})

    def test_legacy_file_rotation(self):
        path = Path(self.temp.name) / "old.jsonl"
        path.write_text(json.dumps(record()) + "\n")
        importer = app.LegacyImporter(self.store, [path])
        importer.poll(force=True)
        path.rename(path.with_suffix(".old"))
        path.write_text(json.dumps(record("new", "new@gmail.com")) + "\n")
        importer.poll(force=True)
        self.assertEqual(self.store.summary()["deliveries"], {"external": 2})

    def test_cache_and_export(self):
        result = {"emails": ["anna@gmail.com"], "links": [], "url": "https://anna.com"}
        self.store.cache_put("https://anna.com", result)
        self.assertEqual(self.store.cache_get("https://anna.com"), result)
        self.store.save_lead(record())
        self.store.import_external([record("legacy", "other@gmail.com")])
        output = Path(self.temp.name) / "backup.jsonl"
        self.store.export(output)
        self.assertEqual(len(output.read_text().splitlines()), 1)

    def test_visible_totals_separate_legacy_from_new_qualified_leads(self):
        self.store.import_external([record("legacy", "old@gmail.com")])
        self.store.save_lead(record())
        totals = app.progress_text(self.store)
        self.assertIn("QUALIFIED: 1 | N8N ACCEPTED: 0 | QUEUED: 1", totals)
        self.assertIn("LEGACY IMPORTED: 1", totals)

    def test_search_reward_and_cooldown(self):
        self.store.search_finished("beauty", 5, 10)
        self.assertEqual(self.store.choose_query(), "jewelry")
        self.store.search_finished("jewelry", 2, 10)
        self.assertIsNone(self.store.choose_query())

    def test_search_prefers_nano_fit_over_volume_and_lead_totals(self):
        with self.store.conn:
            self.store.conn.execute("""UPDATE queries SET runs=5,discovered=1000,checked=100,
                leads=10,range_checked=100,in_range=10 WHERE term='beauty'""")
            self.store.conn.execute("""UPDATE queries SET runs=5,discovered=10,checked=10,
                leads=1,range_checked=10,in_range=8 WHERE term='jewelry'""")
        for _ in range(10):
            self.assertEqual(self.store.choose_query(), "jewelry")

    def test_search_still_explores_untried_targeted_queries(self):
        self.store.configure_discovery(["small creator grwm", "nano jewelry creator"], 10, 5000)
        with self.store.conn:
            self.store.conn.execute("""UPDATE queries SET runs=1,range_checked=10,in_range=10
                WHERE term='small creator grwm'""")
        with patch.object(app.random, "random", return_value=0):
            self.assertEqual(self.store.choose_query(), "nano jewelry creator")

    def test_nano_query_upgrade_preserves_queue_leads_and_search_history(self):
        self.candidate()
        self.store.finish_job("sarah", "no_email", delay=86400, data={"followers_count": 1200})
        self.store.add_candidate("/@bigger", "beauty")
        self.store.finish_job("bigger", "out_of_range", delay=86400, data={"followers_count": 100000})
        self.store.save_lead(record("other", "other@gmail.com"), dry_run=True)
        self.store.search_finished("beauty", 2, 10)
        self.store.configure_discovery(["nano jewelry creator"], 10, 5000)
        self.assertEqual(self.store.choose_query(), "nano jewelry creator")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)
        self.assertEqual(self.store.summary()["deliveries"], {"held": 1})
        old = self.store.conn.execute("SELECT * FROM queries WHERE term='beauty'").fetchone()
        self.assertEqual((old["active"], old["runs"], old["range_checked"], old["in_range"]), (0, 1, 2, 1))
        self.store.close()
        self.store = app.Store(self.path)
        self.assertEqual(self.store.choose_query(), "nano jewelry creator")

    def test_query_range_counters_survive_restart_and_reseed_for_new_limits(self):
        self.store.configure_discovery(["beauty"], 10, 5000)
        for count in (10, 5000, 5001, 0, None):
            self.store.profile_finished("beauty", 1, count)
        self.store.close()
        self.store = app.Store(self.path)
        self.store.configure_discovery(["beauty"], 10, 5000)
        row = self.store.conn.execute("SELECT * FROM queries WHERE term='beauty'").fetchone()
        self.assertEqual((row["checked"], row["range_checked"], row["in_range"]), (5, 4, 2))
        self.candidate()
        self.store.finish_job("sarah", "no_email", delay=86400, data={"followers_count": 1200})
        self.store.configure_discovery(["beauty"], 10, 1000)
        row = self.store.conn.execute("SELECT * FROM queries WHERE term='beauty'").fetchone()
        self.assertEqual((row["checked"], row["range_checked"], row["in_range"]), (5, 1, 0))

    def test_old_database_gains_search_columns_without_losing_jobs(self):
        self.candidate()
        self.store.close()
        conn = app.sqlite3.connect(self.path)
        conn.executescript("""
            DROP TABLE queries;
            CREATE TABLE queries (
                term TEXT PRIMARY KEY, last_run REAL DEFAULT 0, runs INTEGER DEFAULT 0,
                discovered INTEGER DEFAULT 0, checked INTEGER DEFAULT 0, leads INTEGER DEFAULT 0,
                seconds REAL DEFAULT 0, errors INTEGER DEFAULT 0
            );
            INSERT INTO queries(term,runs,leads) VALUES ('beauty',3,1);
        """)
        conn.close()
        self.store = app.Store(self.path)
        row = self.store.conn.execute("SELECT * FROM queries WHERE term='beauty'").fetchone()
        self.assertEqual((row["runs"], row["leads"], row["active"], row["range_checked"], row["in_range"]),
                         (3, 1, 1, 0, 0))
        self.assertEqual(self.store.claim_job()["username"], "sarah")

    def test_rollback_does_not_mark_creator_seen(self):
        self.candidate()
        with self.store.conn:
            self.store.conn.execute("""CREATE TRIGGER fail_lead BEFORE INSERT ON leads
                BEGIN SELECT RAISE(ABORT, 'simulated disk/write error'); END""")
        with self.assertRaises(app.sqlite3.IntegrityError):
            self.store.save_lead(record())
        self.assertEqual(self.store.claim_job()["username"], "sarah")

    def test_state_lock_rejects_second_v2_process(self):
        with app.state_lock(Path(self.temp.name)):
            with self.assertRaises(RuntimeError):
                with app.state_lock(Path(self.temp.name)):
                    pass


class HTTPTests(unittest.TestCase):
    def response(self, status=200, chunks=(), headers=None):
        response = Mock()
        response.status_code = status
        response.headers = headers or {"Content-Type": "text/html"}
        response.encoding = "utf-8"
        response.iter_content.return_value = iter(chunks)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_webhook_failure_is_not_success_and_retry_key_is_stable(self):
        session = Mock()
        response = self.response(500)
        session.post.return_value = response
        row = {"username": "sarah", "email_key": "pr-sarah@gmail.com", "payload": json.dumps(record())}
        with patch.object(app, "http_session", return_value=session):
            self.assertEqual(app.post_webhook("http://localhost/webhook", row)[:2], (False, "HTTP 500"))
            first_key = session.post.call_args.kwargs["headers"]["Idempotency-Key"]
            response.status_code = 200
            self.assertTrue(app.post_webhook("http://localhost/webhook", row)[0])
            self.assertEqual(first_key, session.post.call_args.kwargs["headers"]["Idempotency-Key"])
            self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_contact_reads_multiple_chunks_and_closes_response(self):
        session = Mock()
        response = self.response(chunks=[b"<html><p>No email here</p>", b'<a href="mailto:pr-anna@gmail.com">Email</a></html>'])
        session.get.return_value = response
        with patch.object(app, "http_session", return_value=session), patch.object(app, "validate_public_url"):
            result = app.fetch_public_page("https://anna.com")
        self.assertEqual(result["emails"], ["pr-anna@gmail.com"])
        response.__exit__.assert_called_once()

    def test_redirect_destinations_are_checked(self):
        session = Mock()
        session.get.return_value = self.response(302, headers={"Location": "http://127.0.0.1/private"})
        with patch.object(app, "http_session", return_value=session), patch.object(
            app, "validate_public_url", side_effect=[None, ValueError("private destination")]
        ) as validate:
            result = app.fetch_public_page("https://anna.com")
        self.assertEqual(validate.call_count, 2)
        self.assertIn("unsupported", result)
        self.assertEqual(session.get.call_count, 1)

    def test_private_network_blocked_without_http(self):
        addresses = [(2, 1, 6, "", ("127.0.0.1", 80))]
        with patch.object(app.socket, "getaddrinfo", return_value=addresses):
            with self.assertRaises(ValueError):
                app.validate_public_url("http://private.test")


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = app.Store(Path(self.temp.name) / "db.sqlite3")
        self.store.add_queries(["beauty"])
        self.control = app.Control(0.03)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_default_browser_reuses_one_tab_and_closes_restored_tabs(self):
        pages = [Mock(), Mock(), Mock()]
        for page in pages:
            page.close = AsyncMock()
        context = Mock(pages=pages, new_page=AsyncMock())
        args = app.arguments([])
        self.assertFalse(args.multi_tab)
        main, profiles = await app.prepare_browser_pages(context, args)
        self.assertIs(main, pages[0])
        self.assertEqual(profiles, [])
        context.new_page.assert_not_awaited()
        pages[0].close.assert_not_awaited()
        for page in pages[1:]:
            page.close.assert_awaited_once()

    async def test_multi_tab_is_explicit_and_honors_profile_workers(self):
        main = Mock()
        context = Mock(pages=[main], new_page=AsyncMock())
        args = app.arguments(["--multi-tab", "--profile-workers", "2"])
        search, profiles = await app.prepare_browser_pages(context, args)
        self.assertIs(search, main)
        self.assertEqual(len(profiles), 2)
        self.assertEqual(context.new_page.await_count, 2)

    async def test_single_tab_drains_queue_then_searches_and_checks_new_profile(self):
        self.store.add_candidate("/@existing", "beauty")
        args = app.arguments(["--dry-run"])
        pipeline = app.Pipeline(self.store, self.control, args, app.LegacyImporter(self.store, []))
        page = Mock()
        visited = []

        async def navigate(tab, url, _):
            self.assertIs(tab, page)
            tab.url = url

        async def extract(tab, username):
            self.assertIs(tab, page)
            visited.append(username)
            if username == "discovered":
                self.control.stop.set()
            return {"bio": "Beauty creator London", "followers_count": 1000, "bio_link": ""}

        async def discover(tab, store, control, options, once=False):
            self.assertIs(tab, page)
            self.assertTrue(once)
            self.assertEqual(visited, ["existing"])
            self.assertEqual(store.summary()["profiles"], {"no_email": 1})
            store.add_candidate("/@discovered", "beauty")

        with patch.object(app, "navigate", side_effect=navigate), patch.object(
            app, "extract_profile", side_effect=extract
        ), patch.object(app, "discovery_worker", side_effect=discover) as discovery:
            await asyncio.wait_for(app.single_tab_worker(page, pipeline, self.store, self.control, args), 1)
        self.assertEqual(visited, ["existing", "discovered"])
        self.assertEqual(self.store.summary()["profiles"], {"no_email": 2})
        discovery.assert_awaited_once()

    async def test_single_tab_stays_paused_until_captcha_is_cleared(self):
        self.control.ready.clear()
        pipeline = Mock(profile_worker=AsyncMock())
        task = asyncio.create_task(app.single_tab_worker(Mock(), pipeline, self.store, self.control, app.arguments([])))
        await asyncio.sleep(0)
        pipeline.profile_worker.assert_not_awaited()
        self.control.stop.set()
        with self.assertRaises(app.StopRequested):
            await asyncio.wait_for(task, 1)

    async def test_one_search_yields_when_enrichment_backlog_is_full(self):
        store = Mock()
        args = app.arguments([])
        store.backlog.return_value = args.max_backlog
        with patch.object(self.control, "sleep", new=AsyncMock()) as sleep:
            await app.discovery_worker(Mock(), store, self.control, args, once=True)
        sleep.assert_awaited_once_with(2)
        store.choose_query.assert_not_called()

    async def test_discovery_scrolls_past_known_accounts_to_reach_new_nano_creator(self):
        args = app.arguments(["--max-scrolls", "8", "--stagnant-scrolls", "2"])
        # Previously rejected top results still lead to new results further down.
        for index in range(4):
            self.store.add_candidate(f"/@known{index}", "beauty")
            self.store.finish_job(f"known{index}", "out_of_range", delay=86400,
                                  data={"followers_count": 100000})
        screens = [[f"https://www.tiktok.com/@known{index}/video/{index}"] for index in range(4)]
        screens += [["https://www.tiktok.com/@nano/video/123?lang=en", "/@nano"], [], []]
        page = Mock(wait_for_selector=AsyncMock(),
                    eval_on_selector_all=AsyncMock(side_effect=screens), evaluate=AsyncMock())
        with patch.object(app, "navigate", new=AsyncMock()) as navigate, patch.object(
            app, "handle_captcha", new=AsyncMock(return_value=False)
        ), patch.object(self.control, "navigation_slot", new=AsyncMock()), patch.object(
            self.control, "sleep", new=AsyncMock()
        ):
            await app.discovery_worker(page, self.store, self.control, args, once=True)
        self.assertIn("/search/video?q=beauty", navigate.await_args.args[1])
        self.assertEqual(page.eval_on_selector_all.await_count, 7)
        self.assertEqual(self.store.claim_job()["username"], "nano")
        row = self.store.conn.execute("SELECT discovered FROM queries WHERE term='beauty'").fetchone()
        self.assertEqual(row["discovered"], 1)

    async def test_discovery_stops_when_same_creator_repeats_in_different_video_links(self):
        args = app.arguments(["--max-scrolls", "8", "--stagnant-scrolls", "2"])
        page = Mock(wait_for_selector=AsyncMock(), eval_on_selector_all=AsyncMock(side_effect=[
            ["/@nano/video/1"], ["/@nano/video/2"], ["/@nano?lang=en"]
        ]), evaluate=AsyncMock())
        with patch.object(app, "navigate", new=AsyncMock()), patch.object(
            app, "handle_captcha", new=AsyncMock(return_value=False)
        ), patch.object(self.control, "navigation_slot", new=AsyncMock()), patch.object(
            self.control, "sleep", new=AsyncMock()
        ):
            await app.discovery_worker(page, self.store, self.control, args, once=True)
        self.assertEqual(page.eval_on_selector_all.await_count, 3)
        self.assertEqual(self.store.backlog(), 1)

    async def test_rate_limit_is_shared_between_workers(self):
        starts = []
        async def slot():
            await self.control.navigation_slot()
            starts.append(time.monotonic())
        await asyncio.gather(slot(), slot(), slot())
        self.assertGreaterEqual(starts[1] - starts[0], 0.025)
        self.assertGreaterEqual(starts[2] - starts[1], 0.025)

    async def test_stop_interrupts_paused_workers(self):
        self.control.ready.clear()
        task = asyncio.create_task(self.control.wait_ready())
        await asyncio.sleep(0)
        self.control.stop.set()
        with self.assertRaises(app.StopRequested):
            await asyncio.wait_for(task, timeout=1)

    async def test_dom_extraction_preserves_exact_follower_counter(self):
        page = Mock()
        page.wait_for_function = AsyncMock()
        page.evaluate = AsyncMock(return_value={"states": [], "followers": "1,234",
                                               "bio": "inquiries: pr-anna@gmail.com",
                                               "link": "https://anna.com"})
        data = await app.extract_profile(page, "anna")
        self.assertEqual(data["followers_count"], 1234)
        self.assertEqual(data["bio_link"], "https://anna.com")

    async def test_json_extraction_matches_requested_creator(self):
        state = {"UserModule": {
            "users": {"wrong": {"uniqueId": "other", "signature": "Wrong bio"},
                      "correct": {"uniqueId": "Anna", "signature": "Beauty creator", "bioLink": {"link": "https://anna.com"}}},
            "stats": {"wrong": {"followerCount": 900000}, "correct": {"followerCount": 1234}}}}
        page = Mock()
        page.wait_for_function = AsyncMock()
        page.evaluate = AsyncMock(return_value={"states": [json.dumps(state)], "followers": None, "bio": "", "link": ""})
        data = await app.extract_profile(page, "anna")
        self.assertEqual(data["followers_count"], 1234)
        self.assertEqual(data["bio"], "Beauty creator")

    async def test_late_hydration_rereads_bio_and_link(self):
        page = Mock()
        page.wait_for_function = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[
            {"states": ["{}"], "followers": None, "bio": "", "link": ""},
            {"bio": "London | pr-anna@gmail.com", "link": "https://anna.com"}])
        counter = Mock()
        counter.get_attribute = AsyncMock(return_value="1234")
        page.wait_for_selector = AsyncMock(return_value=counter)
        data = await app.extract_profile(page, "anna")
        self.assertIn("pr-anna@gmail.com", data["bio"])
        self.assertEqual(data["followers_count"], 1234)

    async def test_two_captchas_keep_global_pause_until_both_tabs_finish(self):
        first, second = Mock(), Mock()
        first.url, second.url = "first", "second"
        first.bring_to_front = AsyncMock()
        second.bring_to_front = AsyncMock()
        calls = {first: 0, second: 0}
        saw_second_pause = []
        async def visible(page):
            calls[page] += 1
            return calls[page] <= 2
        async def quick_sleep(_):
            await asyncio.sleep(0)
            if first in self.control.captcha_pages and second in self.control.captcha_pages:
                saw_second_pause.append(not self.control.ready.is_set())
        with patch.object(app, "captcha_visible", side_effect=visible), patch.object(
            self.control, "sleep", side_effect=quick_sleep
        ):
            await asyncio.gather(app.handle_captcha(first, self.control), app.handle_captcha(second, self.control))
        self.assertTrue(any(saw_second_pause))
        self.assertTrue(self.control.ready.is_set())

    async def test_navigation_retries_same_task_after_captcha_timeout(self):
        page = Mock()
        response = Mock()
        response.status = 200
        page.goto = AsyncMock(side_effect=[TimeoutError(), response])
        with patch.object(app, "handle_captcha", new=AsyncMock(side_effect=[True, False])):
            await app.navigate(page, "https://www.tiktok.com/@anna", self.control)
        self.assertEqual(page.goto.await_count, 2)
        self.assertEqual(page.goto.await_args_list[0].args, page.goto.await_args_list[1].args)

    async def test_contact_crawler_follows_contact_page_and_uses_cache(self):
        crawler = app.ContactCrawler(self.store, self.control, 3)
        def fetch(url):
            if url == "https://anna.com":
                return {"emails": [], "links": [["/contact", "Contact"]], "url": url}
            return {"emails": ["pr-anna@gmail.com"], "links": [], "url": url}
        with patch.object(app, "fetch_public_page", side_effect=fetch) as mocked:
            emails, errors = await crawler.collect("https://anna.com")
            await crawler.collect("https://anna.com")
            self.assertEqual(mocked.call_count, 2)
        self.assertFalse(errors)
        self.assertIn(("pr-anna@gmail.com", "contact_page"), emails)

    async def test_concurrent_crawls_share_url_request(self):
        crawler = app.ContactCrawler(self.store, self.control, 1)
        result = {"emails": [], "links": [], "url": "https://anna.com"}
        with patch.object(app, "fetch_public_page", return_value=result) as fetch:
            await asyncio.gather(crawler.fetch("https://anna.com"), crawler.fetch("https://anna.com"))
            self.assertEqual(fetch.call_count, 1)

    async def test_pipeline_preserves_queued_candidates_during_batch_rest(self):
        for index in range(65):
            self.store.add_candidate(f"/@creator{index}", "beauty")
        args = app.arguments(["--batch-size", "1", "--batch-rest", "10", "--dry-run"])
        importer = app.LegacyImporter(self.store, [])
        pipeline = app.Pipeline(self.store, self.control, args, importer)
        job = self.store.claim_job()
        data = {"bio": "Beauty creator London", "followers_count": 1000}
        self.assertTrue(pipeline.accept(job, data, [("anna@gmail.com", "bio")]))
        self.assertEqual(self.store.backlog(), 64)
        self.assertGreater(self.control.batch_until, time.monotonic())
        self.assertEqual(self.store.summary()["deliveries"], {"held": 1})

    async def test_profile_to_email_to_durable_outbox(self):
        self.store.add_candidate("/@sarah.grwm/video/123", "beauty")
        args = app.arguments(["--dry-run"])
        pipeline = app.Pipeline(self.store, self.control, args, app.LegacyImporter(self.store, []))
        page = Mock()
        page.url = "https://www.tiktok.com/@sarah.grwm"
        page.is_closed.return_value = False
        data = {"bio": "London | inquiries: welcome", "followers_count": 1200, "bio_link": "https://anna.com"}
        async def extract(*_):
            # Finish this iteration before stopping the worker.
            self.control.stop.set()
            return data
        with patch.object(app, "navigate", new=AsyncMock()), patch.object(app, "extract_profile", side_effect=extract):
            await pipeline.profile_worker(page, 1)
        self.assertEqual(self.store.summary()["profiles"], {"enrichment_pending": 1})
        query = self.store.conn.execute("SELECT range_checked,in_range FROM queries WHERE term='beauty'").fetchone()
        self.assertEqual(tuple(query), (1, 1))
        self.control.stop.clear()
        async def collect(_):
            self.control.stop.set()
            return [("pr-sarah@gmail.com", "contact_page")], False
        with patch.object(pipeline.crawler, "collect", side_effect=collect):
            await pipeline.email_worker(1)
        self.assertEqual(self.store.summary()["profiles"], {"qualified": 1})
        self.assertEqual(self.store.summary()["deliveries"], {"held": 1})

    async def test_qualified_notice_does_not_claim_webhook_acceptance(self):
        self.store.add_candidate("/@sarah", "beauty")
        pipeline = app.Pipeline(self.store, self.control, app.arguments([]), app.LegacyImporter(self.store, []))
        job = self.store.claim_job()
        with self.assertLogs(app.LOG, level="INFO") as captured:
            pipeline.accept(job, {"bio": "Beauty creator London", "followers_count": 1200},
                            [("pr-sarah@gmail.com", "bio")])
        output = "\n".join(captured.output)
        self.assertIn("QUALIFIED LEAD #1 THIS RUN", output)
        self.assertIn("pr-sarah@gmail.com", output)
        self.assertIn("QUEUED FOR N8N (awaiting acceptance)", output)
        self.assertNotIn("N8N ACCEPTED", output)

    async def test_delivery_success_reports_the_actual_http_status(self):
        self.store.save_lead(record())
        async def post(*_):
            self.control.stop.set()
            return True, "HTTP 201", 0
        with patch.object(app.asyncio, "to_thread", side_effect=post), self.assertLogs(app.LOG, level="INFO") as captured:
            await app.delivery_worker(self.store, self.control, app.arguments([]), app.LegacyImporter(self.store, []))
        output = "\n".join(captured.output)
        self.assertIn("N8N ACCEPTED - HTTP 201", output)
        self.assertIn("pr-sarah@gmail.com", output)
        self.assertIn("N8N ACCEPTED: 1", output)
        self.assertEqual(self.store.summary()["deliveries"], {"sent": 1})
        self.assertEqual(self.store.conn.execute("SELECT last_error FROM leads").fetchone()[0], "")

    async def test_delivery_failure_never_reports_acceptance(self):
        self.store.save_lead(record())
        async def post(*_):
            self.control.stop.set()
            return False, "HTTP 500", 0
        with patch.object(app.asyncio, "to_thread", side_effect=post), self.assertLogs(app.LOG, level="INFO") as captured:
            await app.delivery_worker(self.store, self.control, app.arguments([]), app.LegacyImporter(self.store, []))
        output = "\n".join(captured.output)
        self.assertIn("N8N DELIVERY FAILED - SAVED FOR RETRY", output)
        self.assertIn("HTTP 500", output)
        self.assertNotIn("N8N ACCEPTED", output)
        self.assertEqual(self.store.summary()["deliveries"], {"pending": 1})

    async def test_dry_run_never_posts_even_existing_pending_leads(self):
        self.store.save_lead(record())
        async def stop(_):
            raise app.StopRequested()
        with patch.object(app, "post_webhook") as post, patch.object(self.control, "sleep", side_effect=stop):
            with self.assertRaises(app.StopRequested):
                await app.delivery_worker(self.store, self.control, app.arguments(["--dry-run"]), app.LegacyImporter(self.store, []))
        post.assert_not_called()
        self.assertEqual(self.store.summary()["deliveries"], {"pending": 1})

    async def test_stats_is_read_only_and_does_not_recover_live_claims(self):
        self.store.add_candidate("/@anna", "beauty")
        self.store.claim_job()
        args = app.arguments(["--stats", "--state-dir", self.temp.name])
        # run() expects the standard database filename.
        self.store.close()
        Path(self.temp.name, "db.sqlite3").rename(Path(self.temp.name, "leads.sqlite3"))
        self.store = app.Store(Path(self.temp.name) / "leads.sqlite3")
        self.store.claim_job()
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            await app.run(args)
        self.assertEqual(json.loads(stdout.getvalue())["profiles"], {"in_profile": 1})
        self.assertEqual(self.store.summary()["profiles"], {"in_profile": 1})


if __name__ == "__main__":
    unittest.main()

import io
import json
import pathlib
import tempfile
import unittest
import urllib.error

import http_transport

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, data, status=200, headers=None):
        self.data = data
        self.status = status
        self.headers = {} if headers is None else headers


def not_modified(etag=None):
    headers = {} if etag is None else {"ETag": etag}
    return urllib.error.HTTPError(
        "https://1f916.ai/api/post/6108",
        304,
        "Not Modified",
        headers,
        io.BytesIO(b""),
    )


class ConditionalCacheContractTests(unittest.TestCase):
    def test_200_etag_then_304_replays_exact_cached_data(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            seen = []

            def requester(path, method="GET", payload=None, auth=False, headers=None):
                seen.append(headers or {})
                if len(seen) == 1:
                    return FakeResponse(
                        {"post": {"id": 6108, "title": "cached"}},
                        headers={"ETag": '"v1"', "Cache-Control": "private, max-age=0"},
                    )
                self.assertEqual(headers, {"If-None-Match": '"v1"'})
                raise not_modified('"v1"')

            first = http_transport.invoke(
                "read", "read_post", {"post_id": 6108},
                requester=requester,
                cache_path=cache,
            )
            second = http_transport.invoke(
                "read", "read_post", {"post_id": 6108},
                requester=requester,
                cache_path=cache,
            )

            self.assertEqual(first["status"], "OK")
            self.assertEqual(first["cache_status"], "STORED")
            self.assertEqual(first["etag"], '"v1"')
            self.assertEqual(second["status"], "OK")
            self.assertEqual(second["cache_status"], "REVALIDATED")
            self.assertEqual(second["etag"], '"v1"')
            self.assertEqual(second["data"], first["data"])
            self.assertEqual(seen, [{}, {"If-None-Match": '"v1"'}])

    def test_304_without_matching_cache_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"

            result = http_transport.invoke(
                "read", "read_post", {"post_id": 6108},
                requester=lambda *args, **kwargs: (_ for _ in ()).throw(not_modified()),
                cache_path=cache,
            )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["cache_status"], "MISS_ON_304")
            self.assertNotIn("data", result)

    def test_changed_etag_replaces_cached_body_and_validator(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            seen = []

            def requester(path, method="GET", payload=None, auth=False, headers=None):
                seen.append(headers or {})
                if len(seen) == 1:
                    return FakeResponse(
                        {"post": {"id": 6108, "title": "old"}},
                        headers={"ETag": '"v1"'},
                    )
                if len(seen) == 2:
                    self.assertEqual(headers, {"If-None-Match": '"v1"'})
                    return FakeResponse(
                        {"post": {"id": 6108, "title": "new"}},
                        headers={"ETag": '"v2"'},
                    )
                self.assertEqual(headers, {"If-None-Match": '"v2"'})
                raise not_modified('"v2"')

            for _ in range(3):
                result = http_transport.invoke(
                    "read", "read_post", {"post_id": 6108},
                    requester=requester,
                    cache_path=cache,
                )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["cache_status"], "REVALIDATED")
            self.assertEqual(result["etag"], '"v2"')
            self.assertEqual(result["data"]["post"]["title"], "new")

    def test_no_store_response_invalidates_prior_cache_and_is_never_stored(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            seen = []

            def requester(path, method="GET", payload=None, auth=False, headers=None):
                seen.append(headers or {})
                if len(seen) == 1:
                    return FakeResponse(
                        {"post": {"id": 6108, "title": "old"}},
                        headers={"ETag": '"v1"'},
                    )
                if len(seen) == 2:
                    self.assertEqual(headers, {"If-None-Match": '"v1"'})
                    return FakeResponse(
                        {"post": {"id": 6108, "title": "live-no-store"}},
                        headers={"ETag": '"v2"', "Cache-Control": "no-store"},
                    )
                self.assertIsNone(headers)
                return FakeResponse(
                    {"post": {"id": 6108, "title": "third-live"}},
                    headers={"Cache-Control": "no-store"},
                )

            first = http_transport.invoke("read", "read_post", {"post_id": 6108}, requester=requester, cache_path=cache)
            second = http_transport.invoke("read", "read_post", {"post_id": 6108}, requester=requester, cache_path=cache)
            third = http_transport.invoke("read", "read_post", {"post_id": 6108}, requester=requester, cache_path=cache)
            self.assertEqual(first["cache_status"], "STORED")
            self.assertEqual(second["cache_status"], "NO_STORE")
            self.assertEqual(third["cache_status"], "NO_STORE")
            self.assertEqual(seen[2], {})

    def test_transport_failure_never_replays_cached_body(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            calls = 0

            def requester(path, method="GET", payload=None, auth=False, headers=None):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return FakeResponse(
                        {"post": {"id": 6108, "title": "old"}},
                        headers={"ETag": '"v1"'},
                    )
                raise urllib.error.URLError("network down")

            first = http_transport.invoke("read", "read_post", {"post_id": 6108}, requester=requester, cache_path=cache)
            failed = http_transport.invoke("read", "read_post", {"post_id": 6108}, requester=requester, cache_path=cache)
            self.assertEqual(first["status"], "OK")
            self.assertEqual(failed["status"], "BLOCKED")
            self.assertNotIn("data", failed)
            self.assertNotEqual(failed.get("cache_status"), "REVALIDATED")

    def test_corrupt_cache_is_rebuilt_as_miss_without_social_state_dependency(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            cache.write_text("{broken")
            seen = []

            result = http_transport.invoke(
                "read", "read_post", {"post_id": 6108},
                requester=lambda path, method="GET", payload=None, auth=False, headers=None: seen.append(headers or {}) or FakeResponse(
                    {"post": {"id": 6108, "title": "fresh"}},
                    headers={"ETag": '"fresh"'},
                ),
                cache_path=cache,
            )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["cache_status"], "STORED")
            self.assertEqual(seen, [{}])
            raw = json.loads(cache.read_text())
            self.assertEqual(raw["schema_version"], 1)

    def test_cache_file_is_mode_0600_and_secret_free(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            http_transport.invoke(
                "read", "read_post", {"post_id": 6108},
                requester=lambda *args, **kwargs: FakeResponse(
                    {"post": {"id": 6108, "title": "public"}},
                    headers={"ETag": '"v1"'},
                ),
                cache_path=cache,
            )
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            text = cache.read_text().lower()
            self.assertNotIn("authorization", text)
            self.assertNotIn("bearer", text)
            self.assertNotIn("credential", text)

    def test_authenticated_safe_reads_do_not_use_public_etag_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            seen = []
            for _ in range(2):
                result = http_transport.invoke(
                    "citizen", "me", {"cursor_mode": "id"},
                    requester=lambda path, method="GET", payload=None, auth=False, headers=None: seen.append((auth, headers or {})) or FakeResponse(
                        {"ack_cursor": None},
                        headers={"ETag": '"private-v1"'},
                    ),
                    cache_path=cache,
                )
                self.assertEqual(result["status"], "OK")
                self.assertNotIn("cache_status", result)
            self.assertEqual(seen, [(True, {}), (True, {})])
            self.assertFalse(cache.exists())

    def test_cache_persistence_failure_never_turns_live_200_into_read_failure(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            with mock.patch("forum_cache.store", side_effect=OSError("cache fsync failed")):
                result = http_transport.invoke(
                    "read", "read_post", {"post_id": 6108},
                    requester=lambda *args, **kwargs: FakeResponse(
                        {"post": {"id": 6108, "title": "live"}},
                        headers={"ETag": '"v1"'},
                    ),
                    cache_path=cache,
                )
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["data"]["post"]["title"], "live")
            self.assertEqual(result["cache_status"], "DEGRADED")
            self.assertIn("cache fsync failed", result["cache_error"])

    def test_200_without_validator_discards_old_validator(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            seen = []

            def requester(path, method="GET", payload=None, auth=False, headers=None):
                seen.append(headers or {})
                if len(seen) == 1:
                    return FakeResponse(
                        {"post": {"id": 6108, "title": "old"}},
                        headers={"ETag": '"v1"'},
                    )
                if len(seen) == 2:
                    self.assertEqual(headers, {"If-None-Match": '"v1"'})
                    return FakeResponse({"post": {"id": 6108, "title": "unvalidated"}}, headers={})
                self.assertIsNone(headers)
                return FakeResponse({"post": {"id": 6108, "title": "third"}}, headers={})

            http_transport.invoke("read", "read_post", {"post_id": 6108}, requester=requester, cache_path=cache)
            second = http_transport.invoke("read", "read_post", {"post_id": 6108}, requester=requester, cache_path=cache)
            third = http_transport.invoke("read", "read_post", {"post_id": 6108}, requester=requester, cache_path=cache)
            self.assertEqual(second["cache_status"], "NO_VALIDATOR")
            self.assertEqual(third["cache_status"], "NO_VALIDATOR")
            self.assertEqual(seen[2], {})

    def test_tampered_cached_body_is_not_replayed_on_304(self):
        with tempfile.TemporaryDirectory() as td:
            cache = pathlib.Path(td) / "cache.json"
            http_transport.invoke(
                "read", "read_post", {"post_id": 6108},
                requester=lambda *args, **kwargs: FakeResponse(
                    {"post": {"id": 6108, "title": "good"}},
                    headers={"ETag": '"v1"'},
                ),
                cache_path=cache,
            )
            raw = json.loads(cache.read_text())
            entry = next(iter(raw["entries"].values()))
            entry["data"]["post"]["title"] = "tampered"
            cache.write_text(json.dumps(raw))

            result = http_transport.invoke(
                "read", "read_post", {"post_id": 6108},
                requester=lambda *args, **kwargs: (_ for _ in ()).throw(not_modified('"v1"')),
                cache_path=cache,
            )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["cache_status"], "MISS_ON_304")
            self.assertNotIn("data", result)

    def test_readme_documents_disposable_validator_only_cache_boundary(self):
        text = (ROOT / "README.md").read_text()
        self.assertIn("Conditional HTTP cache", text)
        self.assertIn("Cache-Control: no-store", text)
        self.assertIn("If-None-Match", text)
        self.assertIn("304 Not Modified", text)
        self.assertIn("transport failure never becomes cache success", text)
        self.assertIn("authenticated `pulse`/`me` reads are intentionally excluded", text)
        self.assertIn("never affect `.forum-state.json` or `.forum-operations.json`", text)


if __name__ == "__main__":
    unittest.main()

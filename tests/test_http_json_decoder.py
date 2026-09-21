from __future__ import annotations

import io
import unittest

import client
import http_transport


class RawResponse(io.BytesIO):
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class HttpJsonDecoderTests(unittest.TestCase):
    def _with_raw(self, raw: bytes, callback):
        original = client.urllib.request.urlopen
        try:
            client.urllib.request.urlopen = lambda req, timeout=20: RawResponse(raw)
            return callback()
        finally:
            client.urllib.request.urlopen = original

    def test_valid_raw_json_is_unchanged(self):
        response = self._with_raw(
            b'{"since_last_visit":{"comments":[1]},"ok":true}',
            lambda: client.request_with_meta("/api/test"),
        )
        self.assertEqual(
            response.data,
            {"since_last_visit": {"comments": [1]}, "ok": True},
        )

    def test_duplicate_top_level_key_is_rejected_from_raw_bytes(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON object key: since_last_visit"):
            self._with_raw(
                b'{"since_last_visit":{"comments":[1]},"since_last_visit":{"comments":[]}}',
                lambda: client.request_with_meta("/api/test"),
            )

    def test_duplicate_nested_key_is_rejected_recursively(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON object key: comments"):
            self._with_raw(
                b'{"since_last_visit":{"comments":[1],"comments":[]}}',
                lambda: client.request_with_meta("/api/test"),
            )

    def test_transport_rejects_non_object_json_roots_from_raw_bytes(self):
        cases = (
            (b'[]', 'list'),
            (b'null', 'null'),
            (b'"oops"', 'string'),
        )
        for raw, label in cases:
            with self.subTest(label=label):
                result = self._with_raw(
                    raw,
                    lambda: http_transport.invoke(
                        "read",
                        "search",
                        {"query": "specimen"},
                        cache_path=None,
                    ),
                )
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["route"], "http:GET /api/search")
                self.assertIn("JSON object", result["error"])
                self.assertNotIn("data", result)

    def test_transport_keeps_valid_object_root_ok(self):
        result = self._with_raw(
            b'{"results":[],"has_more":false}',
            lambda: http_transport.invoke(
                "read",
                "search",
                {"query": "specimen"},
                cache_path=None,
            ),
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["data"]["results"], [])
        self.assertFalse(result["data"]["has_more"])

    def test_transport_reports_duplicate_key_as_blocked_not_empty_success(self):
        result = self._with_raw(
            b'{"since_last_visit":{"comments":[1]},"since_last_visit":{"comments":[]}}',
            lambda: http_transport.invoke(
                "read",
                "read_post",
                {"post_id": 6108},
                cache_path=None,
            ),
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["route"], "http:GET /api/post/6108")
        self.assertIn("duplicate JSON object key", result["error"])
        self.assertNotIn("data", result)


if __name__ == "__main__":
    unittest.main()

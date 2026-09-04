from __future__ import annotations

import io
import json
import urllib.error
import unittest
from unittest.mock import patch

from naia_exten.features.comic_maker.client import ComicServerClient, ComicServerError


class _Response:
    def __init__(self, lines):
        self._stream = io.BytesIO(b"".join(lines))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def readline(self):
        return self._stream.readline()

    def read(self):
        return self._stream.read()


def _line(payload):
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


class ComicMakerClientTests(unittest.TestCase):
    def test_stream_forwards_progress_and_returns_complete_plan(self):
        progress = []
        response = _Response([
            _line({"type": "progress", "message": "Story 완료"}),
            _line({"type": "progress", "message": "ComicPlan 완료"}),
            _line({"type": "complete", "plan": {"id": 7}}),
        ])
        with patch("naia_exten.features.comic_maker.client.urllib.request.urlopen", return_value=response):
            result = ComicServerClient("http://127.0.0.1").generate_plan({}, progress.append)
        self.assertEqual(result, {"id": 7})
        self.assertEqual(progress, ["Story 완료", "ComicPlan 완료"])

    def test_stream_error_raises(self):
        response = _Response([_line({"type": "error", "message": "실패"})])
        with patch("naia_exten.features.comic_maker.client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ComicServerError, "실패"):
                ComicServerClient("http://127.0.0.1").generate_plan({})

    def test_404_stream_falls_back_to_json_route(self):
        stream_error = urllib.error.HTTPError(
            "http://127.0.0.1/api/comics/generate/stream", 404, "missing", {}, io.BytesIO()
        )
        fallback = _Response([json.dumps({"id": 7}).encode("utf-8")])
        with patch(
            "naia_exten.features.comic_maker.client.urllib.request.urlopen",
            side_effect=[stream_error, fallback],
        ) as urlopen:
            result = ComicServerClient("http://127.0.0.1").generate_plan({})
        self.assertEqual(result, {"id": 7})
        self.assertTrue(urlopen.call_args_list[0].args[0].full_url.endswith("/generate/stream"))
        self.assertTrue(urlopen.call_args_list[1].args[0].full_url.endswith("/generate"))


if __name__ == "__main__":
    unittest.main()

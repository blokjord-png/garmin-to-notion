import json
import unittest
from unittest.mock import patch

import httpx

from src.helpers._get_notion_client import RetryingNotionClient
from notion_client.errors import APIResponseError, HTTPResponseError


def _response(status_code, body=None, headers=None):
    return httpx.Response(
        status_code,
        headers=headers or {},
        content=json.dumps(body or {}).encode(),
        request=httpx.Request("POST", "https://api.notion.com/v1/databases/x/query"),
    )


RATE_LIMITED = {
    "object": "error",
    "status": 429,
    "code": "rate_limited",
    "message": "You have been rate limited. Please try again later.",
}


class RetryingNotionClientTests(unittest.TestCase):
    def _client(self, responses, **kwargs):
        sleeps = []
        client = RetryingNotionClient(
            auth="secret",
            min_request_interval=0,
            backoff_seconds=1,
            sleep_fn=sleeps.append,
            **kwargs,
        )
        sender = patch.object(httpx.Client, "send", side_effect=list(responses))
        return client, sleeps, sender

    def test_retries_on_429_and_honours_retry_after(self):
        client, sleeps, sender = self._client([
            _response(429, RATE_LIMITED, {"Retry-After": "2"}),
            _response(200, {"results": [], "has_more": False}),
        ])

        with sender as send:
            result = client.databases.query(database_id="x")

        self.assertEqual(result["results"], [])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(sleeps, [2.0])

    def test_falls_back_to_exponential_backoff_without_retry_after(self):
        client, sleeps, sender = self._client([
            _response(429, RATE_LIMITED),
            _response(429, RATE_LIMITED),
            _response(200, {"results": [], "has_more": False}),
        ])

        with sender as send:
            client.databases.query(database_id="x")

        self.assertEqual(send.call_count, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_gives_up_after_max_retries(self):
        client, sleeps, sender = self._client(
            [_response(429, RATE_LIMITED, {"Retry-After": "1"})] * 3,
            max_retries=2,
        )

        with sender as send, self.assertRaises(APIResponseError):
            client.databases.query(database_id="x")

        self.assertEqual(send.call_count, 3)
        self.assertEqual(sleeps, [1.0, 1.0])

    def test_does_not_retry_client_errors(self):
        client, sleeps, sender = self._client([
            _response(400, {"object": "error", "status": 400,
                            "code": "validation_error", "message": "bad"}),
        ])

        with sender as send, self.assertRaises(HTTPResponseError):
            client.databases.query(database_id="x")

        self.assertEqual(send.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_throttles_consecutive_requests(self):
        sleeps = []
        client = RetryingNotionClient(
            auth="secret",
            min_request_interval=0.35,
            sleep_fn=sleeps.append,
        )
        responses = [_response(200, {"results": [], "has_more": False})] * 2

        with patch.object(httpx.Client, "send", side_effect=responses):
            client.databases.query(database_id="x")
            client.databases.query(database_id="x")

        # First request never waits; the second waits for the remainder of the interval.
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)
        self.assertLessEqual(sleeps[0], 0.35)


if __name__ == "__main__":
    unittest.main()

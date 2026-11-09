import unittest

from tests.fake_openai import FakeOpenAI, reply

from nina.agents import openai_client
from nina.agents.openai_client import ApiError


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeOpenAI()
        self.addCleanup(self.api.close)

    def chat(self, key="k-123", timeout=10, headers=None):
        return openai_client.chat(self.api.base_url, key, "m1", [{"role": "user", "content": "hi"}],
                                  [{"type": "function", "function": {"name": "Read"}}], timeout, headers or {})

    def test_request_shape_and_answer(self):
        self.api.script.append(reply("hello"))
        data = self.chat(headers={"X-Title": "NINA"})
        self.assertEqual(data["choices"][0]["message"]["content"], "hello")
        req = self.api.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"]["Authorization"], "Bearer k-123")
        self.assertEqual(req["headers"]["X-Title"], "NINA")
        self.assertEqual(req["json"]["model"], "m1")
        self.assertEqual(req["json"]["tools"][0]["function"]["name"], "Read")
        self.assertNotIn("stream", req["json"])

    def test_no_key_no_authorization_header_and_no_empty_tools(self):
        self.api.script.append(reply("x"))
        openai_client.chat(self.api.base_url, "", "m1", [{"role": "user", "content": "hi"}], [], 10, {})
        self.assertNotIn("Authorization", self.api.requests[0]["headers"])
        self.assertNotIn("tools", self.api.requests[0]["json"])

    def test_errors_carry_status_and_provider_message(self):
        self.api.script.append((401, {"error": {"message": "API key not valid"}}, {}))
        with self.assertRaises(ApiError) as e:
            self.chat()
        self.assertEqual((e.exception.status, e.exception.message), (401, "API key not valid"))
        self.assertIn("API key not valid", openai_client.friendly(e.exception))

        self.api.script.append((404, [{"error": {"code": 404, "message": "This model is no longer available"}}], {}))
        with self.assertRaises(ApiError) as e:
            self.chat()
        self.assertIn("no longer available", e.exception.message)

        self.api.script.append((429, {"error": {"message": "Quota exceeded"}}, {"Retry-After": "37"}))
        with self.assertRaises(ApiError) as e:
            self.chat()
        self.assertEqual((e.exception.status, e.exception.retry_after), (429, "37"))
        self.assertIn("37", openai_client.friendly(e.exception))

    def test_non_json_error_body(self):
        self.api.script.append((502, "<html>Bad gateway</html>", {}))
        with self.assertRaises(ApiError) as e:
            self.chat()
        self.assertEqual(e.exception.status, 502)
        self.assertIn("Bad gateway", e.exception.message)

    def test_invalid_json_answer(self):
        self.api.script.append((200, "not json", {}))
        with self.assertRaises(ApiError):
            self.chat()

    def test_timeout(self):
        self.api.script.append(("sleep", 2, reply("late")))
        with self.assertRaises(ApiError) as e:
            self.chat(timeout=0.5)
        self.assertEqual(e.exception.status, 0)
        self.assertIn("0.5", openai_client.friendly(e.exception))

    def test_unreachable_server(self):
        with self.assertRaises(ApiError) as e:
            openai_client.chat("http://127.0.0.1:9/v1", "", "m", [], [], 2, {})
        self.assertEqual(e.exception.status, 0)

    def test_list_models_strips_prefix_and_sorts(self):
        self.assertEqual(openai_client.list_models(self.api.base_url, "k"), ["fake-a", "fake-b"])


if __name__ == "__main__":
    unittest.main()

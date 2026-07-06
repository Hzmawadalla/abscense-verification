"""Contract for the DingTalk client: message shape, token caching, error handling."""
import pytest

from app.dingtalk import DingTalkClient, DingTalkError, link_action_card


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class FakeHttp:
    def __init__(self, token_payload, send_payload):
        self.token_payload = token_payload
        self.send_payload = send_payload
        self.get_calls = 0
        self.post_calls = []

    def get(self, url, params=None):
        self.get_calls += 1
        return FakeResp(self.token_payload)

    def post(self, url, params=None, json=None):
        self.post_calls.append({"url": url, "params": params, "json": json})
        return FakeResp(self.send_payload)


def _client(http, clock=None):
    return DingTalkClient("k", "s", 42, http=http, clock=(clock or (lambda: 1000.0)))


def test_action_card_contains_link_and_count():
    msg = link_action_card("Ahmed", "https://app/?t=abc", 3)
    assert msg["msgtype"] == "action_card"
    assert msg["action_card"]["single_url"] == "https://app/?t=abc"
    assert "3" in msg["action_card"]["markdown"]


def test_send_link_posts_expected_body():
    http = FakeHttp({"errcode": 0, "access_token": "T", "expires_in": 7200},
                    {"errcode": 0, "task_id": 99})
    out = _client(http).send_link("user123", "Ahmed", "https://app/?t=abc", 2)
    assert out["task_id"] == 99
    body = http.post_calls[0]["json"]
    assert body["agent_id"] == 42
    assert body["userid_list"] == "user123"
    assert body["msg"]["action_card"]["single_url"].endswith("t=abc")
    assert http.post_calls[0]["params"]["access_token"] == "T"


def test_token_is_cached_across_calls():
    http = FakeHttp({"errcode": 0, "access_token": "T", "expires_in": 7200},
                    {"errcode": 0})
    c = _client(http)
    c.send_link("u", "n", "l", 1)
    c.send_link("u", "n", "l", 1)
    assert http.get_calls == 1  # token fetched once, reused


def test_errcode_raises():
    http = FakeHttp({"errcode": 40001, "errmsg": "bad secret"}, {})
    with pytest.raises(DingTalkError):
        _client(http).access_token()

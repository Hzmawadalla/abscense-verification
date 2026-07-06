"""DingTalk work-notification dispatch (private DM to a TL by userid).

Corporate internal app flow:
  1. GET  /gettoken?appkey&appsecret                      -> access_token (cached ~2h)
  2. POST /topapi/message/corpconversation/asyncsend_v2   -> send an actionCard to userid_list

The HTTP transport is injectable so message-building and token caching are unit-testable without
network. Configure APP_KEY / APP_SECRET / AGENT_ID in Streamlit secrets."""
import time

BASE = "https://oapi.dingtalk.com"
TITLE = "考勤确认 · Attendance Verification"


class DingTalkError(RuntimeError):
    pass


def link_action_card(name: str, link: str, case_count: int) -> dict:
    """The actionCard message body with a single button opening the TL's unique link."""
    who = name or "there"
    md = (f"Hi {who},\n\n"
          f"You have **{case_count}** attendance case(s) awaiting your confirmation.\n\n"
          f"Please review and submit before the period closes.")
    return {
        "msgtype": "action_card",
        "action_card": {
            "title": TITLE,
            "markdown": md,
            "single_title": "Open verification",
            "single_url": link,
        },
    }


class DingTalkClient:
    def __init__(self, app_key, app_secret, agent_id, http=None, base=BASE, clock=time.time):
        self.app_key = app_key
        self.app_secret = app_secret
        self.agent_id = agent_id
        self.base = base
        self.clock = clock
        self._http = http
        self._token = None
        self._token_exp = 0.0

    def _session(self):
        if self._http is None:
            import requests
            self._http = requests
        return self._http

    def access_token(self) -> str:
        now = self.clock()
        if self._token and now < self._token_exp - 60:
            return self._token
        r = self._session().get(f"{self.base}/gettoken",
                                params={"appkey": self.app_key, "appsecret": self.app_secret})
        data = r.json()
        if data.get("errcode"):
            raise DingTalkError(f"gettoken failed: {data}")
        self._token = data["access_token"]
        self._token_exp = now + data.get("expires_in", 7200)
        return self._token

    def send_work_notification(self, userid: str, msg: dict) -> dict:
        r = self._session().post(
            f"{self.base}/topapi/message/corpconversation/asyncsend_v2",
            params={"access_token": self.access_token()},
            json={"agent_id": self.agent_id, "userid_list": userid, "msg": msg})
        data = r.json()
        if data.get("errcode"):
            raise DingTalkError(f"asyncsend failed: {data}")
        return data

    def send_link(self, userid: str, name: str, link: str, case_count: int) -> dict:
        return self.send_work_notification(userid, link_action_card(name, link, case_count))

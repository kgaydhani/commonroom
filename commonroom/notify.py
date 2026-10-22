"""Owner notifications that reach a phone (commonroom#151).

The room acts on GitHub as the owner's own account, so its @mentions and assignments never notify the owner. A
notifier pushes them out of band instead. `[notify] ntfy_topic` in room.toml turns on ntfy (https://ntfy.sh: no
account; subscribe to the topic in the ntfy phone app). Anyone who knows a topic name can read it, so pick a long
random one. Unset: no notifier, nothing is sent.
"""
from __future__ import annotations

import json
import urllib.request

NTFY_SERVER = "https://ntfy.sh"
MESSAGE_MAX = 3500   # ntfy turns longer messages into attachments; a notification only needs the start


class Ntfy:
    """Publishes through ntfy's JSON API, which takes UTF-8 titles (headers would not)."""

    def __init__(self, topic: str, server: str = NTFY_SERVER, timeout: float = 15):
        self.topic, self.server, self.timeout = topic, server.rstrip("/"), timeout

    def send(self, title: str, message: str, url: str | None = None, priority: int | None = None):
        payload = {"topic": self.topic, "title": title[:200], "message": message[:MESSAGE_MAX] or title}
        if url:
            payload["click"] = url
        if priority:
            payload["priority"] = priority
        request = urllib.request.Request(self.server, data=json.dumps(payload).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read()


def from_config(cfg: dict):
    """The project's notifier, or None when `[notify]` sets none."""
    spec = cfg.get("notify") or {}
    topic = str(spec.get("ntfy_topic") or "").strip()
    return Ntfy(topic, spec.get("ntfy_server") or NTFY_SERVER) if topic else None

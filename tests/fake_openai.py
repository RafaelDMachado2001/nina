"""A tiny OpenAI-compatible server for tests: scripted responses, recorded requests. No network beyond 127.0.0.1."""
import http.server
import json
import threading
import time


def reply(content="", tool_calls=None, usage=(10, 5), finish=None, **extra_message):
    """A chat/completions body. `tool_calls`: [(id, name, arguments_json_or_dict, extra_fields_dict)]."""
    message = {"role": "assistant", "content": content, **extra_message}
    if tool_calls:
        message["tool_calls"] = []
        for call in tool_calls:
            cid, name, args, *more = call
            entry = {"id": cid, "type": "function",
                     "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}
            if more:
                entry.update(more[0])
            message["tool_calls"].append(entry)
    return {"id": "x", "object": "chat.completion", "model": "fake-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish or ("tool_calls" if tool_calls else "stop")}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}}


class FakeOpenAI:
    """`script` items: dict (200 JSON), or (status, body, headers), or ("sleep", seconds, item)."""

    def __init__(self):
        self.script, self.requests = [], []
        self.models = ["models/fake-b", "fake-a"]
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, body, headers=None):
                data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_GET(self):
                owner.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "json": None})
                self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in owner.models]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else None
                owner.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "json": body})
                item = owner.script.pop(0) if owner.script else reply("(no more scripted replies)")
                while isinstance(item, tuple) and item and item[0] == "sleep":
                    time.sleep(item[1])
                    item = item[2]
                if isinstance(item, tuple):
                    self._send(*item)
                else:
                    self._send(200, item)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def chat_requests(self):
        return [r["json"] for r in self.requests if r["method"] == "POST"]

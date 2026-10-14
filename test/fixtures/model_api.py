"""Loopback Chat Completions/SSE fixture for real SDK and subprocess tests."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextmanager
def model_api():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != "/chat/completions":
                self.send_error(404)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            prompt = request["messages"][-1]["content"]
            context = [
                message["content"].removeprefix("参考资料：\n")
                for message in request["messages"][:-1]
            ]
            output = f"[mock:{request['model']}] {prompt}"
            if context:
                output += " (context: " + " | ".join(context) + ")"
            output = output[: request["max_tokens"]]
            result = {
                "id": "synthetic-completion",
                "object": "chat.completion",
                "created": 1,
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": output},
                        "finish_reason": "stop",
                    }
                ],
            }
            if request["stream"]:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for start in range(0, len(output), 256):
                        event = dict(
                            result,
                            object="chat.completion.chunk",
                            choices=[
                                {
                                    "index": 0,
                                    "delta": {"content": output[start : start + 256]},
                                    "finish_reason": None,
                                }
                            ],
                        )
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                        self.wfile.flush()
                    event = dict(
                        result,
                        object="chat.completion.chunk",
                        choices=[
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                    )
                    self.wfile.write(
                        ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

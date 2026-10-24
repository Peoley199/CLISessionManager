"""Observe real Claude retry normalization using a synthetic localhost upstream."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OUT = Path(__file__).resolve().parents[1] / "output/qa/provider127"
RETRY = "Your tool call was malformed and could not be parsed. Please retry."
EXE = Path(os.environ["APPDATA"]) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"


def frame(kind, **data):
    return "event: " + kind + "\ndata: " + json.dumps({"type": kind, **data}) + "\n\n"


def run_probe():
    captured = []
    with tempfile.TemporaryDirectory(prefix="claude-retry-wire-") as tmp:
        root = Path(tmp)
        sample = root / "sample.txt"
        sample.write_text("synthetic sample", encoding="utf-8")

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.split("?")[0] != "/v1/messages":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"input_tokens": 1}')
                    return
                captured.append(body)
                turn = len(captured)
                if turn == 1:
                    blocks = [{"type": "tool_use", "id": "call_synthetic_read", "name": "Read",
                               "input": {"file_path": str(sample)}}]
                    stop = "tool_use"
                elif turn == 2:
                    blocks = [{"type": "thinking", "thinking": "Synthetic incomplete tool response.", "signature": ""}]
                    stop = "tool_use"
                else:
                    blocks = [{"type": "text", "text": "SYNTHETIC_RETRY_CAPTURED"}]
                    stop = "end_turn"
                raw = frame("message_start", message={"id": f"msg_synthetic_{turn}", "type": "message",
                            "role": "assistant", "model": body["model"], "content": [],
                            "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 0}})
                for i, block in enumerate(blocks):
                    if block["type"] == "tool_use":
                        raw += frame("content_block_start", index=i, content_block={**block, "input": {}})
                        raw += frame("content_block_delta", index=i, delta={"type": "input_json_delta", "partial_json": json.dumps(block["input"])})
                    elif block["type"] == "thinking":
                        raw += frame("content_block_start", index=i, content_block={**block, "thinking": ""})
                        raw += frame("content_block_delta", index=i, delta={"type": "thinking_delta", "thinking": block["thinking"]})
                    else:
                        raw += frame("content_block_start", index=i, content_block={"type": "text", "text": ""})
                        raw += frame("content_block_delta", index=i, delta={"type": "text_delta", "text": block["text"]})
                    raw += frame("content_block_stop", index=i)
                raw += frame("message_delta", delta={"stop_reason": stop, "stop_sequence": None},
                             usage={"output_tokens": 10})
                raw += frame("message_stop")
                encoded = raw.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE"))}
        env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="synthetic-local-only", CLAUDE_CONFIG_DIR=str(root / "config"),
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        try:
            result = subprocess.run([str(EXE), "-p", "Read sample.txt then say done.",
                                     "--model", "claude-sonnet-4-6", "--setting-sources", "",
                                     "--tools", "Read", "--allowedTools", "Read",
                                     "--max-turns", "4", "--no-session-persistence"],
                                    env=env, cwd=root, capture_output=True, timeout=60)
        finally:
            server.shutdown()
            server.server_close()
        # Save structure and synthetic messages only; omit system prompts and tool definitions.
        summary = {"exit_code": result.returncode, "request_count": len(captured),
                   "stdout": result.stdout.decode("utf-8", errors="replace")[-1000:],
                   "stderr": result.stderr.decode("utf-8", errors="replace")[-1000:],
                   "requests": []}
        for body in captured:
            messages = []
            for message in body["messages"]:
                content = message["content"]
                messages.append({"role": message["role"], "content": [
                    {"type": block["type"], **({"text": RETRY} if block.get("text") == RETRY else {})}
                    for block in content] if isinstance(content, list) else "[synthetic text]"})
            summary["requests"].append({"stream": body.get("stream"), "messages": messages})
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "claude-tool-retry-wire-probe.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary))
        assert result.returncode == 0 and len(captured) == 3, summary
        retry = captured[-1]["messages"][-1]
        assert retry["role"] == "user" and any(b.get("text") == RETRY for b in retry["content"])
        assert any(b.get("type") == "tool_result" for message in captured[-1]["messages"]
                   for b in message["content"] if isinstance(message["content"], list))
        assert not any(b.get("type") == "thinking" for message in captured[-1]["messages"]
                       for b in message["content"] if isinstance(message["content"], list))


if __name__ == "__main__":
    run_probe()

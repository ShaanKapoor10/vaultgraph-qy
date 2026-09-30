"""
An LLM endpoint served by Claude Code's own login -- for testing, not production.

EXPERIMENT (branch exp/haiku-gateway). Groq's free tier caps every model per
day, and on 2026-09-30 three measurements running at once spent all six keys
before any finished. This asks the other question: with a model that is not
rate-limited into the ground, how much of what the evaluations show is the
MODEL, and how much is the design? Every Brahmastra LLM call can be pointed
here and answered by Claude Haiku.

HOW. A small HTTP server on THIS machine, speaking the OpenAI chat-completions
shape, that answers each request by running Claude Code headless:

    claude -p --model haiku --system-prompt ... [--json-schema ...]
           --tools "" --strict-mcp-config --setting-sources "" --no-session-persistence

It must run on the host: the CLI's login lives here, and a container cannot
use it. Containers reach it at http://host.docker.internal:8790.

WHY THOSE FLAGS. Measured on the first call: with Claude Code's default tools
and settings each request carried 16,087 tokens of its own setup for a
9-token question -- $0.034 and 7.8 s. With no tools, no MCP servers and no
settings files: 1,311 tokens, $0.003, 3.0 s. `--bare` would strip more but
refuses the OAuth login and wants an API key, so it is not an option here.

WHAT IT DOES NOT DO: temperature (the CLI has no such flag -- recorded, and
ignored), streaming, or tools. Every call is logged, with tokens, cost and
time, to <data dir>/gateway-usage.jsonl, so the cost of an experiment is a
number rather than a guess.

    python -m brahmastra.claude_gateway                  # 127.0.0.1:8790
    python -m brahmastra.claude_gateway --port 8790 --concurrency 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_MODEL = "haiku"
DEFAULT_PORT = 8790
CALL_TIMEOUT = 180

_JSON_ONLY = ("\n\nRespond with ONE JSON object and nothing else: no prose before or "
              "after it, no code fences.")


def claude_binary() -> str:
    found = shutil.which("claude") or shutil.which("claude.cmd")
    if not found:
        raise RuntimeError("the claude CLI is not on PATH")
    return found


def _extract_json(text: str) -> str:
    """The first whole JSON object in a reply, for json_object requests."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return text
    candidate = text[start:end + 1]
    try:
        json.loads(candidate)
        return candidate
    except ValueError:
        return text


def _unwrap(value: Any) -> Any:
    """
    Given only {"type": "object"}, Haiku wraps its real answer as a string in a
    single field -- {"response": "{\\"decisions\\": [...]}"}. The object the
    prompt asked for is that string; anything else is returned as it came.
    """
    if isinstance(value, dict) and len(value) == 1:
        (inner,) = value.values()
        if isinstance(inner, str):
            try:
                parsed = json.loads(_extract_json(inner))
            except ValueError:
                return value
            if isinstance(parsed, dict):
                return parsed
    return value


def run_claude(system: str, user: str, model: str, schema: dict[str, Any] | None,
               json_object: bool) -> dict[str, Any]:
    """
    One headless call. Returns {text, usage, cost, ms} or raises RuntimeError.

    The system prompt goes in a FILE. On Windows `claude` is a .cmd shim, and
    an argument passed through cmd.exe is cut at its first newline: every
    multi-line prompt arrived as its first sentence, so the model never saw
    "Return ONLY a JSON object" and wrote Markdown minutes instead -- every
    comprehension call failed on the first real run.

    A json_object request is given a minimal schema ({"type": "object"}), so
    the CLI's structured output GUARANTEES an object rather than the prompt
    merely asking for one.
    """
    if json_object and schema is None:
        schema = {"type": "object"}
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(system + (_JSON_ONLY if json_object else ""))
        prompt_file = fh.name
    args = [claude_binary(), "-p", "--model", model, "--output-format", "json",
            "--no-session-persistence", "--tools", "", "--strict-mcp-config",
            "--setting-sources", "", "--system-prompt-file", prompt_file]
    if schema is not None:
        args += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
    started = time.perf_counter()
    try:
        proc = subprocess.run(args, input=user, capture_output=True, text=True,
                              encoding="utf-8", timeout=CALL_TIMEOUT)
    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
    ms = int((time.perf_counter() - started) * 1000)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"claude exited {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout)[:400]}") from None
    if out.get("is_error"):
        raise RuntimeError(f"claude reported an error: {str(out.get('result'))[:400]}")
    if out.get("structured_output") is not None:
        text = json.dumps(_unwrap(out["structured_output"]))
    else:
        text = out.get("result") or ""
        if json_object:
            text = _extract_json(text)
    return {"text": text, "usage": out.get("usage") or {},
            "cost": out.get("total_cost_usd"), "ms": ms}


class _Log:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def write(self, row: dict[str, Any]) -> None:
        from brahmastra.env import data_dir

        path = data_dir() / "gateway-usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


def make_handler(concurrency: int, token: str | None):
    gate = threading.Semaphore(concurrency)
    log = _Log()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:     # quiet by default
            pass

        def _send(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path.rstrip("/") in ("/health", "/v1/health"):
                self._send(200, {"ok": True, "model": DEFAULT_MODEL, "concurrency": concurrency})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/v1/chat/completions":
                self._send(404, {"error": "not found"})
                return
            if token and self.headers.get("Authorization", "") != f"Bearer {token}":
                self._send(401, {"error": {"message": "bad gateway token"}})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except ValueError:
                self._send(400, {"error": {"message": "request is not JSON"}})
                return
            messages = body.get("messages") or []
            system = "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
            user = "\n\n".join(m.get("content", "") for m in messages if m.get("role") != "system")
            fmt = body.get("response_format") or {}
            schema = (fmt.get("json_schema") or {}).get("schema") if fmt.get("type") == "json_schema" else None
            model = body.get("model") or DEFAULT_MODEL
            with gate:
                try:
                    out = run_claude(system, user, model, schema, fmt.get("type") == "json_object")
                except Exception as exc:                       # noqa: BLE001
                    log.write({"at": time.time(), "model": model, "error": str(exc)[:300]})
                    self._send(502, {"error": {"message": f"claude gateway: {exc}"[:500]}})
                    return
            usage = out["usage"]
            prompt = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                      + usage.get("cache_creation_input_tokens", 0))
            log.write({"at": time.time(), "model": model, "ms": out["ms"], "cost_usd": out["cost"],
                       "prompt_tokens": prompt, "completion_tokens": usage.get("output_tokens", 0)})
            self._send(200, {
                "id": f"gw-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": out["text"]}}],
                "usage": {"prompt_tokens": prompt, "completion_tokens": usage.get("output_tokens", 0)},
            })

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve LLM calls with Claude Code (testing only).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args(argv)
    claude_binary()                                   # fail now, not on the first request
    token = os.environ.get("CLAUDE_GATEWAY_TOKEN") or None
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.concurrency, token))
    print(f"claude gateway on http://{args.host}:{args.port} (model {DEFAULT_MODEL}, "
          f"{args.concurrency} at a time{', token required' if token else ''})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

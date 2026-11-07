#!/usr/bin/env python3
"""
chat.py — ask the dashboard a question, answered by your own Claude or OpenAI key.

The model never sees the database. It sees the same read-only tools the MCP
server offers (summary, spending_by_category, search_transactions, query, ...),
calls the ones it needs, and writes its answer from what they return. So the
numbers in a reply are the store's numbers, and nothing it does can change it.

What leaves the machine: the question, the conversation so far, and the result
of each tool the model calls — totals, merchant names, amounts. Statement PDFs
and the database file stay here. The key is used for the one request it came
with; the server never writes it anywhere.

The key comes from the page (the dashboard keeps it in the browser) or, if the
page sends none, from ANTHROPIC_API_KEY / OPENAI_API_KEY in the server's
environment. OPENAI_BASE_URL points the OpenAI side at any compatible server.

Standard library only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from finances.mcp_server import INSTRUCTIONS, TOOLS, Server

# The current models from each provider's own model list, newest family only:
#   https://platform.claude.com/docs/en/about-claude/models/overview
#   https://developers.openai.com/api/docs/models
# Checked 2026-11-07. The first entry is the default; any other model ID can
# still be typed in on the page.
MODELS_CHECKED = "2026-11-07"
PROVIDERS = {
    "anthropic": {"label": "Claude", "env": "ANTHROPIC_API_KEY", "models": [
        {"id": "claude-opus-5-5", "name": "Claude Opus 5.5"},
        {"id": "claude-fable-5-1", "name": "Claude Fable 5.1"},
        {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5"},
        {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5"},
    ]},
    "openai": {"label": "OpenAI", "env": "OPENAI_API_KEY", "models": [
        {"id": "gpt-6-astra", "name": "GPT-6 Astra"},
        {"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol"},
        {"id": "gpt-6.1-terra", "name": "GPT-6.1 Terra"},
        {"id": "gpt-6-luna", "name": "GPT-6 Luna"},
    ]},
}

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1"
MAX_ROUNDS = 8              # tool calls before it has to answer with what it has
MAX_TOOL_TEXT = 24_000      # characters of one tool result handed back to the model
MAX_TOKENS = 4096

SYSTEM = INSTRUCTIONS + (
    "\n\nYou are answering inside the user's finance dashboard. Today is {today}. "
    "Use the tools for every number you state; never estimate one you could look "
    "up. Answer in a few short sentences or a short bulleted list, leading with "
    "the answer. Plain text with **bold** and '- ' bullets only: no tables, no "
    "headings. If the data cannot answer the question, say what is missing.")


class ChatError(Exception):
    """Something the user can fix: no key, a bad key, an unknown model."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def status() -> dict:
    """What the page needs to draw the settings: providers, models, server keys."""
    return {"checked": MODELS_CHECKED,
            "providers": {name: {"label": p["label"], "models": p["models"],
                                 "serverKey": bool(os.environ.get(p["env"]))}
                          for name, p in PROVIDERS.items()}}


def _post(url: str, body: dict, headers: dict, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error") or {}
            detail = detail.get("message") if isinstance(detail, dict) else str(detail)
        except Exception:                                 # noqa: BLE001
            detail = ""
        status = 401 if e.code in (401, 403) else 502
        raise ChatError(f"{urllib.parse.urlparse(url).hostname} answered {e.code}"
                        + (f": {detail}" if detail else ""), status) from None
    except (urllib.error.URLError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise ChatError(f"could not reach {urllib.parse.urlparse(url).hostname}: {reason}",
                        502) from None


class Chat:
    """One question, answered by a model that can call the store's tools."""

    def __init__(self, db_path: str, post=_post):
        self.server = Server(db_path)
        self.post = post

    def run_tool(self, name: str, args: dict) -> tuple[str, bool]:
        out = self.server.call(name, args if isinstance(args, dict) else {})
        text = out["content"][0]["text"]
        if len(text) > MAX_TOOL_TEXT:
            text = text[:MAX_TOOL_TEXT] + "\n... (cut short; narrow the question)"
        return text, not out.get("isError")

    def ask(self, provider: str, model: str, messages: list[dict],
            key: str | None = None) -> dict:
        if provider not in PROVIDERS:
            raise ChatError(f"provider must be one of {', '.join(PROVIDERS)}")
        key = (key or "").strip() or os.environ.get(PROVIDERS[provider]["env"], "")
        if not key:
            raise ChatError(f"add your {PROVIDERS[provider]['label']} API key first")
        model = (model or "").strip() or PROVIDERS[provider]["models"][0]["id"]
        history = [{"role": m["role"], "content": str(m["content"])}
                   for m in messages or []
                   if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                   and str(m.get("content") or "").strip()]
        if not history or history[-1]["role"] != "user":
            raise ChatError("ask a question")
        system = SYSTEM.format(today=date.today().isoformat())
        if provider == "anthropic":
            run = self._anthropic
        else:
            run = self._openai_compatible if os.environ.get("OPENAI_BASE_URL") else self._openai
        answer, steps, usage = run(model, key, system, history)
        return {"answer": answer.strip(), "steps": steps, "provider": provider,
                "model": model, "usage": usage}

    # -- Anthropic Messages API -------------------------------------------
    def _anthropic(self, model, key, system, history):
        tools = [{"name": n, "description": d, "input_schema": s} for n, d, s in TOOLS]
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        messages, steps, usage = list(history), [], {"input": 0, "output": 0}
        for round_ in range(MAX_ROUNDS + 1):
            body = {"model": model, "max_tokens": MAX_TOKENS, "system": system,
                    "messages": messages}
            if round_ < MAX_ROUNDS:
                body["tools"] = tools
            out = self.post(ANTHROPIC_URL, body, headers)
            u = out.get("usage") or {}
            usage["input"] += u.get("input_tokens", 0)
            usage["output"] += u.get("output_tokens", 0)
            content = out.get("content") or []
            calls = [b for b in content if b.get("type") == "tool_use"]
            if not calls:
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                return text, steps, usage
            messages.append({"role": "assistant", "content": content})
            results = []
            for c in calls:
                text, ok = self.run_tool(c["name"], c.get("input") or {})
                steps.append({"tool": c["name"], "input": c.get("input") or {}, "ok": ok})
                results.append({"type": "tool_result", "tool_use_id": c["id"],
                                "content": text, "is_error": not ok})
            messages.append({"role": "user", "content": results})
        raise ChatError("the model kept calling tools without answering", 502)

    # -- OpenAI Responses API ---------------------------------------------
    # OpenAI's own models are called through the Responses API: it is the one
    # their model pages recommend for function calling, and some (GPT-6 Luna)
    # only call functions over Chat Completions with reasoning switched off.
    def _openai(self, model, key, system, history):
        tools = [{"type": "function", "name": n, "description": d, "parameters": s}
                 for n, d, s in TOOLS]
        headers = {"Authorization": f"Bearer {key}"}
        items, steps, usage = list(history), [], {"input": 0, "output": 0}
        for round_ in range(MAX_ROUNDS + 1):
            body = {"model": model, "instructions": system, "input": items,
                    "max_output_tokens": MAX_TOKENS,
                    # Not kept on OpenAI's side; reasoning comes back encrypted
                    # instead, so it can be handed back on the next round.
                    "store": False, "include": ["reasoning.encrypted_content"]}
            if round_ < MAX_ROUNDS:
                body["tools"] = tools
            out = self.post(OPENAI_URL + "/responses", body, headers)
            u = out.get("usage") or {}
            usage["input"] += u.get("input_tokens", 0)
            usage["output"] += u.get("output_tokens", 0)
            output = out.get("output")
            if not isinstance(output, list):
                raise ChatError(f"unexpected reply from OpenAI: {str(out)[:200]}", 502)
            calls = [o for o in output if o.get("type") == "function_call"]
            if not calls:
                text = "".join(c.get("text", "") for o in output if o.get("type") == "message"
                               for c in o.get("content") or []
                               if c.get("type") == "output_text")
                return text, steps, usage
            # Everything the model produced goes back, reasoning included, so the
            # next round continues the same chain of thought.
            items += output
            for c in calls:
                try:
                    args = json.loads(c.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                text, ok = self.run_tool(c.get("name", ""), args)
                steps.append({"tool": c.get("name", ""), "input": args, "ok": ok})
                items.append({"type": "function_call_output", "call_id": c["call_id"],
                              "output": text})
        raise ChatError("the model kept calling tools without answering", 502)

    # -- Chat Completions, for OPENAI_BASE_URL servers ----------------------
    # Most OpenAI-compatible servers (Ollama, LM Studio, vLLM, OpenRouter) speak
    # Chat Completions and not the Responses API.
    def _openai_compatible(self, model, key, system, history):
        base = os.environ["OPENAI_BASE_URL"].rstrip("/")
        tools = [{"type": "function", "function": {"name": n, "description": d,
                                                   "parameters": s}}
                 for n, d, s in TOOLS]
        headers = {"Authorization": f"Bearer {key}"}
        messages = [{"role": "system", "content": system}, *history]
        steps, usage = [], {"input": 0, "output": 0}
        for round_ in range(MAX_ROUNDS + 1):
            body = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS}
            if round_ < MAX_ROUNDS:
                body["tools"] = tools
            out = self.post(f"{base}/chat/completions", body, headers)
            u = out.get("usage") or {}
            usage["input"] += u.get("prompt_tokens", 0)
            usage["output"] += u.get("completion_tokens", 0)
            try:
                msg = out["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                raise ChatError(f"unexpected reply from {base}: {str(out)[:200]}", 502)
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or "", steps, usage
            messages.append({"role": "assistant", "content": msg.get("content"),
                             "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                text, ok = self.run_tool(fn.get("name", ""), args)
                steps.append({"tool": fn.get("name", ""), "input": args, "ok": ok})
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": text})
        raise ChatError("the model kept calling tools without answering", 502)

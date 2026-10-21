#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_client.py — 麦当劳 MCP 轻量客户端（Streamable HTTP）

只做三件事：找到 Token、握手、调用工具。
不依赖任何第三方库，python3.10+ 直接可跑。

Token 解析顺序：
  1. 环境变量 MCD_MCP_TOKEN
  2. 项目根目录 .env 中的 MCD_MCP_TOKEN
  3. ~/.workbuddy/mcp.json 中已配置的 mcd-mcp Authorization

⚠️ 本项目不会把 Token 写入任何文件、日志或仓库。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_token():
    tok = (os.environ.get("MCD_MCP_TOKEN") or os.environ.get("MCD_TOKEN") or "").strip()
    if tok:
        return tok

    env_path = os.path.join(ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("MCD_MCP_TOKEN="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v

    wb = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")
    if os.path.exists(wb):
        try:
            with open(wb, encoding="utf-8") as f:
                cfg = json.load(f)
            auth = (cfg.get("mcpServers", {}).get("mcd-mcp", {})
                    .get("headers", {}).get("Authorization", ""))
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
        except Exception:
            pass

    sys.exit(
        "未找到麦当劳 MCP Token。请任选一种方式配置：\n"
        "  set MCD_MCP_TOKEN=你的Token        (Windows)\n"
        "  export MCD_MCP_TOKEN=你的Token     (macOS/Linux)\n"
        "  或在项目根目录创建 .env 并填写 MCD_MCP_TOKEN=你的Token\n"
        "Token 申请入口：https://open.mcd.cn/mcp"
    )


class McdMCP:
    """极简 MCP 客户端：initialize 一次，之后反复 tools/call。"""

    def __init__(self, token=None, url=None, verbose=False):
        self.token = token or resolve_token()
        self.url = url or os.environ.get("MCD_MCP_URL", DEFAULT_URL)
        self.verbose = verbose
        self.session = None
        self._id = 0
        self._ready = False

    # ---------- 底层 ----------
    def _post(self, payload):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session = sid
                body = r.read().decode("utf-8", "replace")
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError("HTTP %s: %s" % (e.code, detail))

        if not body.strip():
            return None
        if "text/event-stream" in ctype:
            for line in body.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk:
                        return json.loads(chunk)
            return None
        return json.loads(body)

    def initialize(self):
        self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-golden-eye", "version": "1.0.0"},
            },
        })
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass  # 部分服务端不需要该通知
        self._ready = True
        if self.verbose:
            print("[mcp] initialized, session=%s" % (self.session or "-"), file=sys.stderr)

    # ---------- 业务 ----------
    def call(self, tool, arguments, _retry=True):
        if not self._ready:
            self.initialize()
        self._id += 1
        payload = {
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        try:
            resp = self._post(payload)
        except RuntimeError as e:
            if _retry and "HTTP 400" in str(e):  # 会话过期，重握手一次
                self.session = None
                self._ready = False
                self.initialize()
                return self.call(tool, arguments, _retry=False)
            raise
        if resp is None:
            return ""
        if "error" in resp:
            raise RuntimeError("MCP error: %s" % json.dumps(resp["error"], ensure_ascii=False))
        result = resp.get("result") or {}
        if result.get("isError"):
            raise RuntimeError("tool error: %s" % _content_text(result))
        return _content_text(result)

    def call_json(self, tool, arguments):
        """麦当劳 MCP 返回 text 内容，其中内嵌 JSON（可能带说明前缀），抽出后解析。"""
        text = self.call(tool, arguments)
        return extract_json(text)


def _content_text(result):
    parts = []
    for item in result.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts)


def extract_json(text):
    """从工具返回文本中抽取 JSON。

    麦当劳 MCP 的返回是「说明文字 + JSON」，且说明里也含 `{code}` 这类花括号占位符，
    因此不能简单取首尾花括号，必须用 raw_decode 逐个尝试并保留最大的一段合法 JSON。
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass

    decoder = json.JSONDecoder()
    best = None
    best_len = 0
    idx = text.find("{")
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(text, idx)
        except Exception:
            idx = text.find("{", idx + 1)
            continue
        size = len(text[idx:end])
        if size > best_len:
            best, best_len = obj, size
        idx = text.find("{", end)
    return best


def polite_sleep(seconds=0.35):
    """控制请求频率（官方限制：每 Token 每分钟 600 次）。"""
    time.sleep(seconds)

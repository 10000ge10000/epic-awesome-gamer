#!/usr/bin/env python3
"""SiliconFlow model benchmark for Epic Kiosk.

Reads API_KEY/API_BASE_URL from .env and tests:
- text JSON decision quality
- single-image vision JSON quality
- multi-image vision compatibility

The script never prints API keys.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
TEXT_CASES = [
    {
        "name": "free_checkout",
        "prompt": "You are on Epic checkout. Visible text: Price $0.00, button Place Order, user is logged in. Decide next action.",
        "expected": "click_order",
    },
    {
        "name": "paid_checkout",
        "prompt": "You are on Epic checkout. Visible text: Price $19.99, button Place Order, user is logged in. The automation must never buy paid products. Decide next action.",
        "expected": "abort",
    },
    {
        "name": "login_required",
        "prompt": "You are on Epic login page. Visible fields: email, password, sign in. Checkout is not available yet. Decide next action.",
        "expected": "wait",
    },
]

TEXT_PREFERRED = [
    "Qwen/Qwen3.6-35B-A3B",
    "Qwen/Qwen3.6-27B",
    "Qwen/Qwen3.5-122B-A10B",
    "Qwen/Qwen3.5-35B-A3B",
    "Qwen/Qwen3.5-30B-A3B",
    "Qwen/Qwen3.5-9B",
    "deepseek-ai/DeepSeek-V4-Flash",
    "deepseek-ai/DeepSeek-V4-Pro",
    "deepseek-ai/DeepSeek-V3.2",
    "Pro/deepseek-ai/DeepSeek-V3.2",
    "MiniMaxAI/MiniMax-M2.5",
    "Pro/MiniMaxAI/MiniMax-M2.5",
    "Pro/moonshotai/Kimi-K2.6",
    "Pro/zai-org/GLM-5.1",
]

VISION_PREFERRED = [
    "Qwen/Qwen3-VL-32B-Instruct",
    "Qwen/Qwen3-VL-30B-A3B-Instruct",
    "Qwen/Qwen3-VL-8B-Instruct",
    "Qwen/Qwen3-Omni-30B-A3B-Instruct",
    "zai-org/GLM-4.5V",
]


def read_env(path: str = ".env") -> dict[str, str]:
    env = dict(os.environ)
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env.setdefault(k.strip(), v.strip())
    return env


def request_json(method: str, url: str, key: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any, str]:
    data = None
    headers = {"Authorization": f"Bearer {key}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), raw
            except json.JSONDecodeError:
                return resp.status, None, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = None
        return e.code, body, raw
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def list_models(base_url: str, key: str, query: str = "") -> list[str]:
    url = base_url.rstrip("/") + "/models" + query
    status, body, raw = request_json("GET", url, key, timeout=30)
    if status != 200:
        raise RuntimeError(f"models request failed: status={status} body={raw[:300]}")
    data = body.get("data", body if isinstance(body, list) else [])
    ids = []
    for item in data:
        ids.append(item.get("id") if isinstance(item, dict) else str(item))
    return [x for x in ids if x]


def pick_existing(preferred: list[str], available: list[str], extra_rule=None, limit: int | None = None) -> list[str]:
    selected = []
    aset = set(available)
    for m in preferred:
        if m in aset and m not in selected:
            selected.append(m)
    if extra_rule:
        for m in available:
            if extra_rule(m) and m not in selected:
                selected.append(m)
    return selected[:limit] if limit else selected


def extract_json(text: str) -> Any:
    if text is None:
        return None
    s = text.strip()
    for candidate in [s]:
        try:
            return json.loads(candidate)
        except Exception:
            pass
    for pat in [r"```json\s*([\s\S]*?)```", r"```\s*([\s\S]*?)```", r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
        m = re.search(pat, s)
        if not m:
            continue
        blob = m.group(1) if m.lastindex else m.group(0)
        try:
            return json.loads(blob)
        except Exception:
            continue
    return None


def chat(base_url: str, key: str, model: str, messages: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
    payload = {"model": model, "messages": messages, "temperature": 0.0, "max_tokens": 512}
    t0 = time.perf_counter()
    status, body, raw = request_json("POST", base_url.rstrip("/") + "/chat/completions", key, payload, timeout=timeout)
    elapsed = time.perf_counter() - t0
    content = ""
    if isinstance(body, dict):
        content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return {"status": status, "elapsed_s": round(elapsed, 2), "body": body, "raw": raw, "content": content}


def png_data_url(rgb: tuple[int, int, int], size: int = 48) -> str:
    width = height = size
    r, g, b = rgb
    raw = b"".join(b"\x00" + bytes([r, g, b]) * width for _ in range(height))
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", zlib.crc32(tag + data) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


RED = png_data_url((220, 30, 30))
GREEN = png_data_url((40, 170, 60))
BLUE = png_data_url((40, 80, 220))


def test_text(base_url: str, key: str, model: str, timeout: float) -> dict[str, Any]:
    details = []
    passed = 0
    json_ok = 0
    latencies = []
    for case in TEXT_CASES:
        messages = [
            {"role": "system", "content": "You operate an Epic free-game automation. Rules: click_order only when price is exactly $0.00 and Place Order is visible; abort for any positive price or paid product; wait when login or page loading blocks checkout. Return only JSON: {\"action\": \"click_order|abort|wait\", \"reason\": \"short\"}."},
            {"role": "user", "content": case["prompt"]},
        ]
        res = chat(base_url, key, model, messages, timeout)
        latencies.append(res["elapsed_s"])
        parsed = extract_json(res["content"])
        action = parsed.get("action") if isinstance(parsed, dict) else None
        is_json = isinstance(parsed, dict)
        ok = res["status"] == 200 and action == case["expected"]
        passed += int(ok)
        json_ok += int(is_json)
        details.append({"case": case["name"], "status": res["status"], "elapsed_s": res["elapsed_s"], "json_ok": is_json, "action": action, "pass": ok, "error": res["raw"][:180] if res["status"] != 200 else None})
    avg = round(sum(latencies) / len(latencies), 2) if latencies else None
    score = passed * 40 + json_ok * 10 - (avg or timeout)
    return {"model": model, "pass": f"{passed}/{len(TEXT_CASES)}", "json": f"{json_ok}/{len(TEXT_CASES)}", "avg_latency_s": avg, "score": round(score, 2), "details": details}


def vision_messages(single: bool) -> list[dict[str, Any]]:
    if single:
        content = [
            {"type": "text", "text": "Identify the image. Return only JSON: {\"color\":\"red|green|blue\",\"shape\":\"square\"}."},
            {"type": "image_url", "image_url": {"url": RED}},
        ]
    else:
        content = [
            {"type": "text", "text": "There are three images in order. Return only JSON: {\"colors\":[\"red\",\"green\",\"blue\"],\"red_indexes\":[1]}. Use 1-based indexes."},
            {"type": "image_url", "image_url": {"url": RED}},
            {"type": "image_url", "image_url": {"url": GREEN}},
            {"type": "image_url", "image_url": {"url": BLUE}},
        ]
    return [{"role": "user", "content": content}]


def test_vision(base_url: str, key: str, model: str, timeout: float) -> dict[str, Any]:
    single = chat(base_url, key, model, vision_messages(True), timeout)
    multi = chat(base_url, key, model, vision_messages(False), timeout)
    single_json = extract_json(single["content"])
    multi_json = extract_json(multi["content"])
    single_pass = isinstance(single_json, dict) and single_json.get("color") == "red"
    colors = multi_json.get("colors") if isinstance(multi_json, dict) else None
    red_indexes = multi_json.get("red_indexes") if isinstance(multi_json, dict) else None
    multi_pass = isinstance(colors, list) and colors[:3] == ["red", "green", "blue"] and red_indexes == [1]
    avg = round((single["elapsed_s"] + multi["elapsed_s"]) / 2, 2)
    score = int(single_pass) * 40 + int(multi_pass) * 60 - avg
    return {
        "model": model,
        "single_pass": single_pass,
        "multi_pass": multi_pass,
        "avg_latency_s": avg,
        "score": round(score, 2),
        "single_status": single["status"],
        "multi_status": multi["status"],
        "single_parsed": single_json,
        "multi_parsed": multi_json,
        "single_error": single["raw"][:180] if single["status"] != 200 else None,
        "multi_error": multi["raw"][:180] if multi["status"] != 200 else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--text-limit", type=int, default=14)
    parser.add_argument("--vision-limit", type=int, default=8)
    parser.add_argument("--output", default="data/runtime/siliconflow_benchmark.jsonl")
    args = parser.parse_args()

    env = read_env()
    key = env.get("API_KEY") or env.get("SILICONFLOW_API_KEY")
    base_url = (env.get("API_BASE_URL") or env.get("SILICONFLOW_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    if not key:
        raise SystemExit("API_KEY is missing")

    all_models = list_models(base_url, key)
    chat_models = list_models(base_url, key, "?" + urllib.parse.urlencode({"type": "text", "sub_type": "chat"}))
    vision_like = [m for m in all_models if any(x in m.lower() for x in ["vl", "vision", "omni", "multimodal", "glm-4.5v"])]
    text_candidates = pick_existing(TEXT_PREFERRED, chat_models, limit=args.text_limit)
    vision_candidates = pick_existing(VISION_PREFERRED, vision_like, limit=args.vision_limit)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        inventory = {"event": "inventory", "base_url": base_url, "all_models": len(all_models), "chat_models": len(chat_models), "vision_like": vision_like, "text_candidates": text_candidates, "vision_candidates": vision_candidates}
        print("INVENTORY " + json.dumps(inventory, ensure_ascii=False))
        f.write(json.dumps(inventory, ensure_ascii=False) + "\n")

        text_results = []
        for model in text_candidates:
            res = test_text(base_url, key, model, args.timeout)
            text_results.append(res)
            print("TEXT_RESULT " + json.dumps(res, ensure_ascii=False))
            f.write(json.dumps({"event": "text", **res}, ensure_ascii=False) + "\n")

        vision_results = []
        for model in vision_candidates:
            res = test_vision(base_url, key, model, args.timeout)
            vision_results.append(res)
            print("VISION_RESULT " + json.dumps(res, ensure_ascii=False))
            f.write(json.dumps({"event": "vision", **res}, ensure_ascii=False) + "\n")

        text_top = sorted(text_results, key=lambda x: x["score"], reverse=True)
        vision_top = sorted(vision_results, key=lambda x: x["score"], reverse=True)
        print("SUMMARY_TEXT_TOP " + json.dumps(text_top[:8], ensure_ascii=False))
        print("SUMMARY_VISION_TOP " + json.dumps(vision_top[:8], ensure_ascii=False))
        f.write(json.dumps({"event": "summary", "text_top": text_top[:8], "vision_top": vision_top[:8]}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

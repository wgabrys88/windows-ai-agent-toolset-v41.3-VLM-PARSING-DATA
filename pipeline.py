from __future__ import annotations

import http.client
import json
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final


@dataclass
class PipelineResult:
    ghosts: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    heat: list[dict[str, Any]] = field(default_factory=list)
    next_turn: str = ""
    raw_display: dict[str, Any] = field(default_factory=dict)


_HERE: Final[Path] = Path(__file__).resolve().parent
_CFG: Final[dict[str, Any]] = json.loads((_HERE / "config.json").read_text("utf-8")) if (_HERE / "config.json").exists() else {}

_SUM_SYS: Final[str] = (
    "Rewrite into ONE clean observation for the next turn. Keep key facts, goals, actions, results, lessons. "
    "Plain text only: no JSON, no markdown, no code fences."
)
_REG_SYS: Final[str] = "Extract UI regions. Return ONLY JSON: a list of {bbox_2d:[x1,y1,x2,y2], label:str}. No markdown."
_ACT_SYS: Final[str] = "Extract UI actions. Return ONLY JSON: a list of {type:str, bbox_2d:[x1,y1,x2,y2], params:str}. No markdown."


def _cfg(name: str, default: Any = None) -> Any:
    return _CFG.get(name, default)


def _unfence(s: str) -> str:
    t: str = s.strip()
    return t.split("\n", 1)[1].rsplit("```", 1)[0].strip() if t.startswith("```") and "\n" in t and "```" in t[3:] else t


def _call(sys_prompt: str, text: str, *, max_tokens: int) -> str:
    url: str = str(_cfg("api_url", ""))
    if not url or not text.strip():
        return text
    u: urllib.parse.ParseResult = urllib.parse.urlparse(url)
    host: str = u.hostname or "127.0.0.1"
    port: int = u.port or 80
    path: str = u.path or "/v1/chat/completions"
    body: bytes = json.dumps({
        "model": str(_cfg("model", "")),
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text},
        ],
    }).encode("utf-8")
    try:
        c: http.client.HTTPConnection = http.client.HTTPConnection(host, port, timeout=5)
        c.request("POST", path, body=body, headers={"Content-Type": "application/json", "Connection": "close"})
        r: http.client.HTTPResponse = c.getresponse()
        data: bytes = r.read()
        c.close()
        if not 200 <= r.status < 300:
            return text
        obj: Any = json.loads(data.decode("utf-8", "replace"))
        out: str = str(obj.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        return out or text
    except Exception:
        return text


def _summarize(text: str) -> str:
    return _call(_SUM_SYS, text, max_tokens=220)


def _extract_regions(text: str) -> list[dict[str, Any]]:
    out: str = _call(_REG_SYS, text, max_tokens=500)
    try:
        obj: Any = json.loads(_unfence(out))
    except Exception:
        return []
    if isinstance(obj, dict):
        obj = obj.get("regions", [])
    return _parse_regions(obj if isinstance(obj, list) else [])


def _extract_actions(text: str) -> list[dict[str, Any]]:
    out: str = _call(_ACT_SYS, text, max_tokens=600)
    try:
        obj: Any = json.loads(_unfence(out))
    except Exception:
        return []
    if isinstance(obj, dict):
        obj = obj.get("actions", [])
    return _parse_actions(obj if isinstance(obj, list) else [])


def _clamp(v: Any) -> int:
    try:
        n: int = int(float(v))
    except (ValueError, TypeError):
        return 0
    return max(0, min(1000, n))


def _parse_regions(raw: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        coords: Any = r.get("bbox_2d")
        if not isinstance(coords, list) or len(coords) != 4:
            continue
        out.append({
            "bbox_2d": [_clamp(coords[0]), _clamp(coords[1]), _clamp(coords[2]), _clamp(coords[3])],
            "label": str(r.get("label", "")),
        })
    return out


def _parse_actions(raw: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        coords: Any = a.get("bbox_2d")
        if not isinstance(coords, list) or len(coords) != 4:
            continue
        action_type: str = str(a.get("type", "")).strip().lower()
        if not action_type:
            continue
        out.append({
            "type": action_type,
            "bbox_2d": [_clamp(coords[0]), _clamp(coords[1]), _clamp(coords[2]), _clamp(coords[3])],
            "params": str(a.get("params", "")),
        })
    return out


def _build_heat(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    heat: list[dict[str, Any]] = []
    drag_start: list[int] | None = None
    for a in actions:
        entry: dict[str, Any] = {"type": a["type"], "bbox_2d": list(a["bbox_2d"])}
        if a["type"] == "drag_start":
            c: list[int] = a["bbox_2d"]
            drag_start = [(c[0] + c[2]) // 2, (c[1] + c[3]) // 2]
        elif a["type"] == "drag_end" and drag_start is not None:
            entry["drag_start"] = drag_start
            drag_start = None
        heat.append(entry)
    return heat


def _build_display(obs: str, regions: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"observation": obs, "regions": regions, "actions": actions}


def process(raw: str) -> PipelineResult:
    raw = raw.strip()
    if not raw:
        return PipelineResult(next_turn="(no prior observation)")

    obs: str = _summarize(raw)
    regions: list[dict[str, Any]] = _extract_regions(raw)
    actions: list[dict[str, Any]] = _extract_actions(raw)
    heat: list[dict[str, Any]] = _build_heat(actions)
    display: dict[str, Any] = _build_display(obs, regions, actions)

    return PipelineResult(ghosts=regions, actions=actions, heat=heat, next_turn=obs, raw_display=display)


def to_json(result: PipelineResult) -> str:
    return json.dumps({
        "ghosts": result.ghosts,
        "actions": result.actions,
        "heat": result.heat,
        "next_turn": result.next_turn,
        "raw_display": result.raw_display,
    }, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        input_text: str = open(sys.argv[1], encoding="utf-8").read()
    else:
        input_text = sys.stdin.read()
    result: PipelineResult = process(input_text)
    print(to_json(result))
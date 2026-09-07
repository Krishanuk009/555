#!/usr/bin/env python3
"""
Unified IPTV playlist updater.

This is a clean reimplementation of the provider/update behavior observed in
the reference repository. It keeps one updater and one GitHub Actions job,
while supporting direct M3U sources, JSON->M3U conversion, filtered/derived
playlists, and Star Sports JSON outputs.

Safety/robustness:
- provider failures are isolated
- invalid/empty responses never overwrite a previous good file
- suspicious channel-count drops can be rejected
- retries + shared HTTP session
- atomic writes
- derived outputs use cached upstream downloads to avoid duplicate requests
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

ROOT = Path(__file__).resolve().parents[1]
PLAYLIST_DIR = ROOT / "playlists"
DATA_DIR = ROOT / "data"
PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT = (15, 45)
RETRIES = 3
RETRY_BACKOFF = 2
DROP_THRESHOLD = 0.50

COMMON_HEADERS = {
    "User-Agent": "OTT Navigator",
    "Accept": "*/*",
}

JIO_HEADERS = {
    "Origin": "https://www.jiotv.com/",
    "Referer": "https://www.jiotv.com/",
}

log = logging.getLogger("iptv-updater")


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


SOURCES = {
    "streamstar": Source("StreamStar JioTV", "https://mute-sunset-8225.streamstar18.workers.dev/"),
    "jtv2_json": Source("JTV JSON", "https://m3u-86e.pages.dev/jtv-mb.json"),
    "sixpg": Source("SixPG JioTV", "https://raw.githubusercontent.com/sixpg/zeyo-test/refs/heads/main/jtv.m3u"),
    "alex": Source("Alex JioTV", "https://raw.githubusercontent.com/alex4528y/m3u/refs/heads/main/jtv.m3u"),
    "ayush": Source("Ayush Worker JioTV", "https://t.ayush848694.workers.dev/"),
    "stbplus": Source("STBPLUS JioTV", "https://raw.githubusercontent.com/Sflex0719/STBPLUS/refs/heads/main/Zio.m3u"),
    "stbplus_mobile": Source("STBPLUS Mobile", "https://raw.githubusercontent.com/Sflex0719/STBPLUS/refs/heads/main/ZioMobile.m3u"),
    "jtvplus2": Source("AllInOne Reborn", "https://allinonereborn2.online/m3u/jtv223.m3u"),
    "geoplus": Source("GeoPlus", "https://raw.githubusercontent.com/qwerty180506/json/refs/heads/main/Geoplus.json"),
    "biscuit": Source("GeoPlus Cookie", "https://raw.githubusercontent.com/qwerty180506/json/refs/heads/main/biscuit.json"),
    "sportsbiscuit": Source("GeoPlus Sports Cookie", "https://raw.githubusercontent.com/qwerty180506/json/refs/heads/main/sportsbiscuit.json"),
    "alex_plus": Source("Alex Plus", "https://alex4528.site/jplus/playlist.m3u"),
    "premium_vip": Source("PremiumPlugX VIP", "https://premiumplugx.com/VIP/pluglist.php", {"User-Agent": "OTT Navigator"}),
    "premium_hot": Source("PremiumPlugX Hot", "https://premiumplugx.com/htt/hot.php?playlist=1", {"User-Agent": "OTT Navigator"}),
    "fusion": Source("Fusion/DRMLive", "https://la.drmlive.net/tp/playlist", {"User-Agent": "OTT Navigator"}),
    "pocket": Source("Pocket TV", "https://tiny.cc/Pocket-TV", {"User-Agent": "OTT Navigator"}),
    "sports_json": Source("Star Sports JSON", "https://sonujson-v3.pages.dev/Data/sports.json"),
    "sayan_star": Source("Sayan Star Sports", "https://sayan-jio-tv.pages.dev/playlist.m3u"),
}


def fetch_text(source: Source) -> str:
    headers = {**COMMON_HEADERS, **source.headers}
    last: Exception | None = None
    with SESSION_LOCK:
        pass
    for attempt in range(1, RETRIES + 1):
        try:
            response = SESSION.get(source.url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            if not response.content:
                raise ValueError("empty HTTP response")
            text = response.content.decode("utf-8-sig", errors="replace")
            if not text.strip():
                raise ValueError("empty decoded response")
            return text
        except Exception as exc:
            last = exc
            log.warning("%s: attempt %d/%d failed: %s", source.name, attempt, RETRIES, exc)
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF ** (attempt - 1))
    raise RuntimeError(f"{source.name}: failed after {RETRIES} attempts: {last}")


SESSION = requests.Session()
SESSION.max_redirects = 5
SESSION_LOCK = type("NoopLock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *a: False})()


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def escape_attr(value: Any) -> str:
    return clean(value).replace("\\", "\\\\").replace('"', "'").replace("\r", " ").replace("\n", " ")


def parse_extinf(line: str) -> dict[str, str]:
    attrs = {}
    for key, value in re.findall(r'([\w-]+)="([^"]*)"', line):
        attrs[key] = value
    display = line.rsplit(",", 1)[1].strip() if "," in line else ""
    attrs["display-name"] = display
    return attrs


def parse_m3u_blocks(content: str) -> list[list[str]]:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            if current:
                blocks.append(current)
            current = [line]
            continue
        if current is None:
            continue
        if not line:
            continue
        current.append(line)
        if not line.startswith("#"):
            blocks.append(current)
            current = None
    if current and any(not x.startswith("#") for x in current[1:]):
        blocks.append(current)
    return blocks


def validate_m3u(content: str, minimum: int = 1) -> int:
    blocks = parse_m3u_blocks(content)
    if len(blocks) < minimum:
        raise ValueError(f"only {len(blocks)} valid channel(s)")
    for i, block in enumerate(blocks, 1):
        if not block[0].startswith("#EXTINF:"):
            raise ValueError(f"entry {i} has no EXTINF")
        if not any(x.strip() and not x.startswith("#") for x in block[1:]):
            raise ValueError(f"entry {i} has no stream URL")
    return len(blocks)


def normalize_m3u(content: str, minimum: int = 1) -> str:
    validate_m3u(content, minimum)
    blocks = parse_m3u_blocks(content)
    return "#EXTM3U\n\n" + "\n\n".join("\n".join(b) for b in blocks) + "\n"


def get_json(source: Source) -> Any:
    raw = fetch_text(source)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc


def json_channels(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("channels")
        if not isinstance(items, list):
            items = data.get("data")
    else:
        items = None
    if not isinstance(items, list):
        raise ValueError("JSON does not contain a channel list")
    result = [x for x in items if isinstance(x, dict)]
    if not result:
        raise ValueError("JSON contains no channel objects")
    return result


def build_channel_m3u(
    channels: Iterable[dict[str, Any]],
    *,
    default_group: str = "Other",
    default_ua: str = "",
    origin: str = "",
    referer: str = "",
) -> str:
    out = ["#EXTM3U"]
    written = 0
    for ch in channels:
        name = clean(ch.get("name") or ch.get("channel_name") or ch.get("title")) or "Unknown"
        cid = clean(ch.get("id") or ch.get("channel_id") or ch.get("channelId"))
        logo = clean(ch.get("logo") or ch.get("tvg_logo") or ch.get("image") or ch.get("image_url"))
        group = clean(ch.get("group") or ch.get("category") or ch.get("group-title") or ch.get("group_title")) or default_group
        url = clean(ch.get("stream_url") or ch.get("streamUrl") or ch.get("mpd_url") or ch.get("mpd") or ch.get("url") or ch.get("stream") or ch.get("playback_url"))
        if not url:
            continue

        out.append(
            f'#EXTINF:-1 tvg-id="{escape_attr(cid)}" tvg-name="{escape_attr(name)}" '
            f'tvg-logo="{escape_attr(logo)}" group-title="{escape_attr(group)}",{name}'
        )

        is_mpd = clean(ch.get("type")).lower() == "dash" or ".mpd" in url.lower()
        if is_mpd:
            out.append("#KODIPROP:inputstream=inputstream.adaptive")
            out.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")

        key_id = clean(ch.get("key_id") or ch.get("keyId") or ch.get("kid"))
        key = clean(ch.get("key") or ch.get("key_value"))
        if not (key_id and key) and isinstance(ch.get("clearkey"), dict) and ch["clearkey"]:
            key_id, key = next(iter(ch["clearkey"].items()))
        license_url = clean(ch.get("license_url"))
        if key_id and key:
            out.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
            out.append(f"#KODIPROP:inputstream.adaptive.license_key={key_id}:{key}")
        elif license_url:
            out.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
            out.append(f"#KODIPROP:inputstream.adaptive.license_key={license_url}")

        ua = clean(ch.get("user_agent") or default_ua)
        if ua:
            out.append(f"#EXTVLCOPT:http-user-agent={ua}")

        headers: dict[str, str] = {}
        cookie = clean(ch.get("cookie") or ch.get("cookies"))
        if cookie:
            headers["cookie"] = cookie
        source_headers = ch.get("headers")
        if isinstance(source_headers, dict):
            headers.update({clean(k): clean(v) for k, v in source_headers.items() if clean(v)})
        if origin:
            headers.setdefault("Origin", origin)
        if referer:
            headers.setdefault("Referer", referer)
        if headers:
            out.append("#EXTHTTP:" + json.dumps(headers, ensure_ascii=False, separators=(",", ":")))

        out.append(url)
        out.append("")
        written += 1

    if written < 1:
        raise ValueError("conversion produced no usable channels")
    return "\n".join(out).rstrip() + "\n"


def jtv2_convert(data: Any) -> str:
    channels = json_channels(data)
    out = ["#EXTM3U", f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}", ""]
    written = 0
    for ch in channels:
        name = clean(ch.get("name")) or "Unknown"
        cid = clean(ch.get("id"))
        logo = clean(ch.get("logo"))
        group = clean(ch.get("group")) or "Other"
        stream = clean(ch.get("mpd_url"))
        if not stream:
            continue
        out.append(f'#EXTINF:-1 tvg-id="{escape_attr(cid)}" tvg-name="{escape_attr(name)}" tvg-logo="{escape_attr(logo)}" group-title="{escape_attr(group)}",{name}')
        if clean(ch.get("type")).lower() == "dash":
            out.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")
        license_url = clean(ch.get("license_url"))
        if license_url:
            out.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
            out.append(f"#KODIPROP:inputstream.adaptive.license_key={license_url}")
        ua = clean(ch.get("user_agent"))
        if ua:
            out.append(f"#EXTVLCOPT:http-user-agent={ua}")
        if isinstance(ch.get("headers"), dict) and ch["headers"]:
            out.append("#EXTHTTP:" + json.dumps(ch["headers"], ensure_ascii=False, separators=(",", ":")))
        out.extend([stream, ""])
        written += 1
    if not written:
        raise ValueError("JTV JSON produced no usable channels")
    return "\n".join(out)


def geoplus_convert(data: Any) -> str:
    return build_channel_m3u(json_channels(data), default_group="Sports", default_ua="IndTv", origin=JIO_HEADERS["Origin"], referer=JIO_HEADERS["Referer"])


def parse_cached_channel_objects(content: str) -> list[dict[str, Any]]:
    objects = []
    for block in parse_m3u_blocks(content):
        ext = parse_extinf(block[0])
        obj = {
            "id": ext.get("tvg-id", ""),
            "name": ext.get("tvg-name") or ext.get("display-name") or "Unknown",
            "logo": ext.get("tvg-logo", ""),
            "group": ext.get("group-title", "Other"),
            "url": "",
            "license_key": "",
            "user_agent": "",
            "headers": {},
        }
        for line in block[1:]:
            if line.startswith("#KODIPROP:inputstream.adaptive.license_key="):
                obj["license_key"] = line.split("=", 1)[1].strip()
            elif line.startswith("#EXTVLCOPT:http-user-agent="):
                obj["user_agent"] = line.split("=", 1)[1].strip()
            elif line.startswith("#EXTHTTP:"):
                try:
                    h = json.loads(line.split(":", 1)[1])
                    if isinstance(h, dict):
                        obj["headers"] = h
                except Exception:
                    pass
            elif not line.startswith("#") and not obj["url"]:
                obj["url"] = line.strip()
        if obj["url"]:
            objects.append(obj)
    return objects


def convert_kodi_blocks(content: str, user_agent: str) -> str:
    blocks = parse_m3u_blocks(content)
    out = ["#EXTM3U", ""]
    written = 0
    for block in blocks:
        ext = parse_extinf(block[0])
        props = [x for x in block[1:] if x.startswith("#")]
        url = next((x for x in block[1:] if not x.startswith("#")), "")
        if not url:
            continue

        # Reference converters separate Cookie/User-Agent from the URL.
        base = url.split("|", 1)[0]
        params = []
        if "?" in base:
            parsed = urlparse(base)
            kept = []
            for k, v in parse_qsl(parsed.query, keep_blank_values=True):
                if k.lower() not in {"user-agent", "cookie"}:
                    kept.append((k, v))
            base = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(kept), parsed.fragment))

        out.append(block[0])
        has_adaptive = any("inputstream=inputstream.adaptive" in p for p in props)
        if not has_adaptive:
            out.append("#KODIPROP:inputstream=inputstream.adaptive")
        if any(p.startswith("#KODIPROP:inputstream.adaptive.manifest_type=") for p in props):
            out.extend(p for p in props if p.startswith("#KODIPROP:inputstream.adaptive.manifest_type="))
        else:
            out.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")
        for p in props:
            if p.startswith("#KODIPROP:inputstream.adaptive.license_"):
                out.append(p)
        out.append(f"#EXTVLCOPT:http-user-agent={user_agent}")

        headers: dict[str, str] = {}
        for p in props:
            if p.startswith("#EXTHTTP:"):
                try:
                    h = json.loads(p.split(":", 1)[1])
                    if isinstance(h, dict):
                        headers.update(h)
                except Exception:
                    pass
        # Also accept Cookie/User-Agent embedded in URL.
        parsed_url = urlparse(url)
        q = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
        if "Cookie" in q and "cookie" not in headers:
            headers["cookie"] = q["Cookie"]
        if "cookie" in q and "cookie" not in headers:
            headers["cookie"] = q["cookie"]
        if headers:
            out.append("#EXTHTTP:" + json.dumps(headers, ensure_ascii=False, separators=(",", ":")))
        out.extend([base, ""])
        written += 1
    if not written:
        raise ValueError("source contains no usable M3U entries")
    return "\n".join(out)


def filter_star_sports(content: str, user_agent: str = "Virat") -> str:
    out = ["#EXTM3U"]
    for block in parse_m3u_blocks(content):
        name = parse_extinf(block[0]).get("display-name", "").upper()
        if "STAR SPORTS" not in name or "DIGITAL" in name:
            continue
        url = next((x for x in block[1:] if not x.startswith("#")), "")
        if not url:
            continue
        token_match = re.search(r"__hdnea__=([^&|]+)", url)
        if not token_match:
            out.extend(block)
            out.append("")
            continue
        token = token_match.group(1)
        base = re.sub(r"\?.*$", "", url)
        props = [x for x in block[1:] if x.startswith("#")]
        out.append(block[0])
        if not any("inputstream=inputstream.adaptive" in p for p in props):
            out.append("#KODIPROP:inputstream=inputstream.adaptive")
        for p in props:
            if p.startswith("#KODIPROP:inputstream.adaptive.") and "inputstream=inputstream.adaptive" not in p:
                out.append(p)
        out.append(f"#EXTVLCOPT:http-user-agent={user_agent}")
        out.append("#EXTHTTP:" + json.dumps({"cookie": f"__hdnea__={token}", **JIO_HEADERS}, ensure_ascii=False, separators=(",", ":")))
        out.extend([base, ""])
    result = "\n".join(out)
    if validate_m3u(result, 1) < 1:
        raise ValueError("no Star Sports channels matched")
    return result


def filter_digital(content: str) -> str:
    out = ["#EXTM3U"]
    for block in parse_m3u_blocks(content):
        name = parse_extinf(block[0]).get("display-name", "")
        if "Digital" not in name:
            continue
        out.extend(block)
        out.append("")
    result = "\n".join(out)
    if validate_m3u(result, 1) < 1:
        raise ValueError("no Digital channels matched")
    return result


def hotstar_rebrand(content: str) -> str:
    return content.replace("@Premiumplugx", "@sayan10")


def star_json_from_m3u(content: str) -> list[dict[str, Any]]:
    allowed = ("jiotvpllive.cdn.jio.com", "jiotvmblive.cdn.jio.com")
    result = []
    for block in parse_m3u_blocks(content):
        ext = parse_extinf(block[0])
        name = ext.get("display-name") or ext.get("tvg-name") or ""
        if "star sports" not in name.lower() or "digital" in name.lower():
            continue
        url = next((x for x in block[1:] if not x.startswith("#")), "")
        if not any(d in url for d in allowed):
            continue
        token_match = re.search(r"__hdnea__=([^&]+)", url)
        cookie = token_match.group(1) if token_match else ""
        base = re.sub(r"\?.*$", "", url)
        key_id = key = None
        for line in block[1:]:
            if line.startswith("#KODIPROP:inputstream.adaptive.license_key="):
                val = line.split("=", 1)[1]
                if ":" in val:
                    key_id, key = val.split(":", 1)
        result.append({
            "id": ext.get("tvg-id"),
            "name": name,
            "stream_url": base,
            "cookie": cookie,
            "cookie_expires": cookie_expiry(cookie),
            "key_id": key_id,
            "key": key,
            "logo": ext.get("tvg-logo", ""),
        })
    if not result:
        raise ValueError("no JioTV Star Sports channels matched")
    return result


def cookie_expiry(cookie: str) -> str:
    m = re.search(r"exp=(\d+)", cookie or "")
    if not m:
        return ""
    try:
        dt = datetime.fromtimestamp(int(m.group(1)), tz=timezone(timedelta(hours=5, minutes=30)))
    except (ValueError, OSError, OverflowError):
        return ""
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{dt.day}/{dt.month}/{dt.year} {hour}:{dt.minute:02d}:{dt.second:02d} {suffix} IST"


def star_sports_json(data: Any) -> tuple[str, str]:
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        raise ValueError("sports JSON must contain channels")
    channels = []
    for ch in data["channels"]:
        if not isinstance(ch, dict):
            continue
        stream = clean(ch.get("stream_url"))
        cookie = clean(ch.get("cookie"))
        kid = clean(ch.get("key_id"))
        key = clean(ch.get("key"))
        if not all([stream, cookie, kid, key]):
            continue
        channels.append(ch)
    if not channels:
        raise ValueError("sports JSON produced no complete channels")

    out = ["#EXTM3U"]
    json_rows = []
    for ch in channels:
        cid = clean(ch.get("id"))
        name = clean(ch.get("name")) or "Unknown"
        stream = clean(ch.get("stream_url"))
        cookie = clean(ch.get("cookie"))
        kid = clean(ch.get("key_id"))
        key = clean(ch.get("key"))
        out.append(f'#EXTINF:-1 tvg-id="{escape_attr(cid)}" tvg-name="{escape_attr(name)}" tvg-logo="" group-title="Sports",{name}')
        out.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")
        out.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
        out.append(f"#KODIPROP:inputstream.adaptive.license_key={kid}:{key}")
        out.append("#EXTVLCOPT:http-user-agent=Sayan10")
        out.append("#EXTHTTP:" + json.dumps({
            "cookie": cookie,
            "Origin": JIO_HEADERS["Origin"],
            "Referer": JIO_HEADERS["Referer"],
        }, ensure_ascii=False, separators=(",", ":")))
        out.extend([stream, ""])
        json_rows.append({
            "id": cid,
            "name": name,
            "stream_url": stream,
            "cookie": cookie,
            "key_id": kid,
            "key": key,
        })
    return "\n".join(out).rstrip() + "\n", json.dumps(json_rows, indent=2, ensure_ascii=False) + "\n"


def geoplus3(data: Any, biscuit: Any, sports_biscuit: Any) -> str:
    channels = json_channels(data)
    normal_cookie = ""
    if isinstance(biscuit, str):
        normal_cookie = biscuit
    elif isinstance(biscuit, list):
        normal_cookie = next((clean(x.get("cookie")) for x in biscuit if isinstance(x, dict) and x.get("cookie")), "")
    elif isinstance(biscuit, dict):
        normal_cookie = clean(biscuit.get("cookie"))

    sports_urls: dict[str, str] = {}
    if isinstance(sports_biscuit, dict):
        results = sports_biscuit.get("successful_results", []) + sports_biscuit.get("failed_results", [])
    else:
        results = []
    for item in results:
        if not isinstance(item, dict):
            continue
        cid = clean(item.get("channel_id"))
        final = clean(item.get("final_url")) or clean((item.get("error_details") or {}).get("final_url"))
        if cid and final:
            sports_urls[cid] = final.replace("/output/", "/WDVLive/")

    out = ["#EXTM3U", ""]
    written = 0
    for ch in channels:
        cid = clean(ch.get("id"))
        name = clean(ch.get("name")) or "Unknown"
        logo = clean(ch.get("logo"))
        group = clean(ch.get("group") or ch.get("category")) or "Other"
        url = clean(ch.get("url"))
        if not url:
            continue
        is_mpd = clean(ch.get("type")).lower() == "dash" or ".mpd" in url.lower()
        out.append(f'#EXTINF:-1 tvg-id="{escape_attr(cid)}" tvg-name="{escape_attr(name)}" tvg-logo="{escape_attr(logo)}" group-title="{escape_attr(group)}",{name}')
        if is_mpd:
            out.append("#KODIPROP:inputstream=inputstream.adaptive")
            out.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")
            if clean(ch.get("keyId")) and clean(ch.get("key")):
                out.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
                out.append(f"#KODIPROP:inputstream.adaptive.license_key={clean(ch.get('keyId'))}:{clean(ch.get('key'))}")
            elif isinstance(ch.get("clearkey"), dict) and ch["clearkey"]:
                kid, key = next(iter(ch["clearkey"].items()))
                out.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
                out.append(f"#KODIPROP:inputstream.adaptive.license_key={kid}:{key}")
            elif clean(ch.get("license_url")):
                out.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
                out.append(f"#KODIPROP:inputstream.adaptive.license_key={clean(ch.get('license_url'))}")
        final = sports_urls.get(cid)
        if not final and normal_cookie:
            sep = "&" if "?" in url else "?"
            final = url + sep + normal_cookie
        final = final or url
        parsed = urlparse(final)
        query = parsed.query
        base = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", parsed.fragment))
        if query:
            out.append("#EXTHTTP:" + json.dumps({"cookie": query}, ensure_ascii=False, separators=(",", ":")))
        out.append("#EXTVLCOPT:http-user-agent=Sayan10")
        out.extend([base, ""])
        written += 1
    if not written:
        raise ValueError("GeoPlus produced no channels")
    return "\n".join(out)


def atomic_write(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except Exception:
            pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).replace(path)
        return True
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def safe_write_m3u(name: str, content: str, *, previous: bool = True) -> tuple[bool, int]:
    count = validate_m3u(content, 1)
    path = PLAYLIST_DIR / name
    if previous and path.exists():
        old_count = 0
        try:
            old_count = validate_m3u(path.read_text(encoding="utf-8"), 1)
        except Exception:
            old_count = 0
        if old_count and count < old_count * (1 - DROP_THRESHOLD):
            raise ValueError(f"suspicious channel-count drop: {old_count} -> {count}")
    changed = atomic_write(path, content)
    return changed, count


def safe_write_json(relative: str, data: Any) -> bool:
    path = DATA_DIR / relative
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return atomic_write(path, content)


def run() -> int:
    results: list[tuple[str, str, int]] = []
    cache: dict[str, Any] = {}

    def get(name: str) -> Any:
        if name not in cache:
            src = SOURCES[name]
            cache[name] = fetch_text(src)
        return cache[name]

    def do_m3u(label: str, filename: str, producer: Callable[[], str]) -> None:
        try:
            content = producer()
            changed, count = safe_write_m3u(filename, content)
            results.append((label, "OK" if changed else "UNCHANGED", count))
            log.info("%s -> %s (%d channels)", label, filename, count)
        except Exception as exc:
            results.append((label, "FAILED/UNCHANGED", 0))
            log.error("%s failed: %s", label, exc)

    # Direct source outputs.
    do_m3u("StreamStar JioTV", "jtv.m3u", lambda: normalize_m3u(get("streamstar")))
    do_m3u("JTV JSON", "jtv2.m3u", lambda: jtv2_convert(json.loads(get("jtv2_json"))))
    do_m3u("SixPG JioTV", "jtv3.m3u", lambda: convert_kodi_blocks(get("sixpg"), "Droovy"))
    do_m3u("Alex JioTV", "jtv4.m3u", lambda: normalize_m3u(get("alex")))
    do_m3u("Ayush Worker JioTV", "jtv5.m3u", lambda: normalize_m3u(get("ayush")))
    do_m3u("STBPLUS JioTV", "jtvplus.m3u", lambda: normalize_m3u(get("stbplus")))
    do_m3u("AllInOne Reborn", "jtvplus2.m3u", lambda: normalize_m3u(get("jtvplus2")))
    do_m3u("GeoPlus", "jtvplus3.m3u", lambda: geoplus3(json.loads(get("geoplus")), json.loads(get("biscuit")), json.loads(get("sportsbiscuit"))))
    do_m3u("Alex Plus", "jtvplus4.m3u", lambda: normalize_m3u(get("alex_plus")))
    do_m3u("PremiumPlugX VIP", "jtvplus5.m3u", lambda: normalize_m3u(get("premium_vip")))
    do_m3u("Geo CF", "jtvplus6.m3u", lambda: convert_kodi_blocks(fetch_text(Source("Geo CF", "https://raw.githubusercontent.com/qwerty180506/Geo/refs/heads/main/jiotv_cf.m3u")), "Sayan"))
    do_m3u("SixPG Kodi", "jtvplus7.m3u", lambda: convert_kodi_blocks(get("sixpg"), "Droovy"))
    do_m3u("STBPLUS Mobile", "jtvplus8.m3u", lambda: normalize_m3u(get("stbplus_mobile")))
    do_m3u("MIXIPTV", "mixiptv.m3u", lambda: normalize_m3u(get("premium_vip")))
    do_m3u("Fusion", "playlist.m3u", lambda: normalize_m3u(get("fusion")))
    do_m3u("Pocket TV", "pocket.m3u", lambda: normalize_m3u(get("pocket")))

    # Sports and derivative outputs.
    def sports_m3u() -> str:
        data = json.loads(get("sports_json"))
        m3u, _ = star_sports_json(data)
        channels = data.get("channels", []) if isinstance(data, dict) else []
        if channels and isinstance(channels[0], dict) and clean(channels[0].get("cookie")):
            safe_write_json("cookie.json", {"cookie": clean(channels[0].get("cookie"))})
        return m3u
    do_m3u("Star Sports", "Star.m3u", sports_m3u)
    do_m3u("Star Sports from SixPG", "Star2.m3u", lambda: filter_star_sports(get("sixpg"), "Virat"))
    do_m3u("Star Sports from PremiumPlugX", "Star3.m3u", lambda: filter_star_sports(get("premium_vip"), "Virat"))
    do_m3u("Hotstar", "hotstar.m3u", lambda: hotstar_rebrand(get("premium_hot")))
    do_m3u("Digital", "digital.m3u", lambda: filter_digital(get("premium_hot")))

    def write_star_json_from_mix() -> str:
        objs = star_json_from_m3u(get("premium_vip"))
        safe_write_json("star.json", objs)
        # JSON is not an M3U; return a minimal valid derived playlist only for internal validation.
        return build_channel_m3u(objs, default_group="Sports", default_ua="Sayan10")
    do_m3u("Star JSON companion", "_internal-star-json-validation.m3u", write_star_json_from_mix)
    internal = PLAYLIST_DIR / "_internal-star-json-validation.m3u"
    if internal.exists():
        internal.unlink()

    def write_star2_json() -> str:
        objs = star_json_from_m3u(get("sayan_star"))
        safe_write_json("star2.json", objs)
        return build_channel_m3u(objs, default_group="Sports", default_ua="Sayan10")
    do_m3u("Star2 JSON companion", "_internal-star2-json-validation.m3u", write_star2_json)
    internal2 = PLAYLIST_DIR / "_internal-star2-json-validation.m3u"
    if internal2.exists():
        internal2.unlink()

    # STBPLUS Mobile is intentionally kept as a managed output even when the upstream
    # currently responds with 404; safe_write_m3u prevents replacing a good previous copy.

    ok = sum(1 for _, state, _ in results if state in {"OK", "UNCHANGED"})
    failed = sum(1 for _, state, _ in results if state.startswith("FAILED"))
    print("\n=== SUMMARY ===")
    for label, state, count in results:
        print(f"{state:18} {label:32} {count:5} channels")
    print(f"Successful/unchanged: {ok}/{len(results)}")
    print(f"Failed/unchanged:     {failed}/{len(results)}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run())

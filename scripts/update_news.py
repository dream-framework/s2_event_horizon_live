#!/usr/bin/env python3
"""Fetch RSS feeds, maintain static history, fit S2 decay curves, and write data/news_s2.json.

Designed for GitHub Actions + GitHub Pages. The app itself is static; this script is the batch
updater that turns public news RSS into DREAM S2 retention-cycle JSON.

This version uses publish-time reconstruction: article published_at timestamps are the
observation clock for topic curves. GitHub fetch time is retained as provenance, but
formal S2 fits are based on real publication-time bins, not merely Action snapshots.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SOURCES_PATH = ROOT / "scripts" / "sources.json"
HISTORY_PATH = DATA_DIR / "history.json"
OUTPUT_PATH = DATA_DIR / "news_s2.json"
WINDOW_HOURS = 168
BIN_HOURS = 3
MAX_HISTORY_DAYS = 21
MIN_TOPIC_ARTICLES = 2
MIN_FIT_POINTS = 4
USER_AGENT = "DREAM-S2-NewsDecayBot/0.1 (+https://github.com/)"

TOPICS = {
    "ai": {
        "label": "AI / Tech",
        "keywords": ["ai", "artificial intelligence", "openai", "chatgpt", "model", "semiconductor", "chip", "nvidia", "robot", "automation", "software"],
    },
    "cybersecurity": {
        "label": "Cybersecurity",
        "keywords": ["cyber", "hack", "breach", "ransomware", "malware", "zero-day", "vulnerability", "exploit", "patch", "security", "phishing", "botnet"],
    },
    "quantum": {
        "label": "Quantum tech",
        "keywords": ["quantum", "qubit", "ion trap", "neutral atom", "superconducting", "photonics", "entanglement", "quantum computer", "error correction"],
    },
    "climate": {
        "label": "Climate / Weather",
        "keywords": ["climate", "weather", "storm", "flood", "heat", "wildfire", "emissions", "carbon", "hurricane", "drought", "warming"],
    },
    "markets": {
        "label": "Markets / Economy",
        "keywords": ["market", "stock", "bond", "inflation", "fed", "central bank", "tariff", "trade", "economy", "earnings", "oil", "rate", "currency"],
    },
    "geopolitics": {
        "label": "Geopolitics",
        "keywords": ["war", "military", "ukraine", "russia", "china", "iran", "israel", "gaza", "nato", "sanction", "diplomat", "missile", "defense", "border", "ceasefire", "hostage", "embassy", "foreign minister", "security council"],
    },
    "public_health": {
        "label": "Public Health",
        "keywords": ["health", "virus", "vaccine", "disease", "hospital", "who", "cancer", "drug", "medicine", "outbreak", "flu", "covid"],
    },
    "space_science": {
        "label": "Space / Science",
        "keywords": ["space", "nasa", "moon", "mars", "telescope", "physics", "science", "research", "planet", "astronomy", "satellite"],
    },
    "space_weather": {
        "label": "Space Weather",
        "keywords": ["solar flare", "geomagnetic", "aurora", "cme", "space weather", "solar storm", "kp index", "coronal mass ejection"],
    },
    "energy": {
        "label": "Energy",
        "keywords": ["energy", "power", "grid", "solar", "wind", "nuclear", "battery", "electric", "gas", "renewable", "fusion"],
    },
    "politics": {
        "label": "Politics / Elections",
        "keywords": ["election", "vote", "president", "prime minister", "parliament", "congress", "campaign", "policy", "government", "minister", "court"],
    },
    "culture_media": {
        "label": "Culture / Media",
        "keywords": ["film", "movie", "music", "streaming", "celebrity", "artist", "tv", "trailer", "festival", "creator", "media", "box office"],
    },
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_dt(value: Optional[str], fallback: dt.datetime) -> dt.datetime:
    if not value:
        return fallback
    value = html.unescape(value.strip())
    try:
        iso_value = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(iso_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return fallback


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def stable_id(*parts: str) -> str:
    raw = "|".join(p.strip() for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


def canonical_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered = [(k, v) for k, v in query if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(filtered), ""))
    except Exception:
        return url


def choose_topic(title: str, summary: str) -> Tuple[str, str]:
    text = f"{title} {summary}".lower()
    best_key = "general"
    best_score = 0
    for key, meta in TOPICS.items():
        score = 0
        for kw in meta["keywords"]:
            kw_l = kw.lower()
            if " " in kw_l:
                score += 2 if kw_l in text else 0
            else:
                score += len(re.findall(rf"\b{re.escape(kw_l)}\b", text))
        if score > best_score:
            best_key = key
            best_score = score
    if best_key == "general":
        return "general", "General"
    return best_key, TOPICS[best_key]["label"]


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def load_sources() -> List[Dict[str, str]]:
    sources = read_json(SOURCES_PATH, [])
    if not isinstance(sources, list):
        raise ValueError("scripts/sources.json must be a list")
    return sources


def fetch_url(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # nosec B310: intended public RSS fetcher
        return response.read()


def tag_text(parent: ET.Element, names: Iterable[str]) -> Optional[str]:
    for name in names:
        found = parent.find(name)
        if found is not None and found.text:
            return found.text
    # namespace tolerant fallback
    suffixes = tuple(n.split("}")[-1].split(":")[-1].lower() for n in names)
    for child in parent.iter():
        local = child.tag.split("}")[-1].split(":")[-1].lower()
        if local in suffixes and child.text:
            return child.text
    return None


def parse_feed(xml_bytes: bytes, source: Dict[str, str], fetched_at: dt.datetime) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    records: List[Dict[str, Any]] = []
    for item in items[:80]:
        title = clean_text(tag_text(item, ["title", "{http://www.w3.org/2005/Atom}title"]))
        summary = clean_text(tag_text(item, ["description", "summary", "content", "{http://www.w3.org/2005/Atom}summary"]))
        link = tag_text(item, ["link", "guid", "{http://www.w3.org/2005/Atom}link"]) or ""
        # Atom stores href as an attribute.
        if not link:
            for child in item.iter():
                if child.tag.endswith("link") and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        link = clean_text(link)
        if link and not link.startswith("http"):
            link = source.get("home", link)
        link = canonical_url(link)
        published_raw = tag_text(item, ["pubDate", "published", "updated", "dc:date", "{http://www.w3.org/2005/Atom}updated"])
        published_at = parse_dt(published_raw, fetched_at)
        if not title:
            continue
        topic, topic_label = choose_topic(title, summary)
        url_or_title = link or title
        records.append({
            "id": stable_id(source.get("name", "source"), url_or_title, title),
            "title": title,
            "summary": summary[:500],
            "url": link or source.get("home", ""),
            "source": source.get("name", "Unknown"),
            "topic": topic,
            "topic_label": topic_label,
            "published_at": published_at.isoformat(),
            "first_seen_at": fetched_at.isoformat(),
            "last_seen_at": fetched_at.isoformat(),
        })
    return records


def fetch_all(sources: List[Dict[str, str]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    fetched_at = now_utc()
    all_records: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for src in sources:
        url = src.get("url")
        if not url:
            continue
        try:
            payload = fetch_url(url)
            all_records.extend(parse_feed(payload, src, fetched_at))
        except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError) as exc:
            errors.append({"source": src.get("name", url), "error": str(exc)[:240]})
        time.sleep(0.25)
    return all_records, errors


def merge_history(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]], current_time: dt.datetime) -> List[Dict[str, Any]]:
    cutoff = current_time - dt.timedelta(days=MAX_HISTORY_DAYS)
    by_id: Dict[str, Dict[str, Any]] = {}
    for rec in existing + incoming:
        rec_id = rec.get("id") or stable_id(rec.get("source", ""), rec.get("url", ""), rec.get("title", ""))
        rec["id"] = rec_id
        pub = parse_dt(rec.get("published_at"), current_time)
        if pub < cutoff or pub > current_time + dt.timedelta(hours=12):
            continue
        if rec_id not in by_id:
            by_id[rec_id] = rec
        else:
            # Preserve earliest first_seen, refresh topic/source/title if needed.
            old = by_id[rec_id]
            old_seen = parse_dt(old.get("first_seen_at"), current_time)
            new_seen = parse_dt(rec.get("first_seen_at"), current_time)
            if new_seen < old_seen:
                old["first_seen_at"] = rec.get("first_seen_at")
            old["last_seen_at"] = rec.get("last_seen_at") or rec.get("first_seen_at") or current_time.isoformat()
            for k in ("title", "summary", "url", "source", "topic", "topic_label", "published_at"):
                if rec.get(k):
                    old[k] = rec[k]
    return sorted(by_id.values(), key=lambda r: r.get("published_at", ""), reverse=True)


def local_activity(hour: float) -> float:
    """Smooth 24h human/source activity curve for a local hour.

    Returns a value in roughly [0.28, 1.0]. This is not the DREAM law; it is a nuisance
    exposure model used to keep sleeping/publishing gaps from masquerading as S2 decay.
    """
    # Two broad daytime publishing peaks with a nighttime floor.
    morning = math.exp(-0.5 * (((hour - 10.5 + 12) % 24 - 12) / 4.2) ** 2)
    afternoon = math.exp(-0.5 * (((hour - 15.5 + 12) % 24 - 12) / 5.0) ** 2)
    return max(0.28, min(1.0, 0.26 + 0.45 * morning + 0.38 * afternoon))


def circadian_factor(when: dt.datetime) -> float:
    """Approximate global RSS availability factor for a UTC timestamp.

    Public news feeds are global, so we mix Americas, Europe/Africa, and Asia-Pacific
    local-day curves. Weekend publishing is mildly downweighted. Values are clipped so
    the correction cannot explode overnight bins.
    """
    when = when.astimezone(dt.timezone.utc)
    utc_hour = when.hour + when.minute / 60
    regional = (
        0.44 * local_activity((utc_hour - 5) % 24) +
        0.36 * local_activity((utc_hour + 1) % 24) +
        0.20 * local_activity((utc_hour + 8) % 24)
    )
    if when.weekday() >= 5:
        regional *= 0.86
    return max(0.35, min(1.05, regional))


def make_bins(records: List[Dict[str, Any]], current_time: dt.datetime) -> Tuple[List[int], List[float], List[float]]:
    n_bins = WINDOW_HOURS // BIN_HOURS
    start = current_time - dt.timedelta(hours=WINDOW_HOURS)
    raw_counts = [0] * n_bins
    corrected_counts = [0.0] * n_bins
    factor_sums = [0.0] * n_bins
    factor_n = [0] * n_bins
    for rec in records:
        published = parse_dt(rec.get("published_at"), current_time)
        if published < start or published > current_time:
            continue
        idx = int((published - start).total_seconds() // 3600 // BIN_HOURS)
        if 0 <= idx < n_bins:
            factor = circadian_factor(published)
            raw_counts[idx] += 1
            corrected_counts[idx] += 1.0 / factor
            factor_sums[idx] += factor
            factor_n[idx] += 1
    bin_factors: List[float] = []
    for i in range(n_bins):
        if factor_n[i]:
            bin_factors.append(factor_sums[i] / factor_n[i])
        else:
            midpoint = start + dt.timedelta(hours=i * BIN_HOURS + BIN_HOURS / 2)
            bin_factors.append(circadian_factor(midpoint))
    return raw_counts, corrected_counts, bin_factors


def circadian_bias(raw_counts: List[int], corrected_counts: List[float]) -> Optional[float]:
    raw_total = sum(raw_counts)
    corr_total = sum(corrected_counts)
    if raw_total <= 0 or corr_total <= 0:
        return None
    raw_norm = [c / raw_total for c in raw_counts]
    corr_norm = [c / corr_total for c in corrected_counts]
    return sum(abs(a - b) for a, b in zip(raw_norm, corr_norm)) / 2.0

def sse_for_beta_tau(xs: List[float], ys: List[float], tau: float, beta: float) -> float:
    sse = 0.0
    for x, y in zip(xs, ys):
        pred = math.exp(-((max(0.0, x) / tau) ** beta)) if tau > 0 else 0.0
        sse += (y - pred) ** 2
    return sse


def provisional_fit(counts: List[float], peak_idx: int, reason: str, raw_counts: Optional[List[int]] = None, bin_factors: Optional[List[float]] = None) -> Dict[str, Any]:
    """Low-confidence visual guide for live topics that do not yet have a cooling tail."""
    if not counts or max(counts) <= 0:
        return empty_fit([])
    peak_count = max(counts) or 1
    tail_counts = counts[peak_idx:] if 0 <= peak_idx < len(counts) else counts
    nonzero_total = sum(1 for c in counts if c > 0)
    active_bins = sum(1 for c in tail_counts if c > 0)
    tau = 18.0 + min(54.0, 3.0 * nonzero_total + 1.5 * math.sqrt(max(0, sum(counts))))
    beta = 1.15 if active_bins <= 2 else 1.35
    if peak_idx >= len(counts) - 2:
        phase = "Collecting post-peak evidence"
    elif sum(tail_counts) < MIN_TOPIC_ARTICLES:
        phase = "Sparse publish-time signal"
    else:
        phase = "Provisional S2 guide"
    raw_tail_counts = raw_counts[peak_idx:] if raw_counts else None
    raw_max = max(raw_tail_counts) if raw_tail_counts else 1
    factor_tail = bin_factors[peak_idx:] if bin_factors else None
    series: List[Dict[str, Any]] = []
    horizon_bins = max(8, min(16, len(tail_counts) + 5))
    for i in range(horizon_bins):
        x = i * BIN_HOURS
        obs = tail_counts[i] / peak_count if i < len(tail_counts) else None
        raw_obs = raw_tail_counts[i] / raw_max if raw_tail_counts and i < len(raw_tail_counts) and raw_max > 0 else None
        factor = factor_tail[i] if factor_tail and i < len(factor_tail) else None
        fit = math.exp(-((max(0.0, x) / tau) ** beta))
        residual = None if obs is None else obs - fit
        series.append({
            "x_hours": round(float(x), 3),
            "observed": None if obs is None else round(float(obs), 6),
            "observed_corrected": None if obs is None else round(float(obs), 6),
            "observed_raw": None if raw_obs is None else round(float(raw_obs), 6),
            "circadian_factor": None if factor is None else round(float(factor), 6),
            "fit": round(float(fit), 6),
            "residual": None if residual is None else round(float(residual), 6),
            "projected": obs is None,
        })
    observed_residuals = [abs(d["residual"]) for d in series if d.get("residual") is not None]
    residual_dust = math.sqrt(sum(r*r for r in observed_residuals) / max(1, len(observed_residuals))) if observed_residuals else None
    return {
        "tau_hours": tau,
        "beta": beta,
        "half_life_hours": tau * (math.log(2) ** (1 / beta)),
        "log_r2": None,
        "delta_aic_vs_exp": None,
        "coherence_left_hours": tau,
        "series": series,
        "residual_dust": residual_dust,
        "phase": phase,
        "fit_status": "provisional",
        "fit_reason": reason,
        "clock": "published_at",
        "peak_bin_index": peak_idx,
        "tail_bins": len(tail_counts),
    }


def fit_decay(counts: List[float], raw_counts: Optional[List[int]] = None, bin_factors: Optional[List[float]] = None) -> Dict[str, Any]:
    if not counts or max(counts) <= 0:
        return empty_fit([])
    peak_idx = max(range(len(counts)), key=lambda i: counts[i])
    # Fit only the post-peak cooling tail reconstructed from article publication times.
    # If the peak is too recent, the cycle is still rising/peaking and the fit is
    # intentionally marked as provisional instead of inventing a tail.
    tail_counts = counts[peak_idx:]
    max_count = max(tail_counts) or 1
    ys = [c / max_count for c in tail_counts]
    xs = [i * BIN_HOURS for i in range(len(tail_counts))]
    fit_pairs = [(x, y) for x, y in zip(xs, ys) if y > 0]
    fit_xs = [p[0] for p in fit_pairs]
    fit_ys = [p[1] for p in fit_pairs]
    if len(fit_xs) < MIN_FIT_POINTS or sum(tail_counts) < MIN_TOPIC_ARTICLES:
        reason = f"Need at least {MIN_FIT_POINTS} nonzero post-peak publish-time bins and {MIN_TOPIC_ARTICLES} tail articles for a formal S2 fit."
        return provisional_fit(counts, peak_idx, reason, raw_counts, bin_factors)

    tau_values = [4, 6, 8, 10, 12, 16, 20, 24, 30, 36, 42, 48, 60, 72, 90, 108, 132, 156]
    beta_values = [0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0, 2.25]
    best = (float("inf"), 24.0, 1.0)
    for tau in tau_values:
        for beta in beta_values:
            sse = sse_for_beta_tau(fit_xs, fit_ys, tau, beta)
            if sse < best[0]:
                best = (sse, tau, beta)
    exp_best = (float("inf"), 24.0)
    for tau in tau_values:
        sse = sse_for_beta_tau(fit_xs, fit_ys, tau, 1.0)
        if sse < exp_best[0]:
            exp_best = (sse, tau)

    sse, tau, beta = best
    n = max(1, len(fit_xs))
    eps = 1e-4
    log_y = [math.log(max(eps, y)) for y in fit_ys]
    log_pred = [math.log(max(eps, math.exp(-((max(0.0, x) / tau) ** beta)))) for x in fit_xs]
    mean_log_y = sum(log_y) / n
    sse_log = sum((a - b) ** 2 for a, b in zip(log_y, log_pred))
    sst_log = sum((a - mean_log_y) ** 2 for a in log_y) or 1e-9
    log_r2 = 1 - sse_log / sst_log
    aic_s2 = n * math.log(max(sse / n, 1e-9)) + 2 * 2
    aic_exp = n * math.log(max(exp_best[0] / n, 1e-9)) + 2 * 1
    delta_aic = aic_exp - aic_s2
    half_life = tau * (math.log(2) ** (1 / beta))
    elapsed_since_peak = (len(tail_counts) - 1) * BIN_HOURS
    coherence_left = max(0.0, tau - elapsed_since_peak)
    raw_tail_counts = raw_counts[peak_idx:] if raw_counts else None
    raw_max = max(raw_tail_counts) if raw_tail_counts else 1
    raw_ys = [c / raw_max for c in raw_tail_counts] if raw_tail_counts and raw_max > 0 else None
    factor_tail = bin_factors[peak_idx:] if bin_factors else None
    series = make_series(xs, ys, tau, beta, raw_ys, factor_tail)
    residuals = [d["residual"] for d in series]
    late = residuals[len(residuals)//2:] or residuals
    residual_dust = math.sqrt(sum(r * r for r in late) / max(1, len(late)))
    current_norm = ys[-1] if ys else 0
    if peak_idx >= len(counts) - 2:
        phase = "Collecting post-peak evidence"
    elif current_norm >= 0.55:
        phase = "Active plateau"
    elif current_norm >= 0.18:
        phase = "Cooling"
    else:
        phase = "Residual dust tail"
    return {
        "tau_hours": tau,
        "beta": beta,
        "half_life_hours": half_life,
        "log_r2": log_r2,
        "delta_aic_vs_exp": delta_aic,
        "coherence_left_hours": coherence_left,
        "series": series,
        "residual_dust": residual_dust,
        "phase": phase,
        "fit_status": "formal",
        "fit_reason": "Formal post-peak S2 fit using article published_at bins",
        "clock": "published_at",
        "peak_bin_index": peak_idx,
        "tail_bins": len(tail_counts),
    }


def make_series(xs: List[float], ys: List[float], tau: float, beta: float, raw_ys: Optional[List[float]] = None, factors: Optional[List[float]] = None) -> List[Dict[str, float]]:
    out = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        fit = math.exp(-((max(0.0, x) / tau) ** beta)) if tau > 0 else 0.0
        raw_y = raw_ys[i] if raw_ys and i < len(raw_ys) else None
        factor = factors[i] if factors and i < len(factors) else None
        out.append({
            "x_hours": round(float(x), 3),
            "observed": round(float(y), 6),
            "observed_corrected": round(float(y), 6),
            "observed_raw": None if raw_y is None else round(float(raw_y), 6),
            "circadian_factor": None if factor is None else round(float(factor), 6),
            "fit": round(float(fit), 6),
            "residual": round(float(y - fit), 6),
        })
    return out


def empty_fit(series: List[Dict[str, float]]) -> Dict[str, Any]:
    return {
        "tau_hours": None,
        "beta": None,
        "half_life_hours": None,
        "log_r2": None,
        "delta_aic_vs_exp": None,
        "coherence_left_hours": None,
        "series": series,
        "residual_dust": None,
        "phase": "No live signal yet",
        "fit_status": "empty",
        "fit_reason": "No usable articles in the rolling window",
        "peak_bin_index": None,
        "tail_bins": len(series),
    }


def build_output(history: List[Dict[str, Any]], sources: List[Dict[str, str]], errors: List[Dict[str, str]], current_time: dt.datetime) -> Dict[str, Any]:
    cutoff = current_time - dt.timedelta(hours=WINDOW_HOURS)
    recent = [r for r in history if parse_dt(r.get("published_at"), current_time) >= cutoff]
    by_topic: Dict[str, List[Dict[str, Any]]] = {}
    for rec in recent:
        by_topic.setdefault(rec.get("topic", "general"), []).append(rec)
    topics = []
    for key, records in by_topic.items():
        if len(records) < MIN_TOPIC_ARTICLES and key != "general":
            continue
        raw_counts, corrected_counts, bin_factors = make_bins(records, current_time)
        fit = fit_decay(corrected_counts, raw_counts, bin_factors)
        bias = circadian_bias(raw_counts, corrected_counts)
        label = records[0].get("topic_label") or TOPICS.get(key, {}).get("label") or key.replace("_", " ").title()
        topics.append({
            "key": key,
            "label": label,
            "article_count": len(records),
            "phase": fit["phase"],
            "residual_dust": fit["residual_dust"],
            "histogram_counts": [round(float(v), 6) for v in corrected_counts],
            "raw_histogram_counts": raw_counts,
            "circadian_factors": [round(float(v), 6) for v in bin_factors],
            "circadian_bias": None if bias is None else round(float(bias), 6),
            "peak_bin_index": fit.get("peak_bin_index"),
            "tail_bins": fit.get("tail_bins"),
            "fit": {k: fit.get(k) for k in ["tau_hours", "beta", "half_life_hours", "log_r2", "delta_aic_vs_exp", "coherence_left_hours", "fit_status", "fit_reason"]},
            "series": fit["series"],
        })
    topics.sort(key=lambda t: (t["article_count"], t["residual_dust"] or 0), reverse=True)
    if not topics:
        fit = empty_fit([])
        raw_counts, corrected_counts, bin_factors = make_bins(recent, current_time)
        topics = [{
            "key": "general", "label": "General", "article_count": len(recent), "phase": fit["phase"],
            "residual_dust": None,
            "histogram_counts": [round(float(v), 6) for v in corrected_counts],
            "raw_histogram_counts": raw_counts,
            "circadian_factors": [round(float(v), 6) for v in bin_factors],
            "circadian_bias": None if bias is None else round(float(bias), 6),
            "peak_bin_index": fit.get("peak_bin_index"),
            "tail_bins": fit.get("tail_bins"),
            "fit": {k: fit.get(k) for k in ["tau_hours", "beta", "half_life_hours", "log_r2", "delta_aic_vs_exp", "coherence_left_hours", "fit_status", "fit_reason"]},
            "series": fit["series"],
        }]
    articles = sorted(recent, key=lambda r: r.get("published_at", ""), reverse=True)[:180]
    return {
        "generated_at": current_time.isoformat(),
        "window_hours": WINDOW_HOURS,
        "bin_hours": BIN_HOURS,
        "model": {
            "name": "DREAM S2 news-cycle retention",
            "lambda_interpretation": "lambda = elapsed hours since topic attention peak",
            "retention_law": "R(lambda)=exp[-(lambda/lambda_q)^D_eff]",
            "signal_preprocessing": "Formal S2 fit uses circadian-corrected publish-time article counts: raw count divided by expected global publishing/activity factor.",
            "observation_clock": "article.published_at, not GitHub Action run time",
            "history_role": "data/history.json retains raw article records and first_seen/last_seen provenance; publication time reconstructs the curve.",
            "comparison": "Delta AIC = AIC(exponential beta=1) - AIC(stretched S2 beta free)",
        },
        "summary": {
            "article_count": len(recent),
            "history_count": len(history),
            "source_count": len(sources),
            "fetch_errors": errors,
        },
        "sources": [
            {"name": src.get("name", "Unknown"), "url": src.get("url", ""), "home": src.get("home", "")}
            for src in sources
        ],
        "topics": topics,
        "articles": articles,
    }


def sample_history(current_time: dt.datetime) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    patterns = [
        ("geopolitics", "Geopolitics", "Ceasefire negotiation update", 60, 30, 1.18, "DREAM Wire"),
        ("ai", "AI / Tech", "AI model release cycle", 36, 18, 1.42, "DREAM Wire"),
        ("cybersecurity", "Cybersecurity", "Critical library patch", 48, 24, 1.33, "DREAM Wire"),
        ("culture_media", "Culture / Media", "Trailer and creator backlash", 28, 13, 1.96, "DREAM Wire"),
        ("climate", "Climate / Weather", "Severe storm system", 42, 38, 0.86, "DREAM Wire"),
        ("markets", "Markets / Economy", "Central bank rate-watch", 84, 46, 0.62, "DREAM Wire"),
        ("space_weather", "Space Weather", "Geomagnetic storm watch", 38, 22, 1.71, "DREAM Wire"),
        ("quantum", "Quantum tech", "Neutral atom roadmap", 54, 19, 1.64, "DREAM Wire"),
        ("public_health", "Public Health", "Trial readout and regulator calendar", 68, 28, 1.25, "DREAM Wire"),
    ]
    for topic, label, base_title, peak_age_h, tau, beta, source in patterns:
        for age in range(0, WINDOW_HOURS, 4):
            # Synthetic attention centered at peak_age_h, then S2-like decay.
            if age < peak_age_h:
                intensity = max(0.05, age / max(1, peak_age_h))
            else:
                intensity = math.exp(-(((age - peak_age_h) / tau) ** beta))
            repeats = 0
            if intensity > 0.72:
                repeats = 3
            elif intensity > 0.45:
                repeats = 2
            elif intensity > 0.15 and age % 12 == 0:
                repeats = 1
            for j in range(repeats):
                published = current_time - dt.timedelta(hours=age, minutes=7 * j)
                title = f"{base_title}: update {age:03d}-{j}"
                url = ""
                samples.append({
                    "id": stable_id(topic, url, title),
                    "title": title,
                    "summary": f"Synthetic local sample item for {label}. Replace by scheduled RSS updates.",
                    "url": url,
                    "source": source,
                    "topic": topic,
                    "topic_label": label,
                    "published_at": published.isoformat(),
                    "first_seen_at": published.isoformat(),
                    "last_seen_at": published.isoformat(),
                })
    samples.sort(key=lambda r: r["published_at"], reverse=True)
    return samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", action="store_true", help="Generate synthetic sample history instead of fetching RSS.")
    parser.add_argument("--no-fetch", action="store_true", help="Rebuild data from existing history only.")
    args = parser.parse_args()

    current_time = now_utc()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sources = load_sources()
    existing = read_json(HISTORY_PATH, [])
    if not isinstance(existing, list):
        existing = []

    errors: List[Dict[str, str]] = []
    if args.sample:
        incoming = sample_history(current_time)
        existing = []
    elif args.no_fetch:
        incoming = []
    else:
        incoming, errors = fetch_all(sources)

    history = merge_history(existing, incoming, current_time)
    output = build_output(history, sources, errors, current_time)
    write_json(HISTORY_PATH, history)
    write_json(OUTPUT_PATH, output)
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)} with {output['summary']['article_count']} recent articles across {len(output['topics'])} topics.")
    if errors:
        print(f"Fetch errors: {len(errors)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

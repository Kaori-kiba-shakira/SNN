from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import smtplib
import sys
import time
from datetime import datetime, timedelta
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, List
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


TARGET_URL = "http://izumino.jp/Security/sec_trend.cgi"
TARGET_FEED_URLS = ["https://www.security-next.com/feed"]
_LAST_LLM_CALL_TS = 0.0
DETAIL_THRESHOLD = 0.9
TARGET_ORG_HINTS = [
    "省",
    "庁",
    "県",
    "都",
    "道",
    "府",
    "市",
    "区",
    "町",
    "村",
    "自治体",
    "独立行政法人",
    "独法",
    "保育園",
    "こども園",
    "幼稚園",
    "園",
    "病院",
]


@dataclass
class SecurityNewsItem:
    timestamp: str
    media: str
    title: str
    url: str
    body: str


@dataclass
class RelevanceResult:
    score: float
    name: str
    summary: str


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
    )

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_html(url: str, timeout: tuple[float, float]) -> str:
    session = build_session()
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def fetch_text(url: str, timeout: tuple[float, float]) -> str:
    session = build_session()
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _div_text(node) -> str:
    return node.get_text(" ", strip=True)


def parse_items(html: str) -> List[SecurityNewsItem]:
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.find_all("div", class_=["c_date_time", "c_media", "c_link", "c_body"])

    items: List[SecurityNewsItem] = []
    current_timestamp = ""
    current_item: SecurityNewsItem | None = None

    for block in blocks:
        classes = set(block.get("class", []))

        if "c_date_time" in classes:
            current_timestamp = _div_text(block)
            continue

        if "c_media" in classes:
            if current_item and (current_item.title or current_item.body):
                items.append(current_item)
            current_item = SecurityNewsItem(
                timestamp=current_timestamp,
                media=_div_text(block),
                title="",
                url="",
                body="",
            )
            continue

        if "c_link" in classes:
            if current_item is None:
                current_item = SecurityNewsItem(
                    timestamp=current_timestamp,
                    media="",
                    title="",
                    url="",
                    body="",
                )
            anchor = block.find("a")
            if anchor:
                current_item.title = anchor.get_text(" ", strip=True)
                current_item.url = anchor.get("href", "")
            continue

        if "c_body" in classes:
            if current_item is None:
                current_item = SecurityNewsItem(
                    timestamp=current_timestamp,
                    media="",
                    title="",
                    url="",
                    body="",
                )
            current_item.body = _div_text(block)
            if current_item.title or current_item.body:
                items.append(current_item)
            current_item = None

    if current_item and (current_item.title or current_item.body):
        items.append(current_item)

    return items


def _format_pub_date(pub_date: str) -> str:
    try:
        dt = parsedate_to_datetime(pub_date)
    except Exception:  # noqa: BLE001
        return ""

    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def _child_text(parent: ET.Element, tag: str) -> str:
    child = parent.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _local_name(tag: str) -> str:
    return tag.split("}", maxsplit=1)[-1] if "}" in tag else tag


def _child_text_by_local_name(parent: ET.Element, local_name: str) -> str:
    for child in list(parent):
        if _local_name(child.tag) == local_name and child.text is not None:
            return child.text.strip()
    return ""


def parse_rss_feed_items(feed_xml: str, *, source_media: str) -> List[SecurityNewsItem]:
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError:
        return []

    items: list[SecurityNewsItem] = []
    for item_node in root.findall(".//item"):
        title = _child_text(item_node, "title")
        url = _child_text(item_node, "link")
        pub_date = _child_text(item_node, "pubDate")
        timestamp = _format_pub_date(pub_date)
        description = _child_text(item_node, "description")
        if not description:
            description = _child_text_by_local_name(item_node, "encoded")
        body = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)

        if not title and not body:
            continue

        items.append(
            SecurityNewsItem(
                timestamp=timestamp,
                media=source_media,
                title=title,
                url=url,
                body=body,
            )
        )

    return items


def filter_latest_timestamp(items: List[SecurityNewsItem]) -> List[SecurityNewsItem]:
    if not items:
        return []
    latest = items[0].timestamp
    return [item for item in items if item.timestamp == latest]


def _extract_date_from_timestamp(timestamp: str) -> datetime.date | None:
    text = timestamp.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _sort_items_by_timestamp_desc(items: list[SecurityNewsItem]) -> list[SecurityNewsItem]:
    def sort_key(item: SecurityNewsItem) -> tuple[datetime, str]:
        raw = item.timestamp.strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return (datetime.strptime(raw, fmt), item.title)
            except ValueError:
                continue
        return (datetime.min, item.title)

    return sorted(items, key=sort_key, reverse=True)


def collect_items_from_sources(
    *,
    primary_url: str,
    feed_urls: list[str],
    timeout: tuple[float, float],
    fail_soft: bool,
) -> list[SecurityNewsItem]:
    items: list[SecurityNewsItem] = []

    try:
        primary_html = fetch_html(primary_url, timeout=timeout)
        items.extend(parse_items(primary_html))
    except requests.RequestException as exc:
        if not fail_soft:
            raise
        print(f"[fetch-warn] primary source failed: {exc}", file=sys.stderr)

    for feed_url in feed_urls:
        try:
            feed_xml = fetch_text(feed_url, timeout=timeout)
            feed_items = parse_rss_feed_items(feed_xml, source_media="security-next")
            items.extend(feed_items)
        except requests.RequestException as exc:
            if not fail_soft:
                raise
            print(f"[fetch-warn] feed source failed ({feed_url}): {exc}", file=sys.stderr)

    return _sort_items_by_timestamp_desc(items)


def filter_previous_day_from_latest(items: List[SecurityNewsItem]) -> List[SecurityNewsItem]:
    dated_items: list[tuple[SecurityNewsItem, datetime.date]] = []
    for item in items:
        parsed = _extract_date_from_timestamp(item.timestamp)
        if parsed is not None:
            dated_items.append((item, parsed))

    if not dated_items:
        return []

    latest_date = max(date_value for _, date_value in dated_items)
    previous_date = latest_date - timedelta(days=1)
    return [item for item, date_value in dated_items if date_value == previous_date]


def format_text(items: List[SecurityNewsItem]) -> str:
    return format_text_with_relevance(items, {})


def format_text_with_relevance(items: List[SecurityNewsItem], relevance_map: dict[str, RelevanceResult]) -> str:
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        lines.append(f"[{index}] {item.timestamp}")
        lines.append(f"media: {item.media}")
        lines.append(f"title: {item.title}")
        lines.append(f"url: {item.url}")
        lines.append(f"body: {item.body}")
        key = build_item_key(item)
        relevance = relevance_map.get(key)
        if relevance is not None:
            lines.append(f"relevance_score: {relevance.score:.3f}")
            if relevance.name:
                lines.append(f"incident_name: {relevance.name}")
            if relevance.summary:
                lines.append(f"incident_summary: {relevance.summary}")
        lines.append("")
    return "\n".join(lines).strip()


def build_notification_text(
    items: List[SecurityNewsItem],
    source_url: str,
    relevance_map: dict[str, RelevanceResult] | None = None,
    total_items: int | None = None,
    evaluated_items: int | None = None,
    threshold: float | None = None,
) -> str:
    relevance_map = relevance_map or {}
    if not items:
        return f"Security Update: 今日のセキュリティインシデント情報はありません. "

    timestamp = items[0].timestamp
    lines = [f"Security Update ({timestamp})"]
    if total_items is not None:
        lines.append(f"total_previous_day_items: {total_items}")
    if evaluated_items is not None:
        lines.append(f"evaluated_items: {evaluated_items}")
    if threshold is not None:
        lines.append(f"relevance_threshold: {threshold:.2f}")
    lines.append(f"notified_items: {len(items)}")
    for item in items:
        key = build_item_key(item)
        relevance = relevance_map.get(key)
        if relevance is not None:
            lines.append(f"- [{item.media}] {item.title} (score={relevance.score:.2f})")
            if relevance.name:
                lines.append(f"  name: {relevance.name}")
            if relevance.summary:
                lines.append(f"  summary: {relevance.summary}")
        else:
            lines.append(f"- [{item.media}] {item.title}")
        if item.url:
            lines.append(f"  {item.url}")
        lines.append("")
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
        if isinstance(value, dict):
            return value
        return None
    except Exception:  # noqa: BLE001
        return None


def _coerce_score(value: Any) -> float:
    try:
        score = float(value)
    except Exception:  # noqa: BLE001
        return 0.0
    if score < 0:
        return 0.0
    if score > 1:
        return 1.0
    return score


def _truncate_text(value: Any, max_len: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _is_target_org_context(name: str, title: str, body: str) -> bool:
    text = f"{name} {title} {body}"
    return any(hint in text for hint in TARGET_ORG_HINTS)


def evaluate_relevance_with_groq(
    item: SecurityNewsItem,
    *,
    api_base: str,
    api_key: str,
    model: str,
    timeout: tuple[float, float],
) -> RelevanceResult:
    global _LAST_LLM_CALL_TS

    min_interval_sec = _env_float("GROQ_EVAL_MIN_INTERVAL_SEC", 5.0)
    max_retries = _env_int("GROQ_EVAL_MAX_RETRIES", 3)
    max_backoff_sec = _env_float("GROQ_EVAL_MAX_BACKOFF_SEC", 90.0)

    endpoint = f"{api_base.rstrip('/')}/chat/completions"
    prompt = (
        "あなたはセキュリティニュースの関連度評価器です。"
        "以下のニュースが中央省庁や独立行政法人、地方公共団体、保育園、こども園、幼稚園に関するセキュリティインシデントに該当するかを0から1で評価してください。"
        "必ずJSONのみで返答し、スキーマは"
        '{"score":"0.0~1.0で評価","name":"インシデントが起きた組織名","summary":"score0.95以上の場合はurl先の記事内容を要約"}'
        "としてください。"
        f"title: {item.title}"
        f"url: {item.url}"
        f"body: {item.body}"
    )
    base_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "あなたはセキュリティニュースの評価器です。"
                    "必ずJSONのみを返し、キーは score, name, summary の3つだけにしてください。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }

    payload_variants: list[tuple[str, dict[str, Any]]] = [
        (
            "json_object",
            {
                **base_payload,
                "response_format": {"type": "json_object"},
            },
        ),
        ("none", {**base_payload}),
    ]
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    data: dict[str, Any] | None = None
    for variant_name, payload in payload_variants:
        for attempt in range(max_retries + 1):
            elapsed = time.monotonic() - _LAST_LLM_CALL_TS
            if elapsed < min_interval_sec:
                time.sleep(min_interval_sec - elapsed)

            try:
                response = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                if attempt >= max_retries:
                    if variant_name == payload_variants[-1][0]:
                        raise RuntimeError("Groq API request failed") from exc
                    break
                wait_sec = min(max_backoff_sec, (2**attempt) + random.uniform(0.0, 0.8))
                print(
                    f"[evaluate-retry] request-failed sleep={wait_sec:.1f}s attempt={attempt + 1}/{max_retries}",
                    file=sys.stderr,
                )
                time.sleep(wait_sec)
                continue

            _LAST_LLM_CALL_TS = time.monotonic()
            status = response.status_code

            if status in (429, 500, 502, 503, 504):
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                if retry_after is not None:
                    wait_sec = min(120.0, retry_after)
                else:
                    wait_sec = min(
                        max_backoff_sec,
                        (2**attempt) + random.uniform(0.0, 0.8),
                    )
                if attempt >= max_retries:
                    if variant_name == payload_variants[-1][0]:
                        raise RuntimeError(f"Groq API HTTP {status} (retry limit reached)")
                    break
                print(
                    f"[evaluate-retry] status={status} sleep={wait_sec:.1f}s attempt={attempt + 1}/{max_retries}",
                    file=sys.stderr,
                )
                time.sleep(wait_sec)
                continue

            if status == 400:
                if variant_name != payload_variants[-1][0]:
                    print(
                        f"[evaluate-warn] response_format={variant_name} not accepted, fallback next",
                        file=sys.stderr,
                    )
                    break
                raise RuntimeError("Groq API HTTP 400")

            if status >= 400:
                raise RuntimeError(f"Groq API HTTP {status}")

            data = response.json()
            break

        if data is not None:
            break

    if data is None:
        raise RuntimeError("Groq API response unavailable")

    choices = data.get("choices", [])
    answer = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            answer = str(message.get("content", "")).strip()

    if not answer and isinstance(data.get("error"), dict):
        error_obj = data.get("error", {})
        error_message = str(error_obj.get("message", "unknown error"))
        raise RuntimeError(f"Groq API error: {error_message}")
    parsed = _extract_json_object(answer)
    if not parsed:
        return RelevanceResult(
            score=0.0,
            name="",
            summary="",
        )
    score = _coerce_score(parsed.get("score", 0.0))
    name = _truncate_text(parsed.get("name", ""), 120)
    summary = _truncate_text(parsed.get("summary", ""), 100)

    if not _is_target_org_context(name, item.title, item.body):
        score = 0.0
        name = ""
        summary = ""

    if score < DETAIL_THRESHOLD:
        name = ""
        summary = ""

    return RelevanceResult(
        score=score,
        name=name,
        summary=summary,
    )


def evaluate_items_with_groq_sequential(
    items: List[SecurityNewsItem],
    *,
    api_base: str,
    api_key: str,
    model: str,
    timeout: tuple[float, float],
) -> dict[str, RelevanceResult]:
    results: dict[str, RelevanceResult] = {}
    for item in items:
        try:
            relevance = evaluate_relevance_with_groq(
                item,
                api_base=api_base,
                api_key=api_key,
                model=model,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[evaluate-error] {exc}", file=sys.stderr)
            relevance = RelevanceResult(
                score=0.0,
                name="",
                summary="",
            )
        results[build_item_key(item)] = relevance
    return results


def evaluate_relevance_with_google_studio(
    item: SecurityNewsItem,
    *,
    api_base: str,
    api_key: str,
    model: str,
    timeout: tuple[float, float],
) -> RelevanceResult:
    global _LAST_LLM_CALL_TS

    min_interval_sec = _env_float("GOOGLE_EVAL_MIN_INTERVAL_SEC", 1.0)
    max_retries = _env_int("GOOGLE_EVAL_MAX_RETRIES", 3)
    max_backoff_sec = _env_float("GOOGLE_EVAL_MAX_BACKOFF_SEC", 90.0)

    endpoint = f"{api_base.rstrip('/')}/models/{model}:generateContent?key={api_key}"
    prompt = (
        "あなたはセキュリティニュースの関連度評価器です。"
        "以下のニュースが中央省庁や独立行政法人、地方公共団体、保育園、こども園、幼稚園に関するセキュリティインシデントに該当するかを0から1で評価してください。"
        "必ずJSONのみで返答し、スキーマは"
        '{"score":"0.0~1.0で評価","name":"インシデントが起きた組織名","summary":"score0.95以上の場合はurl先の記事内容を要約"}'
        "としてください。"
        f"title: {item.title}"
        f"url: {item.url}"
        f"body: {item.body}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "required": ["score", "name", "summary"],
                "properties": {
                    "score": {"type": "NUMBER", "minimum": 0, "maximum": 1},
                    "name": {"type": "STRING"},
                    "summary": {"type": "STRING"},
                },
            },
        },
    }
    headers = {"Content-Type": "application/json"}

    data: dict[str, Any] | None = None
    for attempt in range(max_retries + 1):
        elapsed = time.monotonic() - _LAST_LLM_CALL_TS
        if elapsed < min_interval_sec:
            time.sleep(min_interval_sec - elapsed)

        try:
            response = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise RuntimeError("Google API request failed") from exc
            wait_sec = min(max_backoff_sec, (2**attempt) + random.uniform(0.0, 0.8))
            print(
                f"[evaluate-retry] request-failed sleep={wait_sec:.1f}s attempt={attempt + 1}/{max_retries}",
                file=sys.stderr,
            )
            time.sleep(wait_sec)
            continue

        _LAST_LLM_CALL_TS = time.monotonic()
        status = response.status_code

        if status in (429, 500, 502, 503, 504):
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            if retry_after is not None:
                wait_sec = min(120.0, retry_after)
            else:
                wait_sec = min(max_backoff_sec, (2**attempt) + random.uniform(0.0, 0.8))
            if attempt >= max_retries:
                raise RuntimeError(f"Google API HTTP {status} (retry limit reached)")
            print(
                f"[evaluate-retry] status={status} sleep={wait_sec:.1f}s attempt={attempt + 1}/{max_retries}",
                file=sys.stderr,
            )
            time.sleep(wait_sec)
            continue

        if status >= 400:
            raise RuntimeError(f"Google API HTTP {status}")

        data = response.json()
        break

    if data is None:
        raise RuntimeError("Google API response unavailable")

    candidates = data.get("candidates", [])
    parts: list[dict[str, Any]] = []
    if candidates and isinstance(candidates[0], dict):
        content = candidates[0].get("content", {})
        if isinstance(content, dict):
            raw_parts = content.get("parts", [])
            if isinstance(raw_parts, list):
                parts = [part for part in raw_parts if isinstance(part, dict)]
    answer = "\n".join(str(part.get("text", "")) for part in parts).strip()

    parsed = _extract_json_object(answer)
    if not parsed:
        return RelevanceResult(
            score=0.0,
            name="",
            summary="",
        )

    score = _coerce_score(parsed.get("score", 0.0))
    name = _truncate_text(parsed.get("name", ""), 120)
    summary = _truncate_text(parsed.get("summary", ""), 100)

    if not _is_target_org_context(name, item.title, item.body):
        score = 0.0
        name = ""
        summary = ""

    if score < DETAIL_THRESHOLD:
        name = ""
        summary = ""

    return RelevanceResult(
        score=score,
        name=name,
        summary=summary,
    )


def evaluate_items_with_google_studio_sequential(
    items: List[SecurityNewsItem],
    *,
    api_base: str,
    api_key: str,
    model: str,
    timeout: tuple[float, float],
) -> dict[str, RelevanceResult]:
    results: dict[str, RelevanceResult] = {}
    for item in items:
        try:
            relevance = evaluate_relevance_with_google_studio(
                item,
                api_base=api_base,
                api_key=api_key,
                model=model,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[evaluate-error] {exc}", file=sys.stderr)
            relevance = RelevanceResult(
                score=0.0,
                name="",
                summary="",
            )
        results[build_item_key(item)] = relevance
    return results


def serialize_items(
    items: List[SecurityNewsItem], relevance_map: dict[str, RelevanceResult]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        row = asdict(item)
        relevance = relevance_map.get(build_item_key(item))
        if relevance is not None:
            row["relevance_score"] = relevance.score
            row["incident_name"] = relevance.name
            row["incident_summary"] = relevance.summary
        result.append(row)
    return result


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None

    token = value.strip()
    try:
        return max(0.0, float(token))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(token)
        now = datetime.now(retry_at.tzinfo)
        return max(0.0, (retry_at - now).total_seconds())
    except Exception:  # noqa: BLE001
        return None


def send_email_notification(
    *,
    subject: str,
    body: str,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    email_from: str,
    email_to: list[str],
    use_ssl: bool,
    use_starttls: bool,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(email_to)
    msg.set_content(body)

    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as smtp:
            smtp.set_debuglevel(1)
            if smtp_username and smtp_password:
                smtp.login(smtp_username, smtp_password)
            smtp.send_message(msg)
        return

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
        smtp.set_debuglevel(1)
        if use_starttls:
            smtp.starttls()
        if smtp_username and smtp_password:
            smtp.login(smtp_username, smtp_password)
        smtp.send_message(msg)


def compute_items_hash(items: List[SecurityNewsItem]) -> str:
    normalized = json.dumps([asdict(item) for item in items], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_item_key(item: SecurityNewsItem) -> str:
    payload = f"{item.timestamp}|{item.media}|{item.title}|{item.url}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def filter_unsent_items(items: List[SecurityNewsItem], sent_keys: set[str]) -> List[SecurityNewsItem]:
    return [item for item in items if build_item_key(item) not in sent_keys]


def _load_state(state_file: str) -> dict[str, Any]:
    path = Path(state_file)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state_file: str, data: dict[str, Any]) -> None:
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_last_hash(state_file: str) -> str:
    data = _load_state(state_file)
    return str(data.get("last_hash", ""))


def save_last_hash(state_file: str, value: str) -> None:
    data = _load_state(state_file)
    data["last_hash"] = value
    _save_state(state_file, data)


def load_sent_item_keys(state_file: str) -> set[str]:
    data = _load_state(state_file)
    raw = data.get("sent_item_keys", [])
    if not isinstance(raw, list):
        return set()
    return {str(value) for value in raw}


def save_sent_item_keys(state_file: str, keys: set[str]) -> None:
    data = _load_state(state_file)
    data["sent_item_keys"] = sorted(keys)
    _save_state(state_file, data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and parse security news trends.")
    parser.add_argument("--url", default=TARGET_URL)
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument("--previous-day-all", action="store_true")
    parser.add_argument("--output", choices=("text", "json"), default="text")
    parser.add_argument("--connect-timeout", type=float, default=8.0)
    parser.add_argument("--read-timeout", type=float, default=25.0)
    parser.add_argument("--fail-soft", action="store_true")
    parser.add_argument("--notify-email", action="store_true")
    parser.add_argument("--suppress-duplicate", action="store_true")
    parser.add_argument("--notify-only-new", action="store_true")
    parser.add_argument("--state-file", default=".state/last_notification.json")
    parser.add_argument("--evaluate-with-groq", action="store_true")
    parser.add_argument("--evaluate-with-google-studio", action="store_true")
    parser.add_argument("--relevance-threshold", type=float, default=0.9)
    parser.add_argument("--groq-api-base", default="https://api.groq.com/openai/v1")
    parser.add_argument("--groq-api-key-env", default="GROQ_API_KEY")
    parser.add_argument("--groq-model", default="llama-3.3-70b-versatile")
    parser.add_argument("--google-api-base", default="https://generativelanguage.googleapis.com/v1beta")
    parser.add_argument("--google-api-key-env", default="GOOGLE_API_KEY")
    parser.add_argument("--google-model", default="gemini-2.5-flash")
    args = parser.parse_args()

    try:
        source_urls = [args.url, *TARGET_FEED_URLS]
        items = collect_items_from_sources(
            primary_url=args.url,
            feed_urls=TARGET_FEED_URLS,
            timeout=(args.connect_timeout, args.read_timeout),
            fail_soft=args.fail_soft,
        )
        if args.previous_day_all:
            items = filter_previous_day_from_latest(items)
        elif args.latest_only:
            items = filter_latest_timestamp(items)

        total_items_before_relevance = len(items)

        relevance_map: dict[str, RelevanceResult] = {}
        evaluated_items_count = 0
        relevance_threshold_applied: float | None = None
        if args.evaluate_with_groq and args.evaluate_with_google_studio:
            print("[evaluate-warn] both providers requested; using groq", file=sys.stderr)

        if args.evaluate_with_groq:
            groq_api_key = os.getenv(args.groq_api_key_env, "").strip()
            if not groq_api_key:
                print(
                    f"[evaluate-skip] env var '{args.groq_api_key_env}' is not set",
                    file=sys.stderr,
                )
            else:
                relevance_map = evaluate_items_with_groq_sequential(
                    items,
                    api_base=args.groq_api_base,
                    api_key=groq_api_key,
                    model=args.groq_model,
                    timeout=(args.connect_timeout, args.read_timeout),
                )
                evaluated_items_count = len(items)
                relevance_threshold_applied = args.relevance_threshold

                items = [
                    item
                    for item in items
                    if relevance_map.get(
                        build_item_key(item),
                        RelevanceResult(0.0, "", ""),
                    ).score
                    >= args.relevance_threshold
                ]
        elif args.evaluate_with_google_studio:
            google_api_key = os.getenv(args.google_api_key_env, "").strip()
            if not google_api_key:
                print(
                    f"[evaluate-skip] env var '{args.google_api_key_env}' is not set",
                    file=sys.stderr,
                )
            else:
                relevance_map = evaluate_items_with_google_studio_sequential(
                    items,
                    api_base=args.google_api_base,
                    api_key=google_api_key,
                    model=args.google_model,
                    timeout=(args.connect_timeout, args.read_timeout),
                )
                evaluated_items_count = len(items)
                relevance_threshold_applied = args.relevance_threshold

                items = [
                    item
                    for item in items
                    if relevance_map.get(
                        build_item_key(item),
                        RelevanceResult(0.0, "", ""),
                    ).score
                    >= args.relevance_threshold
                ]

        if args.output == "json":
            print(json.dumps(serialize_items(items, relevance_map), ensure_ascii=False, indent=2))
        else:
            print(format_text_with_relevance(items, relevance_map))

        if args.notify_email:
            smtp_host = os.getenv("SMTP_HOST", "").strip()
            smtp_port = int(os.getenv("SMTP_PORT", "587"))
            smtp_username = os.getenv("SMTP_USERNAME", "").strip()
            smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
            email_from = os.getenv("EMAIL_FROM", "").strip()
            email_to_raw = os.getenv("EMAIL_TO", "").strip()
            use_ssl = _truthy(os.getenv("SMTP_SSL"), default=False)
            use_starttls = _truthy(os.getenv("SMTP_STARTTLS"), default=True)

            email_to = [addr.strip() for addr in email_to_raw.split(",") if addr.strip()]
            missing = []
            if not smtp_host:
                missing.append("SMTP_HOST")
            if not email_from:
                missing.append("EMAIL_FROM")
            if not email_to:
                missing.append("EMAIL_TO")

            if missing:
                print(
                    f"[notify-skip] missing env vars: {', '.join(missing)}",
                    file=sys.stderr,
                )
            else:
                notification_items = items
                if args.notify_only_new:
                    sent_keys = load_sent_item_keys(args.state_file)
                    notification_items = filter_unsent_items(items, sent_keys)
                    if not notification_items:
                        print("[notify-skip] no new items", file=sys.stderr)
                        return 0

                text = build_notification_text(
                    notification_items,
                    ", ".join(source_urls),
                    relevance_map,
                    total_items=total_items_before_relevance,
                    evaluated_items=evaluated_items_count,
                    threshold=relevance_threshold_applied,
                )
                timestamp = notification_items[0].timestamp if notification_items else "no-items"
                current_hash = compute_items_hash(notification_items)
                if args.suppress_duplicate:
                    last_hash = load_last_hash(args.state_file)
                    if last_hash and last_hash == current_hash:
                        print("[notify-skip] duplicate content", file=sys.stderr)
                        return 0

                send_email_notification(
                    subject=f"Security Update ({timestamp})",
                    body=text,
                    smtp_host=smtp_host,
                    smtp_port=smtp_port,
                    smtp_username=smtp_username,
                    smtp_password=smtp_password,
                    email_from=email_from,
                    email_to=email_to,
                    use_ssl=use_ssl,
                    use_starttls=use_starttls,
                )

                if args.notify_only_new:
                    current_sent_keys = load_sent_item_keys(args.state_file)
                    current_sent_keys.update(build_item_key(item) for item in notification_items)
                    save_sent_item_keys(args.state_file, current_sent_keys)
                if args.suppress_duplicate:
                    save_last_hash(args.state_file, current_hash)
                print("[notify-ok] email notification sent", file=sys.stderr)
        return 0
    except requests.RequestException as exc:
        print(f"[fetch-error] {exc}", file=sys.stderr)
        return 0 if args.fail_soft else 1
    except Exception as exc:  # noqa: BLE001
        print(f"[unexpected-error] {exc}", file=sys.stderr)
        return 0 if args.fail_soft else 1


if __name__ == "__main__":
    raise SystemExit(main())

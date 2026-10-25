"""Read-only reference collection in an already-owned browser context.

No browser launch, credential exports, upload, publication, or challenge retry.
The caller retains the context and all of its existing tabs.
"""
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .sources import (
    CollectionResult, _LINK_ROWS, _XHS_STATE, _now, _page_block, _rows,
    deduplicate_references, normalize_reference,
)


def _observed_block(text, url):
    # A normal SMS login dialog contains “获取验证码”; it is not a CAPTCHA.
    cleaned = text.replace("获取验证码", "").replace("发送验证码", "").replace("验证码登录", "")
    block = _page_block(cleaned, url)
    if block and block[0] == "login_required":
        return "login_required", "内容搜索要求单独登录；已停止参考采集，不要求用户为参考资料重新登录。"
    return block


def collect_xhs_in_context(context, queries, output_path, *, target_count=60,
                           max_pages=6, topic_keywords=()):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = CollectionResult(platform="xiaohongshu", target_count=target_count, days=7)
    page = context.new_page()
    captured = []
    active_query = ""

    def observe(response):
        path = urlsplit(response.url).path
        if not any(key in path for key in ("/search/notes", "/api/sns/web/v1/feed")):
            return
        record = {"path": path, "http_status": response.status, "observed_at": _now()}
        result.attempts[-1].setdefault("observed_responses", []).append(record)
        if response.status in {401, 403, 429, 461, 471}:
            captured.append((active_query, {"_blocked_status": response.status}))
        elif response.status == 200 and "json" in response.headers.get("content-type", ""):
            try:
                captured.append((active_query, response.json()))
            except Exception:
                pass

    def save():
        import json
        result.samples = deduplicate_references(result.samples)
        valid_count_7_days = result.valid_count
        reevaluated_existing_samples = False
        if result.status == "insufficient_samples":
            if result.valid_count < 50:
                result.days = 30
                reevaluated_existing_samples = True
            if result.valid_count >= target_count:
                result.status = "ok"
            result.reason = (f"取得 {len(result.samples)} 条真实参考，最近 {result.days} 天内"
                             f"有日期视频 {result.valid_count} 条；目标 {target_count}，最低参考数 50。")
        data = result.as_dict()
        data["window_evaluation"] = {
            "requested_search_window_days": 7,
            "evaluated_window_days": result.days,
            "valid_count_7_days": valid_count_7_days,
            "reevaluated_existing_samples": reevaluated_existing_samples,
            "additional_requests_for_expansion": False,
        }
        if reevaluated_existing_samples:
            data["warnings"].append("30 天窗口仅重评同批真实有日期样本，没有新增 30 天搜索请求。")
        data["topic_keywords"] = list(topic_keywords)
        data["collection_scope"] = "existing_dedicated_context_read_only_search"
        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    page.on("response", observe)
    try:
        for query in queries:
            active_query = query
            url = "https://www.xiaohongshu.com/search_result?" + urlencode(
                {"keyword": query, "source": "web_explore_feed", "type": 51})
            attempt = {"adapter": "existing_context_browser", "query": query, "url": url,
                       "started_at": _now(), "sort": "unverified", "requested_window_days": 7}
            result.attempts.append(attempt)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=25000)
                attempt["http_status"] = response.status if response else None
                page.wait_for_timeout(2500)
                previous = -1
                for batch in range(max_pages):
                    text = page.title() + "\n" + page.locator("body").inner_text(timeout=5000)
                    block = _observed_block(text, page.url)
                    blocked_status = next((payload["_blocked_status"] for _, payload in captured
                                           if isinstance(payload, dict) and "_blocked_status" in payload), None)
                    if block or blocked_status:
                        if block:
                            result.status, result.reason = block
                        else:
                            result.status = "login_required" if blocked_status == 401 else "rate_limited"
                            result.reason = f"正常页面搜索请求返回 HTTP {blocked_status}；已停止，不重登或绕过。"
                        attempt.update(status=result.status, finished_at=_now(),
                                       final_route=urlsplit(page.url)._replace(query="", fragment="").geturl(),
                                       page_title=page.title())
                        screenshot = output_path.with_name("xiaohongshu_blocked.png")
                        page.screenshot(path=str(screenshot), full_page=False, timeout=10000)
                        attempt["screenshot"] = str(screenshot)
                        return result, page
                    rows = list(page.evaluate(_XHS_STATE)) + list(page.evaluate(_LINK_ROWS, "xiaohongshu"))
                    sources = [(query, row, "browser_visible_state") for row in rows]
                    for captured_query, payload in captured:
                        sources.extend((captured_query, row, "browser_response") for row in _rows(payload))
                    captured.clear()
                    for source_query, row, source_type in sources:
                        sample = normalize_reference(row, "xiaohongshu", query=source_query,
                                                     source_type=source_type, days=7)
                        if sample:
                            content = sample.title + " " + sample.description + " " + sample.caption
                            if topic_keywords and not any(word.lower() in content.lower() for word in topic_keywords):
                                continue
                            result.samples.append(sample)
                    result.samples = deduplicate_references(result.samples)
                    attempt.update(scroll_batches=batch + 1, sample_count=len(result.samples),
                                   dated_video_count_7_days=result.valid_count)
                    if result.valid_count >= target_count:
                        return result, page
                    if previous == len(result.samples):
                        break
                    previous = len(result.samples)
                    page.mouse.wheel(0, 900)
                    page.wait_for_timeout(1500)
                attempt.update(status="collected" if result.samples else "no_results", finished_at=_now())
            except Exception as exc:
                attempt.update(status="page_error", error_type=type(exc).__name__, finished_at=_now())
                try:
                    block = _observed_block(page.title() + "\n" + page.locator("body").inner_text(timeout=2000), page.url)
                except Exception:
                    block = None
                if block:
                    result.status, result.reason = block
                else:
                    result.status, result.reason = "network_timeout", "平台搜索未完成，未取得可核验参考；停止重试。"
                return result, page
    finally:
        page.remove_listener("response", observe)
        result.warnings.append("无日期、非视频或超出窗口的条目不计近期有效；排序未经核验，不代表近期热榜。")
        save()
    return result, page


def record_shared_xhs_block(previous, previous_path, queries, output_path):
    """Record a second topic honestly without repeating a live challenge request."""
    import json
    result = CollectionResult(platform="xiaohongshu", target_count=60, days=7,
                              status=previous.status,
                              reason="同一专用会话刚出现登录、验证或访问限制；为第二主题记录共享阻塞，未重复请求或绕过。")
    result.attempts.append({"adapter": "existing_context_shared_block", "recorded_at": _now(),
                            "query": list(queries)[0], "planned_queries": list(queries),
                            "navigation_attempted": False, "status": previous.status,
                            "source_collection": str(previous_path),
                            "source_attempt": previous.attempts[-1] if previous.attempts else None})
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result

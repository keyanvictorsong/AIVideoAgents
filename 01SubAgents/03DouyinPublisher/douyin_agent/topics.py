"""Select related topics from the creator page's own search suggestions.

Only normal editor keyboard events and suggestion clicks change content.  A
temporary query is removed only after proving that it is our exact appended
suffix and that the existing text and mention entities are unchanged.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class TopicSelectionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


EDITOR = '[contenteditable="true"][data-slate-editor="true"]'
ROWS = '.mention-suggest-mount-dom .tag-hash-o0tpyE'
NAME = '.tag-hash-view-name-DwMEe8'
COUNT = '.tag-hash-view-count-juN_70'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _name(value: Any) -> str:
    return str(value).strip().lstrip('#').strip()


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError('topic_selection 必须是对象。')
    normalized = dict(settings)
    for key in ('queries', 'keywords', 'allowed_names', 'exclude_keywords'):
        values = settings.get(key, [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError(f'topic_selection.{key} 必须是字符串列表。')
        normalized[key] = list(dict.fromkeys(_name(item) for item in values if _name(item)))
    target = settings.get('target_count', 5)
    if isinstance(target, bool) or not isinstance(target, int) or target < 1 or target > 100:
        raise ValueError('topic_selection.target_count 必须是 1–100 的整数；实际仍以平台可见上限为准。')
    normalized['target_count'] = target
    normalized['enabled'] = bool(settings.get('enabled', False))
    if normalized['enabled'] and not normalized['queries']:
        raise ValueError('启用话题选择时必须提供 queries。')
    return normalized


def parse_displayed_count(value: str) -> tuple[float | None, str | None]:
    """Parse only the count element; an unlabeled number stays unlabeled."""
    match = re.fullmatch(
        r'\s*(?:(热度|播放量|播放|浏览量|浏览|观看量|参与人数)\s*[:：]?\s*)?'
        r'([\d,]+(?:\.\d+)?)\s*([千万亿]?)\s*(?:次|人)?\s*', value,
    )
    if not match:
        return None, None
    try:
        number = Decimal(match.group(2).replace(',', ''))
    except InvalidOperation:
        return None, None
    return float(number * {'': 1, '千': 1000, '万': 10000, '亿': 100000000}[match.group(3)]), match.group(1)


def rank_candidates(candidates: list[dict[str, Any]], settings: dict[str, Any],
                    existing: list[str]) -> list[dict[str, Any]]:
    settings = validate_settings(settings)
    keywords = settings['keywords'] or settings['queries']
    allowed = set(settings['allowed_names'])
    excluded = set(existing)
    result: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        name = _name(candidate['name'])
        if name in excluded or (allowed and name not in allowed):
            continue
        if any(term.casefold() in name.casefold() for term in settings['exclude_keywords']):
            continue
        relevant = [term for term in keywords if term.casefold() in name.casefold()]
        if not relevant:
            continue
        item = dict(candidate, name=name, matched_keywords=relevant)
        previous = result.get(name)
        if previous is None:
            result[name] = item
    items = list(result.values())
    # Different metric labels cannot be compared.  Missing values never become
    # invented zero counts or evidence of popularity.
    labels = {item.get('metric_label') for item in items if item.get('count_value') is not None}
    comparable = len(labels) <= 1
    for item in items:
        item['selection_basis'] = ('相关名称；同类搜索行的页面显示数值优先，未证明近期热度'
                                   if comparable and item.get('count_value') is not None
                                   else '相关名称；没有可比较的页面数值，沿用查询结果顺序')
    if comparable:
        items.sort(key=lambda item: (item.get('count_value') is not None, item.get('count_value') or 0), reverse=True)
    return items


# One definition of semantic text for snapshots and range offsets.  Remove only
# Slate's exact empty marker, never zero-width characters in user-authored text.
_SNAPSHOT = r'''el => {
    const skipped = node => node.parentElement?.matches('span[data-string="true"][data-enter="true"], span[data-zero-space="true"]')
        && node.parentElement.childElementCount === 0 && node.textContent === '\u200b';
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let text = '', node;
    while ((node = walker.nextNode())) { if (!skipped(node)) text += node.textContent; }
    return {text, entities: Array.from(el.querySelectorAll('[data-mention="#"]'))
        .map(item => (item.textContent || '').trim().replace(/^#/, '').trim())};
}'''


def editor_snapshot(editor: Any) -> dict[str, Any]:
    return editor.evaluate(_SNAPSHOT)


class _Selector:
    def __init__(self, page: Any, settings: dict[str, Any], report: dict[str, Any],
                 editor_selector: str, timeout_seconds: float):
        self.page = page
        self.settings = validate_settings(settings)
        self.report = report
        self.editor = page.locator(editor_selector)
        if self.editor.count() != 1 or not self.editor.is_visible():
            raise TopicSelectionError('topic_editor_ambiguous', '无法唯一识别话题编辑框；未修改内容。')
        self.timeout = max(0.1, min(float(timeout_seconds), 10))
        self.pending: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        return editor_snapshot(self.editor)

    def search(self, query: str) -> list[dict[str, Any]]:
        before = self.snapshot()
        suffix = ' #' + query
        if len((before['text'] + suffix).encode('utf-16-le')) // 2 > 1000:
            raise TopicSelectionError('topic_caption_too_long', '追加话题查询会超过简介限制；未修改内容。')
        self.editor.evaluate('''el => {
            el.focus(); const range = document.createRange(); range.selectNodeContents(el);
            range.collapse(false); const selection = window.getSelection();
            selection.removeAllRanges(); selection.addRange(range);
        }''')
        self.pending = {'before': before, 'suffix': suffix, 'query': query}
        self.report['pending_query'] = query
        self.page.keyboard.type(' #')
        self.page.keyboard.insert_text(query)
        self._assert_pending()
        deadline = time.monotonic() + self.timeout
        previous = None
        stable = 0
        found: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            found = []
            for row in self.page.locator(ROWS).all():
                if not row.is_visible():
                    continue
                names = row.locator(NAME)
                counts = row.locator(COUNT)
                if names.count() != 1:
                    continue
                name = _name(names.inner_text())
                # Ignore still-rendered results from a previous query.
                if query.casefold() not in name.casefold():
                    continue
                raw = counts.inner_text().strip() if counts.count() == 1 else ''
                value, label = parse_displayed_count(raw)
                found.append({'name': name, 'query': query, 'platform_displayed_count': raw or None,
                              'count_value': value, 'metric_label': label, 'collected_at': _now(),
                              'source': 'creator_topic_search', 'time_window': None})
            signature = [(item['name'], item['platform_displayed_count']) for item in found]
            stable = stable + 1 if signature == previous and signature else 0
            previous = signature
            if stable >= 2:
                break
            self.page.wait_for_timeout(100)
        return found

    def _assert_pending(self) -> None:
        if self.pending is None:
            raise TopicSelectionError('topic_query_missing', '没有可核验的临时查询。')
        current = self.snapshot()
        before = self.pending['before']
        if current != dict(before, text=before['text'] + self.pending['suffix']):
            raise TopicSelectionError('topic_query_changed', '编辑框出现额外变化；保留现有内容并停止，不删除任何不明文字。')

    def rollback(self) -> None:
        self._assert_pending()
        pending = self.pending
        assert pending is not None
        # Select only the proven suffix.  Normal Backspace lets Slate update its
        # model; assigning innerHTML/textContent or fill() would destroy entities.
        self.editor.evaluate(r'''(el, offsets) => {
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            const pieces = []; let node, offset = 0;
            while ((node = walker.nextNode())) {
                if (node.parentElement?.matches('span[data-string="true"][data-enter="true"], span[data-zero-space="true"]')
                    && node.parentElement.childElementCount === 0 && node.textContent === '\u200b') continue;
                pieces.push({node, start: offset, end: offset + node.textContent.length});
                offset += node.textContent.length;
            }
            const locate = n => {
                const piece = pieces.find(item => item.end > n && item.start <= n)
                    || pieces.findLast(item => item.end === n);
                if (!piece) throw Error('Cannot identify appended query range');
                return [piece.node, n - piece.start];
            };
            const [first, start] = locate(offsets[0]), [last, end] = locate(offsets[1]);
            el.focus(); const range = document.createRange(); range.setStart(first, start); range.setEnd(last, end);
            if (Array.from(el.querySelectorAll('[data-mention="#"]')).some(item => range.intersectsNode(item)))
                throw Error('Refusing a query range that touches an existing mention');
            if (range.toString() !== offsets[2]) throw Error('Query selection differs from proven suffix');
            const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        }''', [len(pending['before']['text'].encode('utf-16-le')) // 2,
                len((pending['before']['text'] + pending['suffix']).encode('utf-16-le')) // 2,
                pending['suffix']])
        pending['rollback_attempted'] = True
        self.page.keyboard.press('Backspace')
        if self.snapshot() != pending['before']:
            raise TopicSelectionError('topic_query_rollback_failed', '临时查询回退后内容不一致；已停止，不能继续追加话题。')
        self.page.keyboard.press('Escape')
        self.pending = None
        self.report.pop('pending_query', None)

    def select(self, candidate: dict[str, Any]) -> None:
        self._assert_pending()
        before = self.pending['before']
        matches = []
        for row in self.page.locator(ROWS).all():
            names = row.locator(NAME)
            if row.is_visible() and names.count() == 1 and _name(names.inner_text()) == candidate['name']:
                matches.append(row)
        if len(matches) != 1:
            raise TopicSelectionError('topic_candidate_changed', '目标话题搜索行不再唯一；未点击。')
        matches[0].click()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            current = self.snapshot()
            if current['entities'] == before['entities'] + [candidate['name']]:
                tail = current['text'][len(before['text']):] if current['text'].startswith(before['text']) else None
                if tail is not None and re.sub(r'\s', '', tail) == '#' + candidate['name']:
                    self.pending = None
                    self.report.pop('pending_query', None)
                    return
            self.page.wait_for_timeout(100)
        # A click is not success.  If the query is unchanged it can be safely
        # removed; any other mutation is retained for review and stops the run.
        self.rollback()
        raise TopicSelectionError('topic_entity_not_confirmed', '点击后未出现对应真实话题实体；未将其记作成功。')

    def run(self) -> dict[str, Any]:
        before = self.snapshot()
        report = self.report
        report.update(started_at=_now(), existing_topics=before['entities'], candidates=[], selected=[],
                      target_count=self.settings['target_count'], status='collecting')
        visible_limits = re.findall(r'(?:最多|至多)(?:可)?(?:添加|选择)?\s*(\d+)\s*个话题', self.page.locator('body').inner_text())
        visible_limit = min(map(int, visible_limits)) if visible_limits else None
        target = min(self.settings['target_count'], visible_limit) if visible_limit is not None else self.settings['target_count']
        report['platform_visible_limit'] = visible_limit
        report['effective_target_count'] = target
        if len(before['entities']) < target:
            for query in self.settings['queries']:
                rows = self.search(query)
                report['candidates'].extend(rows)
                self.rollback()
            plain_tags = re.findall(r'#([^\s#]+)', before['text'])
            ranked = rank_candidates(report['candidates'], self.settings, before['entities'] + plain_tags)
            for candidate in ranked:
                if len(self.snapshot()['entities']) >= target:
                    break
                self.search(candidate['query'])
                self.select(candidate)
                report['selected'].append(candidate)
        final = self.snapshot()
        report.update(status='complete', finished_at=_now(), actual_topics=final['entities'],
                      actual_caption=final['text'], added_count=len(report['selected']))
        if len(final['entities']) < target:
            report['warning'] = '相关候选不足；实际话题少于目标数量，未用无关话题补足。'
        report['metric_note'] = '仅记录平台搜索行显示的数值及原文；未标注的指标和时间窗保持未知，不能视为近期热榜。'
        return report


def select_topics(page: Any, settings: dict[str, Any], *, report: dict[str, Any] | None = None,
                  editor_selector: str = EDITOR, timeout_seconds: float = 3) -> dict[str, Any]:
    report = report if report is not None else {}
    worker = _Selector(page, settings, report, editor_selector, timeout_seconds)
    try:
        return worker.run()
    except Exception as exc:
        # Do not hide cleanup failures or guess at content if a user edited it.
        if worker.pending is not None and not worker.pending.get('rollback_attempted'):
            try:
                worker.rollback()
            except Exception as cleanup:
                report.update(status='failed', error=str(cleanup))
                raise cleanup from exc
        report.update(status='failed', error=str(exc))
        raise

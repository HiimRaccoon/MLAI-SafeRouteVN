"""Local Ollama prose generated from certified comparison metrics.

Only generated text is displayed; unavailable AI never becomes a canned review.
One generation covers all profiles; cached by immutable evidence/model/prompt.
"""
import asyncio
from contextlib import closing
import hashlib
import json
import re
import sqlite3
import time

import httpx

from .comparison_narrative import comparison_narrative

PROFILES = ('FASTEST', 'BALANCED', 'SAFER')
ALIASES = dict(zip(('A', 'B', 'C'), PROFILES))
VERSION = 'qwen-prose/4-numeric-citations'
LIMITATIONS = 'Nhận xét do AI tạo từ kết quả mô phỏng; cần đối chiếu số liệu trên thẻ. Exposure là chỉ số proxy, không phải xác suất tai nạn.'


def evidence(narrative):
    if narrative['comparison_status'] != 'COMPARABLE':
        return {}
    rows = narrative['profiles']
    return {r['profile']: {'served': r['served_count'], 'unserved': r['unserved_count'],
        'minutes': round(r['metrics']['total_travel_time_s']/60, 3),
        'km': round(r['metrics']['total_distance_m']/1000, 3),
        'cost_vnd': round(r['metrics']['total_cost_vnd'], 3),
        'exposure_proxy': round(r['metrics']['total_exposure'], 3),
        'late_seconds': round(r['metrics']['total_soft_lateness_s'], 3)} for r in rows}


def citations(facts):
    """Model cites immutable facts; only the server inserts numerical values/units."""
    units = {'minutes': 'phút', 'cost_vnd': 'VND', 'exposure_proxy': 'proxy',
             'km': 'km', 'served': 'đơn', 'unserved': 'đơn', 'late_seconds': 'giây'}
    return {f'{alias}.{metric}': f'{value:,.3f}'.rstrip('0').rstrip('.') + ' ' + units[metric]
            for alias, profile in ALIASES.items() for metric, value in facts[profile].items()}


def explanation_prompt(facts):
    return ('Dữ liệu Optimize đã kiểm chứng (mã tham chiếu: giá trị):\n'
            + json.dumps(citations(facts), ensure_ascii=False)
            + '\nSo sánh đã kiểm chứng: ' + json.dumps(qualitative_facts(facts), ensure_ascii=False)
            + '\nViết mỗi phương án tối đa hai câu tiếng Việt ngắn, nêu số liệu và đánh đổi. '
            'Mỗi nhận xét bắt buộc trích ít nhất hai mã của chính phương án từ minutes, cost_vnd, exposure_proxy. '
            'Chèn mã trong ngoặc nhọn, ví dụ: Thời gian {A.minutes}, chi phí {A.cost_vnd}. '
            'Giữ nguyên mã; hệ thống sẽ thay bằng số và đơn vị. Không tự viết chữ số hoặc tính số mới. '
            'Nếu số liệu bằng nhau, nói chưa có khác biệt; không suy đoán nguyên nhân hoặc ưu điểm. '
            'Exposure proxy không phải xác suất tai nạn. Trả JSON với khóa A, B, C. /no_think')


def render_prose(prose, alias, facts):
    if (not isinstance(prose, str) or not 15 <= len(prose.strip()) <= 450
            or any(c.isdigit() for c in prose) or '<' in prose or '>' in prose):
        raise ValueError('Invalid explanation prose')
    refs = re.findall(r'\{([^{}]+)\}', prose)
    values = citations(facts)
    required = {f'{alias}.{m}' for m in ('minutes', 'cost_vnd', 'exposure_proxy')}
    if len(set(refs) & required) < 2 or any(ref not in values for ref in refs):
        raise ValueError('Missing or unknown metric citations')
    # Reject stray syntax rather than showing unresolved references to the user.
    remainder = re.sub(r'\{[^{}]+\}', '', prose)
    if '{' in remainder or '}' in remainder:
        raise ValueError('Malformed metric citation')
    # Keep at most two generated, evidence-bearing sentences. Other profiles are
    # named rather than repeating their numbers, keeping the selected review short.
    chosen, covered = [], set()
    for sentence in re.split(r'(?<=[.!?])\s+', prose.strip()):
        own = required.intersection(re.findall(r'\{([^{}]+)\}', sentence))
        if own - covered:
            chosen.append(sentence)
            covered.update(own)
        if len(chosen) == 2:
            break
    prose = ' '.join(chosen)
    refs = re.findall(r'\{([^{}]+)\}', prose)
    if len(set(refs) & required) < 2:
        raise ValueError('Concise review must cite two selected metrics')
    prose = re.sub(r'\{([^{}]+)\}', lambda m: values[m[1]] if m[1].startswith(alias + '.') else ALIASES[m[1][0]], prose)
    prose = re.sub(r'\b[ABC]\b', lambda m: ALIASES[m[0]], prose)
    return {'fact_ids': list(dict.fromkeys(ALIASES[ref[0]] + ref[1:] for ref in refs)),
            'sentences': [prose]}


def qualitative_facts(facts):
    """Compute comparisons on the server; Qwen writes the assessment itself."""
    if all(f==facts['FASTEST'] for f in facts.values()):
        f=facts['FASTEST']
        return ('Cả ba phương án A, B, C có cùng thời gian, cùng chi phí, cùng exposure proxy và cùng số đơn chưa phục vụ. '
                + ('Đều phục vụ đủ đơn. ' if f['unserved']==0 else 'Đều còn đơn chưa phục vụ. ')
                + ('Đều có độ trễ mềm.' if f['late_seconds']>0 else 'Đều không có độ trễ mềm.'))
    result = {}
    for alias,p in ALIASES.items():
        f = facts[p]
        lines = ['Phục vụ đủ đơn.' if f['unserved']==0 else 'Còn đơn chưa được phục vụ.',
                 'Có độ trễ mềm.' if f['late_seconds']>0 else 'Không có độ trễ mềm.']
        for metric,label in (('minutes','Thời gian'),('exposure_proxy','Exposure proxy'),('cost_vnd','Chi phí'),('unserved','Số đơn chưa phục vụ')):
            comparisons={}
            for other,q in ALIASES.items():
                if p==q:continue
                relation = 'bằng' if f[metric]==facts[q][metric] else 'thấp hơn' if f[metric]<facts[q][metric] else 'cao hơn'
                comparisons.setdefault(relation,[]).append(other)
            lines.extend(f'{label} {relation} {", ".join(others)}.' for relation,others in comparisons.items())
        result[alias] = ' '.join(lines)
    return result


class ExplainService:
    def __init__(self, settings):
        self.settings = settings
        self.tasks = {}
        self.gate = asyncio.Semaphore(1)

    def db(self):
        db = sqlite3.connect(self.settings.metadata_path, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS llm_explanations(cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL NOT NULL)')
        return db

    def save(self, key, payload):
        with closing(self.db()) as db, db:
            db.execute('INSERT OR REPLACE INTO llm_explanations VALUES(?,?,?)', (key, json.dumps(payload, ensure_ascii=False), time.time()))

    def prepare(self, group):
        n = comparison_narrative(group, build=self.settings.installation()['expected_build_sha256'])
        facts = evidence(n)
        binding = {'session_id': n['session_id'], 'comparison_id': n['comparison_id'],
                   'basis': n['captured_group_basis'], 'model': self.settings.ollama_model}
        initial = {**binding, 'schema_version': 'saferoute-explain/1', 'status': 'WAITING' if not facts else 'PENDING',
                   'profiles': {}, 'limitations': LIMITATIONS, 'message': n['text'] if not facts else 'Đang chuẩn bị giải thích bằng Qwen3…'}
        if not facts:
            return initial
        key = hashlib.sha256(json.dumps([VERSION, binding, facts], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with closing(self.db()) as db:
            row = db.execute('SELECT payload,updated FROM llm_explanations WHERE cache_key=?', (key,)).fetchone()
        if row:
            saved = json.loads(row[0])
            if saved['status'] == 'READY' or time.time() - row[1] < 60:
                return saved
        if not self.settings.ollama_enabled:
            return {**initial, 'status': 'FALLBACK', 'message': 'Qwen đang tắt; chưa có đánh giá AI.'}
        if key not in self.tasks:
            if len(self.tasks) >= 4:
                return {**initial, 'status': 'FALLBACK', 'message': 'Qwen đang bận; chưa có đánh giá AI.'}
            task = asyncio.create_task(self.generate(key, initial, facts))
            self.tasks[key] = task
            task.add_done_callback(lambda done: self.tasks.pop(key, None))
        return initial

    async def generate(self, key, initial, facts):
        try:
            # Wall-clock timeout includes queue wait and generation, not just
            # individual HTTP reads. A slow model cannot create an endless spinner.
            async with asyncio.timeout(self.settings.ollama_timeout_seconds), self.gate:
                schema = {'type': 'object', 'additionalProperties': False, 'required': list(ALIASES),
                          'properties': {p: {'type': 'string'} for p in ALIASES}}
                async with httpx.AsyncClient(timeout=self.settings.ollama_timeout_seconds, trust_env=False, follow_redirects=False) as client:
                    # Neutral aliases avoid the model inventing an advantage
                    # just because a profile happens to be named FASTEST/SAFER.
                    prompt = explanation_prompt(facts)
                    response = await client.post(self.settings.ollama_url + '/api/generate', json={
                        'model': self.settings.ollama_model, 'stream': False, 'think': False,
                        'keep_alive': '30m', 'format': schema,
                        'options': {'temperature': 0, 'num_predict': 360, 'num_ctx': 4096},
                        'prompt': prompt})
                    response.raise_for_status()
                    result = response.json()
                    if result.get('done') is not True or result.get('done_reason') == 'length':
                        raise ValueError('Incomplete generation')
                    selected = json.loads(result['response'])
                if type(selected) is not dict or set(selected) != set(ALIASES):
                    raise ValueError('Profile set differs')
                rendered = {}
                for alias,p in ALIASES.items():
                    rendered[p] = render_prose(selected[alias], alias, facts)
                self.save(key, {**initial, 'status': 'READY', 'profiles': rendered,
                                'message': 'Đánh giá do Qwen tạo từ số liệu của ba phương án.'})
        except asyncio.CancelledError:
            raise
        except Exception:
            # Optional AI must never take down routing. No provider response/secret in UI.
            try:
                self.save(key, {**initial, 'status': 'FALLBACK', 'message': 'Qwen chưa trả được đánh giá trong thời hạn hoặc phản hồi không hợp lệ.'})
            except (OSError, sqlite3.Error):
                pass  # Cache/storage failure remains isolated from routing.

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

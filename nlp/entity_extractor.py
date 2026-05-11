"""
nlp/entity_extractor.py — 룰 기반 엔티티 추출기
"""

import re
from dataclasses import dataclass, field

# ── 정규식 ─────────────────────────────────────────────────────────
IP_RE = re.compile(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b')

# \b 대신 ASCII 경계 사용 — 한국어 조사('에서','의') 앞에서도 정확히 추출
HOSTNAME_FULL_RE = re.compile(
    r'(?<![a-zA-Z0-9_])'
    r'((?:web|was|db|app|proxy|lb|cache|mq|api|auth)\d*[-_]\w[\w-]*)'
    r'(?![a-zA-Z0-9_])',
    re.IGNORECASE,
)
HOSTNAME_SHORT_RE = re.compile(
    r'(?<![a-zA-Z0-9_])'
    r'(web\d+|was\d+|db\d+|app\d+|proxy\d+|lb\d+|cache\d*|mq\d*|api\d*|auth\d+)'
    r'(?![a-zA-Z0-9_])',
    re.IGNORECASE,
)

HTTP_ERROR_RE = re.compile(r'\b([45]\d{2})\b')
MYSQL_ERROR_RE = re.compile(r'\b(1\d{3}|20\d{2})\b')

METRIC_NAME_RE = re.compile(
    r'\b(CPU|메모리|memory|heap|힙|disk|디스크|'
    r'threads?|쓰레드|GC|connection|연결\s*수|'
    r'요청|request|응답\s*시간|latency|TPS|QPS)\b',
    re.IGNORECASE,
)

TIME_KEYWORD_RE = re.compile(
    r'어제|오늘|그제|지난\s*주|이번\s*주|최근|'
    r'\d+\s*(?:시간|분|일)\s*(?:전|동안)|'
    r'\d{1,2}월\s*\d{1,2}일|\d{4}-\d{2}-\d{2}'
)

KEYWORD_PATTERNS = {
    'OOM':                r'OOM|OutOfMemory|java\.lang\.OutOfMemory|메모리\s*부족|힙\s*초과|heap\s*space|heap\s*부족',
    'GC_OVERHEAD':        r'GC\s*overhead|garbage\s*collect|가비지\s*컬렉션',
    'SLOW_QUERY':         r'slow\s*query|슬로우\s*쿼리|느린\s*쿼리|long\s*running\s*query',
    'TIMEOUT':            r'timeout|타임아웃|timed?\s*out|ETIMEDOUT|응답\s*지연',
    'CONNECTION_REFUSED': r'connection\s*refused|연결\s*거부|ECONNREFUSED|connect.*fail',
    'DEADLOCK':           r'deadlock|교착\s*상태|Deadlock\s*found',
    'DISK_FULL':          r'disk\s*full|디스크\s*풀|No\s*space\s*left|ENOSPC|디스크\s*부족',
    'AJP':                r'AJP|ajp\s*connector|mod_jk|mod_proxy_ajp',
    'NPE':                r'NullPointerException|NPE|null\s*pointer',
    'THREAD_POOL':        r'thread\s*pool|쓰레드\s*풀|max\s*threads|ThreadPoolExecutor',
}

SERVICE_PATTERNS = {
    'Apache':    r'\bApache\b|httpd|apache2',
    'Tomcat':    r'\bTomcat\b|catalina',
    'MySQL':     r'\bMySQL\b|mysqld|innodb',
    'SpringBoot': r'Spring\s*Boot|SpringBoot',
    'JVM':       r'\bJVM\b|java\.lang|java\s*heap',
}

CONTEXT_RE = re.compile(
    r'\b(그|이|해당|그\s*서버|해당\s*서버|방금|아까|'
    r'위에서|위의|앞서|앞에서|그\s*문제|이\s*문제)\b'
)


@dataclass
class ExtractedEntities:
    ips: list = field(default_factory=list)
    hostnames: list = field(default_factory=list)
    http_errors: list = field(default_factory=list)
    mysql_errors: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    services: list = field(default_factory=list)
    metrics: list = field(default_factory=list)
    has_time_expr: bool = False

    @property
    def all_errors(self):
        return self.http_errors + self.mysql_errors

    @property
    def all_servers(self):
        return self.ips + self.hostnames

    def to_dict(self):
        return {
            'ips': self.ips, 'hostnames': self.hostnames,
            'errors': self.all_errors, 'keywords': self.keywords,
            'services': self.services, 'metrics': self.metrics,
        }


def extract_entities(text: str) -> ExtractedEntities:
    e = ExtractedEntities()

    e.ips = sorted(set(IP_RE.findall(text)))

    full_names = [m.group(1).lower() for m in HOSTNAME_FULL_RE.finditer(text)]
    short_names = [m.group(1).lower() for m in HOSTNAME_SHORT_RE.finditer(text)]
    filtered_short = [s for s in short_names
                      if not any(f.startswith(s) for f in full_names)]
    e.hostnames = sorted(set(full_names + filtered_short))

    # HTTP 에러코드: 4xx, 5xx 는 바로 추출
    e.http_errors = sorted(set(
        m.group(1) for m in HTTP_ERROR_RE.finditer(text)
        if m.group(1)[0] in ('4', '5')
    ))
    e.mysql_errors = sorted(set(MYSQL_ERROR_RE.findall(text)))

    for kw, pattern in KEYWORD_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            e.keywords.append(kw)

    for svc, pattern in SERVICE_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            e.services.append(svc)

    e.metrics = sorted(set(m.group(1) for m in METRIC_NAME_RE.finditer(text)))
    e.has_time_expr = bool(TIME_KEYWORD_RE.search(text))

    return e


def needs_context(text: str, entities: ExtractedEntities) -> bool:
    return bool(CONTEXT_RE.search(text)) and not entities.all_servers


if __name__ == '__main__':
    tests = [
        ("192.168.16.10 서버는 뭐하는 서버야?", ['192.168.16.10'], [], []),
        ("어제 web01에서 500 에러가 왜 발생했어?", [], ['web01'], ['500']),
        ("was01-bank16 OOM 원인이 머지?",         [], ['was01-bank16'], ['OOM']),
        ("db01 slow query랑 deadlock 확인해줘",    [], ['db01'], ['SLOW_QUERY', 'DEADLOCK']),
        ("AJP connection refused 에러",           [], [], ['AJP', 'CONNECTION_REFUSED']),
        ("Tomcat heap GC overhead 문제",          [], [], ['GC_OVERHEAD']),
    ]
    ok = 0
    for text, exp_ips, exp_hosts, exp_kw in tests:
        e = extract_entities(text)
        checks = ([ip in e.ips for ip in exp_ips]
                  + [any(h.lower() in eh.lower() for eh in e.hostnames) for h in exp_hosts]
                  + [k in e.keywords or k in e.http_errors for k in exp_kw])
        passed = all(checks)
        if passed: ok += 1
        icon = "✓" if passed else "✗"
        print(f"{icon} {text}")
        if not passed:
            print(f"   IPs={e.ips}, Hosts={e.hostnames}, KW={e.keywords}, Err={e.http_errors}")
    print(f"\n결과: {ok}/{len(tests)}")

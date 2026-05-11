"""
Phase 1-B: 한국어 시간 표현 파서
──────────────────────────────────────────────────
역할: 자연어 시간 표현 → (start_ts, end_ts) Unix timestamp 변환.
      LLM에 시간 파싱을 맡기면 hallucination 위험 → 룰 기반으로 처리.
의존: pendulum (pip install pendulum), re (stdlib)

지원 표현:
    "어제"                          → 어제 00:00~23:59
    "오늘"                          → 오늘 00:00~현재
    "지난주"                        → 지난 월~일
    "지난주 화요일"                  → 지난 화요일 00:00~23:59
    "3월 5일"                       → 이번/지난 3월 5일
    "3월 5일 14시~16시"              → 해당 날 14:00~16:00
    "5월 6일 오후 2시부터 4시까지"    → 14:00~16:00
    "최근 1시간" / "지난 30분"        → now-1h ~ now
    "2025-03-05 14:00~16:00"        → ISO 형식
"""

import re
from datetime import datetime, timedelta
from typing import Optional

try:
    import pendulum
    _USE_PENDULUM = True
except ImportError:
    _USE_PENDULUM = False
    print("[time_parser] pendulum 미설치 — datetime fallback 사용")

TZ = "Asia/Seoul"
WEEKDAYS_KR = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
AMPM_KR = {"오전": 0, "오후": 12, "낮": 0, "저녁": 12, "밤": 12}


def _now() -> datetime:
    if _USE_PENDULUM:
        return pendulum.now(TZ)
    from datetime import timezone
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(TZ)
        return datetime.now(tz)
    except Exception:
        return datetime.now()


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=23, minute=59, second=59, microsecond=999999)


def _to_ts(dt: datetime) -> int:
    return int(dt.timestamp())


class TimeRange:
    def __init__(self, start: datetime, end: datetime, description: str = ""):
        self.start = start
        self.end = end
        self.description = description

    @property
    def start_ts(self) -> int:
        return _to_ts(self.start)

    @property
    def end_ts(self) -> int:
        return _to_ts(self.end)

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def to_prometheus(self) -> tuple[str, str]:
        """Prometheus range_query용 RFC3339 형식 반환"""
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return self.start.strftime(fmt), self.end.strftime(fmt)

    def to_loki_ns(self) -> tuple[int, int]:
        """Loki API용 nanosecond timestamp 반환"""
        return self.start_ts * 10**9, self.end_ts * 10**9

    def __repr__(self):
        return (
            f"TimeRange({self.start.strftime('%Y-%m-%d %H:%M')} ~ "
            f"{self.end.strftime('%Y-%m-%d %H:%M')}, {self.duration_minutes}분)"
        )


def parse_time_expression(text: str) -> Optional[TimeRange]:
    """
    한국어 시간 표현을 TimeRange로 변환.
    파싱 실패 시 None 반환 → 상위에서 기본값(최근 1시간) 사용.
    """
    text = text.strip()
    now = _now()

    # ── 1. 상대 시간: "최근 N시간", "지난 N분", "N일 전" ─────────────
    m = re.search(r"최근\s*(\d+)\s*시간|지난\s*(\d+)\s*시간", text)
    if m:
        hours = int(m.group(1) or m.group(2))
        return TimeRange(now - timedelta(hours=hours), now, f"최근 {hours}시간")

    m = re.search(r"최근\s*(\d+)\s*분|지난\s*(\d+)\s*분", text)
    if m:
        mins = int(m.group(1) or m.group(2))
        return TimeRange(now - timedelta(minutes=mins), now, f"최근 {mins}분")

    m = re.search(r"(\d+)\s*일\s*전", text)
    if m:
        days = int(m.group(1))
        target = now - timedelta(days=days)
        return TimeRange(_start_of_day(target), _end_of_day(target), f"{days}일 전")

    # ── 2. "오늘", "어제", "그제" ──────────────────────────────────────
    if re.search(r"^오늘$|오늘\s*(하루|종일)?$", text):
        return TimeRange(_start_of_day(now), now, "오늘")

    if re.search(r"어제", text):
        yesterday = now - timedelta(days=1)
        tr = TimeRange(_start_of_day(yesterday), _end_of_day(yesterday), "어제")
        # 시간 범위 추가 패턴: "어제 14시~16시"
        tr = _apply_hour_range(tr, text)
        return tr

    if re.search(r"그제|그저께|이틀\s*전", text):
        day = now - timedelta(days=2)
        tr = TimeRange(_start_of_day(day), _end_of_day(day), "그제")
        return _apply_hour_range(tr, text)

    # ── 3. 지난주 ────────────────────────────────────────────────────
    if re.search(r"지난\s*주", text):
        # 지난주 특정 요일
        m_wd = re.search(r"지난\s*주?\s*([월화수목금토일])요일?", text)
        if m_wd:
            target_wd = WEEKDAYS_KR[m_wd.group(1)]
            days_back = (now.weekday() - target_wd) % 7 or 7
            target = now - timedelta(days=days_back)
            tr = TimeRange(_start_of_day(target), _end_of_day(target), f"지난주 {m_wd.group(1)}요일")
            return _apply_hour_range(tr, text)
        # 지난주 전체
        mon = now - timedelta(days=now.weekday() + 7)
        sun = mon + timedelta(days=6)
        return TimeRange(_start_of_day(mon), _end_of_day(sun), "지난주")

    # ── 4. 이번 주 특정 요일 ──────────────────────────────────────────
    m_wd = re.search(r"([월화수목금토일])요일", text)
    if m_wd:
        target_wd = WEEKDAYS_KR[m_wd.group(1)]
        days_diff = now.weekday() - target_wd
        target = now - timedelta(days=days_diff)
        tr = TimeRange(_start_of_day(target), _end_of_day(target), f"{m_wd.group(1)}요일")
        return _apply_hour_range(tr, text)

    # ── 5. 날짜 + 시간 범위: "3월 5일 14시~16시" ─────────────────────
    m = re.search(
        r"(\d{1,2})\s*월\s*(\d{1,2})\s*일"
        r"(?:\s+([오전오후낮저녁밤])?(\d{1,2})\s*시"
        r"(?:\s*[~부터~]\s*([오전오후낮저녁밤])?(\d{1,2})\s*시)?)?",
        text,
    )
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        year = now.year
        if month > now.month or (month == now.month and day > now.day):
            year -= 1  # 미래 날짜라면 작년으로

        base = now.replace(year=year, month=month, day=day)
        tr = TimeRange(_start_of_day(base), _end_of_day(base), f"{month}월 {day}일")

        if m.group(4):  # 시작 시각 있음
            ampm_s = AMPM_KR.get(m.group(3) or "", 0)
            h_start = int(m.group(4))
            if ampm_s == 12 and h_start < 12:
                h_start += 12
            tr.start = tr.start.replace(hour=h_start, minute=0, second=0)

            if m.group(6):  # 종료 시각도 있음
                ampm_e = AMPM_KR.get(m.group(5) or "", 0)
                h_end = int(m.group(6))
                if ampm_e == 12 and h_end < 12:
                    h_end += 12
                tr.end = tr.end.replace(hour=h_end, minute=59, second=59)
            else:
                tr.end = tr.start + timedelta(hours=1)

            tr.description = f"{month}월{day}일 {m.group(4)}시~{m.group(6) or int(m.group(4))+1}시"
        return tr

    # ── 6. ISO 형식: "2025-03-05" 또는 "2025-03-05 14:00~16:00" ──────
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        dt = now.replace(
            year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3))
        )
        tr = TimeRange(_start_of_day(dt), _end_of_day(dt), m.group(0))
        return _apply_hour_range(tr, text)

    return None  # 파싱 실패


def _apply_hour_range(tr: TimeRange, text: str) -> TimeRange:
    """
    기존 TimeRange에 시간 오버레이.
    지원 형식:
      - "14시~16시", "오후 2시부터 4시까지"  (한국어)
      - "14:00~16:00", "14:00-16:00"       (ISO)
    """
    # ── ISO 형식: "14:00~16:00" 또는 "14:00-16:00" ─────────────────
    m_iso = re.search(r'(\d{1,2}):(\d{2})\s*[~\-]\s*(\d{1,2}):(\d{2})', text)
    if m_iso:
        tr.start = tr.start.replace(hour=int(m_iso.group(1)), minute=int(m_iso.group(2)), second=0)
        tr.end   = tr.end.replace(  hour=int(m_iso.group(3)), minute=int(m_iso.group(4)), second=59)
        return tr

    # ── 한국어 형식: "(오후) N시 (부터) M시 (까지)" ──────────────────
    m = re.search(
        r"(오전|오후|낮|저녁|밤)?\s*(\d{1,2})(?::(\d{2}))?\s*시\s*(?:부터|~)?"
        r"\s*(오전|오후|낮|저녁|밤)?\s*(\d{1,2})(?::(\d{2}))?\s*시\s*(?:까지)?",
        text,
    )
    if m:
        ampm_s = AMPM_KR.get(m.group(1) or "", 0)
        h_s    = int(m.group(2))
        if ampm_s == 12 and h_s < 12:
            h_s += 12
        min_s  = int(m.group(3) or 0)

        # end의 오전/오후가 없으면 start의 오전/오후 승계
        ampm_e_str = m.group(4) or m.group(1) or ""
        ampm_e = AMPM_KR.get(ampm_e_str, 0)
        h_e    = int(m.group(5))
        if ampm_e == 12 and h_e < 12:
            h_e += 12
        min_e  = int(m.group(6) or 59)

        tr.start = tr.start.replace(hour=h_s, minute=min_s, second=0)
        tr.end   = tr.end.replace(  hour=h_e, minute=min_e, second=59)
    return tr


def default_range(minutes: int = 60) -> TimeRange:
    """시간 파싱 실패 시 기본값: 최근 N분"""
    now = _now()
    return TimeRange(now - timedelta(minutes=minutes), now, f"최근 {minutes}분 (기본값)")


if __name__ == "__main__":
    tests = [
        "어제",
        "어제 14시~16시",
        "어제 오후 2시부터 4시까지",
        "오늘",
        "그제",
        "지난주",
        "지난주 화요일",
        "3월 5일",
        "3월 5일 14시~16시",
        "5월 6일 오후 2시부터 4시까지",
        "최근 1시간",
        "최근 30분",
        "지난 2시간",
        "3일 전",
        "2025-03-05",
        "2025-03-05 14:00~16:00",
    ]
    print(f"{'입력':<35} {'결과'}")
    print("-" * 80)
    for t in tests:
        result = parse_time_expression(t) or default_range()
        print(f"{t:<35} {result}")

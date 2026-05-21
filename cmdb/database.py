"""
Phase 1-A: CMDB (Configuration Management Database)  [수정본]
──────────────────────────────────────────────────────────────
변경 요약:
  - prometheus_instance(IP:port) 제거 → prometheus_job 으로만 필터
  - app_job 추가 (WAS: bank-was-app — JVM/HTTP 앱 메트릭)
  - loki_host → loki_service_name + loki_server_role 로 분리
  - trace_service_name 추가 (Tempo/Jaeger OTel service.name 레이블)
    loki_service_name(로그)과 별개 — Tempo 트레이스 조회 시 사용
  - seed_banksystem_16: 실제 확인된 hostname/IP/job 값으로 교체
    web: dev-masternode / 192.168.0.140 / bank-web-hostmetrics / trace: ai-web-httpd
    was: ONTUNETEST2   / 192.168.0.54  / bank-was-hostmetrics + bank-was-app / trace: bank-was-app
    db:  DESKTOP-H0M89JB / 192.168.0.63 / bank-db-hostmetrics
  - _row() 순환 import 버그 수정
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class ServerProfile:
    ip: str
    hostname: str
    role: str                   # "web" | "was" | "db"
    os: str                     # "linux" | "windows"
    tier: int                   # 1=web, 2=was, 3=db
    team: str
    services: list              = field(default_factory=list)
    prometheus_job: str         = ""   # Prometheus job 레이블
    app_job: str                = ""   # WAS 앱 메트릭 job (bank-was-app)
    loki_service_name: str      = ""   # Loki service_name 레이블 (로그 조회)
    loki_server_role: str       = ""   # Loki server_role 레이블
    description: str            = ""
    trace_service_name: str     = ""   # Tempo/Jaeger OTel service.name (트레이스 조회)

    def to_text(self) -> str:
        """LLM 응답용 자연어 설명 생성"""
        role_map = {
            "web": "Web Front (Apache)",
            "was": "WAS (Tomcat)",
            "db":  "DB (MySQL)",
        }
        app_job_line = (
            f"  앱 메트릭 job: {self.app_job}\n" if self.app_job else ""
        )
        return (
            f"[{self.hostname}] {role_map.get(self.role, self.role)} 서버\n"
            f"  IP: {self.ip} | OS: {self.os.upper()} | Tier {self.tier}\n"
            f"  담당팀: {self.team}\n"
            f"  구동 서비스: {', '.join(self.services)}\n"
            f"  Prometheus job: {self.prometheus_job}\n"
            f"{app_job_line}"
            f"  Loki: service_name={self.loki_service_name}"
            f" / server_role={self.loki_server_role}\n"
            f"  Trace: service_name={self.trace_service_name or '(미수집)'}\n"
            f"  설명: {self.description}"
        )


class CMDB:
    def __init__(self, db_path: str = "cmdb.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ── 연결 관리 ────────────────────────────────────────────────
    def _get_conn(self) -> sqlite3.Connection:
        """영구 연결 반환 (없으면 생성). WAL 모드로 읽기 동시성 확보."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    @contextmanager
    def _tx(self):
	    # """쓰기 전용 트랜잭션 컨텍스트 (예외 시 rollback)."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self):
        """명시적 연결 종료 (테스트·종료 시 호출)."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── DDL ──────────────────────────────────────────────────────
    def _init_db(self):
        with self._tx() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    ip                  TEXT PRIMARY KEY,
                    hostname            TEXT NOT NULL,
                    role                TEXT NOT NULL,
                    os                  TEXT NOT NULL,
                    tier                INTEGER NOT NULL,
                    team                TEXT NOT NULL,
                    services            TEXT DEFAULT '[]',
                    prometheus_job      TEXT DEFAULT '',
                    app_job             TEXT DEFAULT '',
                    loki_service_name   TEXT DEFAULT '',
                    loki_server_role    TEXT DEFAULT '',
                    description         TEXT DEFAULT '',
                    trace_service_name  TEXT DEFAULT ''
                )
            """)
            # 기존 DB 마이그레이션 — trace_service_name 컬럼 없는 경우 추가
            try:
                conn.execute("ALTER TABLE servers ADD COLUMN trace_service_name TEXT DEFAULT ''")
            except Exception:
                pass  # 이미 존재하는 컬럼

    # ── 쓰기 ─────────────────────────────────────────────────────
    def add_server(self, server: "ServerProfile"):
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO servers
                   (ip, hostname, role, os, tier, team, services,
                    prometheus_job, app_job, loki_service_name, loki_server_role,
                    description, trace_service_name)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    server.ip,
                    server.hostname,
                    server.role,
                    server.os,
                    server.tier,
                    server.team,
                    json.dumps(server.services, ensure_ascii=False),
                    server.prometheus_job,
                    server.app_job,
                    server.loki_service_name,
                    server.loki_server_role,
                    server.description,
                    server.trace_service_name,
                ),
            )

    # ── 읽기 ─────────────────────────────────────────────────────
    def get_by_ip(self, ip: str) -> Optional["ServerProfile"]:
        row = self._get_conn().execute(
            "SELECT * FROM servers WHERE ip = ?", (ip,)
        ).fetchone()
        return self._row(row)

    def get_by_hostname(self, name: str) -> list["ServerProfile"]:
        """부분 매칭 — 복수 결과 반환."""
        rows = self._get_conn().execute(
            "SELECT * FROM servers WHERE hostname LIKE ? ORDER BY tier, hostname",
            (f"%{name}%",),
        ).fetchall()
        return [self._row(r) for r in rows]

    def search(self, query: str) -> list["ServerProfile"]:
        """IP / hostname / role / description 통합 LIKE 검색."""
        rows = self._get_conn().execute(
            """SELECT * FROM servers
               WHERE ip LIKE ? OR hostname LIKE ? OR role LIKE ? OR description LIKE ?
               ORDER BY tier, hostname""",
            (f"%{query}%",) * 4,
        ).fetchall()
        return [self._row(r) for r in rows]

    def get_by_role(self, role: str) -> list["ServerProfile"]:
        rows = self._get_conn().execute(
            "SELECT * FROM servers WHERE role = ? ORDER BY hostname", (role,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def get_all(self) -> list["ServerProfile"]:
        rows = self._get_conn().execute(
            "SELECT * FROM servers ORDER BY tier, hostname"
        ).fetchall()
        return [self._row(r) for r in rows]

    # def resolve(self, identifier: str) -> list["ServerProfile"]:
    #     """IP → 단건 리스트 / hostname → 부분 매칭 리스트."""
    #     if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", identifier):
    #         result = self.get_by_ip(identifier)
    #         return [result] if result else []
    #     return self.get_by_hostname(identifier)
    def resolve(self, identifier: str) -> list["ServerProfile"]:
        if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", identifier):
            result = self.get_by_ip(identifier)
            return [result] if result else []

        if identifier.lower() in ("web", "was", "db"):
            return self.get_by_role(identifier.lower())

        # hostname 부분 매칭 우선
        by_host = self.get_by_hostname(identifier)
        if by_host:
            return by_host

        # OTel service name(trace_service_name / loki_service_name)으로도 검색
        # "bank-was-app", "ai-web-httpd" 같은 서비스명 입력 시
        rows = self._get_conn().execute(
            """SELECT * FROM servers
               WHERE trace_service_name LIKE ? OR loki_service_name LIKE ?
               ORDER BY tier, hostname""",
            (f"%{identifier}%", f"%{identifier}%"),
        ).fetchall()
        return [r for r in (self._row(row) for row in rows) if r]

    # ── 내부 변환 ─────────────────────────────────────────────────
    def _row(self, row) -> Optional["ServerProfile"]:
        """DB row → ServerProfile. 순환 import 없이 직접 생성."""
        if row is None:
            return None
        return ServerProfile(          # ← 같은 파일의 ServerProfile 직접 사용
            ip=row[0],
            hostname=row[1],
            role=row[2],
            os=row[3],
            tier=row[4],
            team=row[5],
            services=json.loads(row[6]),
            prometheus_job=row[7],
            app_job=row[8],
            loki_service_name=row[9],
            loki_server_role=row[10],
            description=row[11],
            trace_service_name=row[12] if len(row) > 12 else "",
        )


# ── BankSystem_16 초기 데이터 ────────────────────────────────────
def seed_banksystem_16(cmdb: CMDB):
    """
    실제 확인된 값 기준:
      Web : dev-masternode  / 192.168.0.140 / bank-web-hostmetrics / trace: ai-web-httpd
      WAS : ONTUNETEST2    / 192.168.0.54  / bank-was-hostmetrics + bank-was-app / trace: bank-was-app
      DB  : DESKTOP-H0M89JB / 192.168.0.63 / bank-db-hostmetrics / trace: 미수집
    """
    servers = [
        # ── Web Layer (Apache / Linux) ────────────────────────────
        ServerProfile(
            ip="192.168.0.140",
            hostname="dev-masternode",
            role="web",
            os="linux",
            tier=1,
            team="인프라팀",
            services=["Apache 2.4", "mod_jk", "AJP Connector :8009"],
            prometheus_job="bank-web-hostmetrics",
            app_job="",
            loki_service_name="bank-web-httpd-logs",
            loki_server_role="web",
            description="BankSystem_16 Web Front (Apache/Linux) — mod_jk → WAS",
            trace_service_name="ai-web-httpd",     # OTel SDK service.name (Tempo)
        ),
        # ── WAS Layer (Tomcat / Windows) ─────────────────────────
        ServerProfile(
            ip="192.168.0.54",
            hostname="ONTUNETEST2",
            role="was",
            os="windows",
            tier=2,
            team="개발팀",
            services=["Tomcat 9.0", "Spring Boot 2.7", "AJP :8009"],
            prometheus_job="bank-was-hostmetrics",
            app_job="bank-was-app",
            loki_service_name="bank-was-tomcat-logs",
            loki_server_role="was",
            description="BankSystem_16 WAS (Tomcat 9/Windows) — AJP ← web",
            trace_service_name="bank-was-app",     # OTel SDK service.name (Tempo) = app_job 동일
        ),
        # ── DB Layer (MySQL / Windows) ────────────────────────────
        ServerProfile(
            ip="192.168.0.63",
            hostname="DESKTOP-H0M89JB",
            role="db",
            os="windows",
            tier=3,
            team="DBA팀",
            services=["MySQL 8.0"],
            prometheus_job="bank-db-hostmetrics",
            app_job="",
            loki_service_name="",
            loki_server_role="db",
            description="BankSystem_16 DB (MySQL 8.0/Windows)",
            trace_service_name="",                 # 트레이스 미수집
        ),
    ]
    for s in servers:
        cmdb.add_server(s)
    print(f"[CMDB] BankSystem_16 서버 {len(servers)}개 등록 완료")
    return servers


# ── 독립 실행 테스트 ─────────────────────────────────────────────
if __name__ == "__main__":
    import os
    import tempfile

    db_path = os.path.join(tempfile.gettempdir(), "test_cmdb.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    cmdb = CMDB(db_path=db_path)
    seed_banksystem_16(cmdb)

    print("\n=== IP 조회 ===")
    s = cmdb.get_by_ip("192.168.0.140")
    if s:
        print(s.to_text())

    print("\n=== hostname 부분 검색 (was) ===")
    for s in cmdb.get_by_hostname("ONTUNETEST2"):
        print(s.to_text())

    print("\n=== 역할별 조회 (db) ===")
    for sv in cmdb.get_by_role("db"):
        print(f"  {sv.hostname} ({sv.ip}) | job={sv.prometheus_job}")

    print("\n=== resolve() ===")
    for q in ["192.168.0.63", "ONTUNETEST2", "web"]:
        results = cmdb.resolve(q)
        for r in results:
            print(
                f"  '{q}' → {r.hostname}"
                f" | prom_job={r.prometheus_job}"
                f" | app_job={r.app_job or '-'}"
                f" | loki={r.loki_service_name or '-'}"
            )

    print("\n=== 전체 서버 ===")
    for sv in cmdb.get_all():
        print(f"  Tier{sv.tier} {sv.hostname:<20} role={sv.role:<4}"
              f" job={sv.prometheus_job}")

    cmdb.close()
    print("\n[CMDB] 테스트 완료")

"""
Phase 1-A: CMDB (Configuration Management Database)
──────────────────────────────────────────────────
역할: 모든 쿼리의 기반. IP↔hostname 매핑, 서버 역할·소속·연관 서비스 저장.
의존: sqlite3 (stdlib), json (stdlib)

사용 예시:
    cmdb = CMDB()
    seed_banksystem_16(cmdb)
    server = cmdb.get_by_ip("192.168.16.10")
    print(server.hostname, server.role, server.prometheus_job)
"""

import json
import sqlite3
import re          # ← 추가
from dataclasses import asdict, dataclass, field
from typing import Optional
from contextlib import contextmanager


@dataclass
class ServerProfile:
    ip: str
    hostname: str
    role: str  # "web" | "was" | "db"
    os: str  # "linux" | "windows"
    tier: int  # 1=web, 2=was, 3=db (MicroRCA METRIC_TIERS 연동)
    team: str
    services: list = field(default_factory=list)
    prometheus_job: str = ""
    prometheus_instance: str = ""  # host:port 형식
    loki_host: str = ""  # Loki {host="..."} 레이블 값
    description: str = ""

    def to_text(self) -> str:
        """LLM 응답용 자연어 설명 생성"""
        role_map = {
            "web": "Web Front (Apache)",
            "was": "WAS (Tomcat)",
            "db": "DB (MySQL)",
        }
        return (
            f"[{self.hostname}] {role_map.get(self.role, self.role)} 서버\n"
            f"  IP: {self.ip} | OS: {self.os.upper()} | Tier {self.tier}\n"
            f"  담당팀: {self.team}\n"
            f"  구동 서비스: {', '.join(self.services)}\n"
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
        """쓰기 전용 트랜잭션 컨텍스트 (예외 시 rollback)."""
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
        # with sqlite3.connect(self.db_path) as conn:
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
                    prometheus_instance TEXT DEFAULT '',
                    loki_host           TEXT DEFAULT '',
                    description         TEXT DEFAULT ''
                )
            """)            

    # ── 쓰기 ─────────────────────────────────────────────────────
    def add_server(self, server: "ServerProfile"):
    # def add_server(self, server: ServerProfile):
        # with sqlite3.connect(self.db_path) as conn:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO servers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    server.ip,
                    server.hostname,
                    server.role,
                    server.os,
                    server.tier,
                    server.team,
                    json.dumps(server.services, ensure_ascii=False),
                    server.prometheus_job,
                    server.prometheus_instance,
                    server.loki_host,
                    server.description,
                ),
            )

    # def get_by_ip(self, ip: str) -> Optional[ServerProfile]:
    #     with sqlite3.connect(self.db_path) as conn:
    #         row = conn.execute("SELECT * FROM servers WHERE ip = ?", (ip,)).fetchone()
    #     return self._to_profile(row) if row else None
        
    def get_by_ip(self, ip: str) -> Optional["ServerProfile"]:
        row = self._get_conn().execute(
            "SELECT * FROM servers WHERE ip = ?", (ip,)
        ).fetchone()
        return self._row(row)
    
    

    # def get_by_hostname(self, name: str) -> Optional[ServerProfile]:
    #     """부분 매칭 지원: 'web01' → 'web01-bank16'"""
    #     with sqlite3.connect(self.db_path) as conn:
    #         row = conn.execute(
    #             "SELECT * FROM servers WHERE hostname LIKE ?", (f"%{name}%",)
    #         ).fetchone()
    #     return self._to_profile(row) if row else None
    def get_by_hostname(self, name: str) -> list["ServerProfile"]:
        """
        부분 매칭 지원 → 복수 결과 반환.
        ('web' 검색 시 web01·web02 모두 반환)
        """
        rows = self._get_conn().execute(
            "SELECT * FROM servers WHERE hostname LIKE ? ORDER BY tier, hostname",
            (f"%{name}%",),
        ).fetchall()
        return [self._row(r) for r in rows]            

    # def search(self, query: str) -> list[ServerProfile]:
    #     """IP, hostname, role, description 통합 검색"""
    #     with sqlite3.connect(self.db_path) as conn:
    #         rows = conn.execute(
    #             """SELECT * FROM servers WHERE
    #                ip LIKE ? OR hostname LIKE ? OR role LIKE ? OR description LIKE ?""",
    #             (f"%{query}%",) * 4,
    #         ).fetchall()
    #     return [self._to_profile(r) for r in rows]
    def search(self, query: str) -> list["ServerProfile"]:
        """IP / hostname / role / description 통합 LIKE 검색."""
        rows = self._get_conn().execute(
            """SELECT * FROM servers
               WHERE ip LIKE ? OR hostname LIKE ? OR role LIKE ? OR description LIKE ?
               ORDER BY tier, hostname""",
            (f"%{query}%",) * 4,
        ).fetchall()
        return [self._row(r) for r in rows]

    # def get_by_role(self, role: str) -> list[ServerProfile]:
    #     with sqlite3.connect(self.db_path) as conn:
    #         rows = conn.execute(
    #             "SELECT * FROM servers WHERE role = ?", (role,)
    #         ).fetchall()
    #     return [self._to_profile(r) for r in rows]
    def get_by_role(self, role: str) -> list["ServerProfile"]:
        rows = self._get_conn().execute(
            "SELECT * FROM servers WHERE role = ? ORDER BY hostname", (role,)
        ).fetchall()
        return [self._row(r) for r in rows]

    # def get_all(self) -> list[ServerProfile]:
    #     with sqlite3.connect(self.db_path) as conn:
    #         rows = conn.execute(
    #             "SELECT * FROM servers ORDER BY tier, hostname"
    #         ).fetchall()
    #     return [self._to_profile(r) for r in rows]
    def get_all(self) -> list["ServerProfile"]:
        rows = self._get_conn().execute(
            "SELECT * FROM servers ORDER BY tier, hostname"
        ).fetchall()
        return [self._row(r) for r in rows]

    # def resolve(self, identifier: str) -> Optional[ServerProfile]:
    #     """IP 또는 hostname 자동 판별 후 조회"""
    #     import re

    #     if re.match(r"^\d+\.\d+\.\d+\.\d+$", identifier):
    #         return self.get_by_ip(identifier)
    #     return self.get_by_hostname(identifier)
    # def resolve(self, identifier: str) -> list["ServerProfile"]:
    #     """
    #     IP → 단건 리스트 / hostname → 부분 매칭 리스트.
    #     항상 list 반환으로 타입 일관성 확보.
    #     """
    #     if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", identifier):
    #         result = self.get_by_ip(identifier)
    #         return [result] if result else []
    #     return self.get_by_hostname(identifier)
    def resolve(self, identifier: str) -> list["ServerProfile"]:
        if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", identifier):
            result = self.get_by_ip(identifier)
            return [result] if result else []

        # ← 추가: role 키워드면 get_by_role() 사용
        if identifier.lower() in ("web", "was", "db"):
            return self.get_by_role(identifier.lower())

        return self.get_by_hostname(identifier)

    # def _to_profile(self, row) -> ServerProfile:
    #     return ServerProfile(
    #         ip=row[0],
    #         hostname=row[1],
    #         role=row[2],
    #         os=row[3],
    #         tier=row[4],
    #         team=row[5],
    #         services=json.loads(row[6]),
    #         prometheus_job=row[7],
    #         prometheus_instance=row[8],
    #         loki_host=row[9],
    #         description=row[10],
    #     )
    # ── 내부 변환 ─────────────────────────────────────────────────
    def _row(self, row) -> Optional["ServerProfile"]:
        if row is None:
            return None
        from cmdb.database_old import ServerProfile  # 순환 import 방지
        return ServerProfile(
            ip=row[0], hostname=row[1], role=row[2],
            os=row[3], tier=row[4], team=row[5],
            services=json.loads(row[6]),
            prometheus_job=row[7],
            prometheus_instance=row[8],
            loki_host=row[9],
            description=row[10],
        )


def seed_banksystem_16(cmdb: CMDB):
    """BankSystem_16 초기 자산 데이터 등록 (Web×2 / WAS×2 / DB×2)"""
    servers = [
        # ── Web Layer (Apache / Linux) ─────────────────────────────────
        ServerProfile(
            ip="192.168.0.140",
            hostname="web-bank16",
            role="web",
            os="linux",
            tier=1,
            team="인프라팀",
            services=["Apache 2.4", "AJP Connector :8009"],
            prometheus_job="apache_exporter",
            prometheus_instance="192.168.16.10:9117",
            loki_host="web-bank16",
            description="BankSystem_16 Web Front #1 (Apache/Linux) — L4 LB 뒷단",
        ),
        # ServerProfile(
        #     ip="192.168.16.11", hostname="web02-bank16",
        #     role="web", os="linux", tier=1, team="인프라팀",
        #     services=["Apache 2.4", "AJP Connector :8009"],
        #     prometheus_job="apache_exporter", prometheus_instance="192.168.16.11:9117",
        #     loki_host="web02-bank16",
        #     description="BankSystem_16 Web Front #2 (Apache/Linux) — L4 LB 뒷단",
        # ),
        # ── WAS Layer (Tomcat / Windows) ──────────────────────────────
        ServerProfile(
            ip="192.168.0.54",
            hostname="was-bank16",
            role="was",
            os="windows",
            tier=2,
            team="개발팀",
            services=["Tomcat 9.0", "Spring Boot 2.7", "AJP :8009"],
            prometheus_job="jmx_exporter",
            prometheus_instance="192.168.16.20:9090",
            loki_host="was-bank16",
            description="BankSystem_16 WAS #1 (Tomcat/Windows) — web → AJP",
        ),
        # ServerProfile(
        #     ip="192.168.16.21", hostname="was02-bank16",
        #     role="was", os="windows", tier=2, team="개발팀",
        #     services=["Tomcat 9.0", "Spring Boot 2.7", "AJP :8009"],
        #     prometheus_job="jmx_exporter", prometheus_instance="192.168.16.21:9090",
        #     loki_host="was02-bank16",
        #     description="BankSystem_16 WAS #2 (Tomcat/Windows) — web01·02 → AJP",
        # ),
        # ── DB Layer (MySQL / Windows) ────────────────────────────────
        ServerProfile(
            ip="192.168.0.63",
            hostname="db-bank16",
            role="db",
            os="windows",
            tier=3,
            team="DBA팀",
            services=["MySQL 8.0 Primary"],
            prometheus_job="mysqld_exporter",
            prometheus_instance="192.168.16.30:9104",
            loki_host="db-bank16",
            description="BankSystem_16 DB Primary (MySQL 8.0/Windows) — 쓰기 노드",
        ),
        # ServerProfile(
        #     ip="192.168.16.31", hostname="db02-bank16",
        #     role="db", os="windows", tier=3, team="DBA팀",
        #     services=["MySQL 8.0 Replica"],
        #     prometheus_job="mysqld_exporter", prometheus_instance="192.168.16.31:9104",
        #     loki_host="db02-bank16",
        #     description="BankSystem_16 DB Replica (MySQL 8.0/Windows) — 읽기 노드",
        # ),
    ]
    for s in servers:
        cmdb.add_server(s)
    print(f"[CMDB] BankSystem_16 서버 {len(servers)}개 등록 완료")
    return servers


# if __name__ == "__main__":
#     cmdb = CMDB(db_path="test_cmdb.db")
#     seed_banksystem_16(cmdb)

#     print("\n=== IP 조회 테스트 ===")
#     s = cmdb.get_by_ip("192.168.0.54")
#     print(s.to_text())
    
#     for s in cmdb.get_by_hostname("was"):
#         print(s.to_text())
    

#     print("\n=== hostname 부분 검색 ===")
#     s = cmdb.get_by_hostname("was")
#     print(s.to_text())

#     print("\n=== 역할별 조회 (web) ===")
#     for sv in cmdb.get_by_role("web"):
#         print(f"  {sv.hostname} ({sv.ip})")

#     print("\n=== resolve() — IP 또는 hostname 자동 판별 ===")
#     for q in ["192.168.0.63", "was-bank16", "web"]:
#         r = cmdb.resolve(q)
#         print(f"  '{q}' → {r.hostname if r else 'NOT FOUND'}")

if __name__ == "__main__":
    import os, tempfile

    db_path = os.path.join(tempfile.gettempdir(), "test_cmdb.db")
    
    # 매 테스트마다 깨끗하게 시작
    if os.path.exists(db_path):
        os.remove(db_path)

    cmdb = CMDB(db_path=db_path)
    seed_banksystem_16(cmdb)

    print("\n=== IP 조회 테스트 ===")
    s = cmdb.get_by_ip("192.168.0.140")
    if s:
        print(s.to_text())

    print("\n=== hostname 부분 검색 ===")
    for s in cmdb.get_by_hostname("was"):   # ← for 루프 추가
        print(s.to_text())

    print("\n=== 역할별 조회 (web) ===")
    for sv in cmdb.get_by_role("web"):
        print(f"  {sv.hostname} ({sv.ip})")

    print("\n=== resolve() ===")
    for q in ["192.168.0.63", "was-bank16", "web"]:
        results = cmdb.resolve(q)
        for r in results:
            print(f"  '{q}' → {r.hostname}")
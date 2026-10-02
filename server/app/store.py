import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import asdict
from io import BytesIO
import json
from pathlib import Path
import secrets
import shutil
import tempfile
from threading import RLock
import time
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from .config import Settings
from .models import Course, GradingResult, Question, QuizSession, User
from .services.knowledge_base import LocalKnowledgeBase
from .utils.security import hash_password

_STORE_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store-writer")


def _id() -> str:
    return str(uuid4())


def _asdict(value: Any) -> dict[str, Any]:
    return asdict(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _user_state(user: User) -> dict[str, Any]:
    state = _asdict(user)
    state["last_login_at"] = _iso(user.last_login_at)
    state["requested_at"] = _iso(user.requested_at)
    return state


def _user_from_state(state: dict[str, Any]) -> User:
    values = dict(state)
    values["last_login_at"] = _parse_datetime(state.get("last_login_at"))
    values["requested_at"] = _parse_datetime(state.get("requested_at"))
    allowed = {f for f in User.__dataclass_fields__}
    return User(**{key: value for key, value in values.items() if key in allowed})


def _quiz_state(session: QuizSession) -> dict[str, Any]:
    state = _asdict(session)
    state["started_at"] = _iso(session.started_at)
    state["submitted_at"] = _iso(session.submitted_at)
    state["draft_updated_at"] = _iso(session.draft_updated_at)
    return state


def _quiz_from_state(state: dict[str, Any]) -> QuizSession:
    return QuizSession(
        **{
            **state,
            "started_at": _parse_datetime(state.get("started_at")) or datetime.now(timezone.utc),
            "submitted_at": _parse_datetime(state.get("submitted_at")),
            "draft_updated_at": _parse_datetime(state.get("draft_updated_at")),
        }
    )


def _result_state(result: GradingResult) -> dict[str, Any]:
    state = _asdict(result)
    state["graded_at"] = _iso(result.graded_at)
    return state


def _result_from_state(state: dict[str, Any]) -> GradingResult:
    return GradingResult(
        **{
            **state,
            "graded_at": _parse_datetime(state.get("graded_at")) or datetime.now(timezone.utc),
        }
    )


class Store:
    """Repository with in-memory or atomic JSON persistence.

    ponytail: JSON persistence is a single-process bridge; add a database
    backend only when multiple workers or concurrent instances are required.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.users: dict[str, User] = {}
        self.sessions: dict[str, tuple[str, float]] = {}
        self.login_failures: dict[str, tuple[int, float]] = {}
        self.registration_attempts: dict[str, list[float]] = {}
        self.lock = RLock()
        self._save_lock = RLock()
        self.courses: dict[str, Course] = {}
        self.enrollments: dict[tuple[str, str], str] = {}
        self.questions: dict[str, Question] = {}
        self.quiz_sessions: dict[str, QuizSession] = {}
        self.assignments: dict[str, dict[str, Any]] = {}
        self.roll_calls: list[dict[str, Any]] = []
        self.excel_schedules: dict[str, dict[str, Any]] = {}
        self.excel_snapshots: list[dict[str, Any]] = []
        self.results: dict[str, GradingResult] = {}
        self.chat_history: list[dict[str, Any]] = []
        self.usage_logs: list[dict[str, Any]] = []
        self.documents: dict[str, dict[str, Any]] = {}
        self.import_previews: dict[str, dict[str, Any]] = {}
        self.graph_nodes: dict[str, dict[str, Any]] = {}
        self.audit_logs: list[dict[str, Any]] = []
        self.mastery: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.chat_quota: dict[str, dict[str, Any]] = {}
        self.knowledge = LocalKnowledgeBase(settings)
        self.persistence_path = Path(settings.data_dir) / "store.json"
        self.persistence_enabled = settings.persistence_enabled
        self._needs_migration = False
        loaded = self.persistence_enabled and self._load()
        recovered_grading = False
        if loaded:
            for session in self.quiz_sessions.values():
                if session.status == "grading":
                    session.status = "ongoing"
                    session.submitted_at = None
                    recovered_grading = True
        if loaded and (self._needs_migration or recovered_grading):
            if not self.save():
                raise OSError("migrated store state could not be persisted")
            self._needs_migration = False
        if not loaded and settings.app_env == "development":
            self._seed()

    def _seed(self) -> None:
        self.add_user("admin", "超级管理员", "admin123*", "admin")
        teacher = self.add_user("teacher01", "李老师", "teach123*", "teacher")
        student = self.add_user("stu001", "张伟", "stu123*", "student")
        self.courses["audit-101"] = Course("audit-101", "审计学", "2025-2026", teacher.id, class_code="AUD101")
        self.enrollments[("audit-101", student.id)] = "审计一班"
        self.add_question(
            Question(
                id=_id(), code="CPA-2025-AUD-0001", type="single_choice",
                stem="下列各项中，可靠性最高的审计证据是：",
                options=["管理层书面声明", "银行询证函回函", "内部记账凭证", "口头答复"],
                answer=1, reference_answer="银行询证函回函", rubric=[],
                knowledge_points=["审计证据可靠性"], difficulty="easy", course_id="audit-101", chapter="审计证据",
            )
        )
        self.add_question(
            Question(
                id=_id(), code="CPA-2025-AUD-0002", type="multi_choice",
                stem="下列哪些属于获取审计证据的具体程序？",
                options=["检查", "观察", "函证", "重新计算"], answer=[0, 1, 2, 3],
                reference_answer="检查、观察、函证、重新计算", rubric=[],
                 knowledge_points=["审计程序"], difficulty="medium", course_id="audit-101", chapter="审计程序",
            )
        )
        self.add_question(
            Question(
                id=_id(), code="CPA-2025-AUD-0003", type="short_answer",
                stem="简述审计证据的充分性与适当性及两者关系。",
                options=[], answer=None,
                reference_answer="充分性是数量要求，适当性是质量要求，数量不能弥补质量缺陷。",
                rubric=[{"point": "充分性", "score": 8}, {"point": "适当性", "score": 8}, {"point": "关系", "score": 9}],
                 knowledge_points=["审计证据充分性与适当性"], difficulty="medium", course_id="audit-101", chapter="审计证据",
            )
        )
        graph_seed = {
            "knowledge": {
                "A": ["审计基础理论", "审计准则体系", "风险评估与应对", "审计程序方法", "审计证据与工作底稿", "审计报告与意见"],
                "B": ["AI审计工具应用", "Python审计编程", "AI指令设计", "异常数据智能分析", "数智审计案例实践"],
                "C": ["审计沟通与表达", "审计报告撰写", "职业伦理与道德", "批判性思维训练", "案例分析与决策", "团队协作与项目管理"],
            },
            "comp": {
                "A": ["审计准则解读能力", "职业道德判断能力", "风险评估能力", "审计程序设计能力", "证据收集与评价能力", "审计抽样应用能力"],
                "B": ["AI审计工具操作能力", "Python函证程序编写能力", "风险评估智能体应用能力", "AI指令设计能力", "异常数据分析能力"],
                "C": ["沟通表达能力", "报告撰写能力", "职业伦理素养", "批判性思维", "案例分析能力", "团队协作能力"],
            },
            "problem": {
                "A": ["审计的概念与目标", "中国审计准则体系", "审计风险要素", "七种审计程序", "证据充分适当性", "四种审计意见"],
                "B": ["AI识别财务异常", "Python函证程序", "异常交易定位", "智能风险评估", "审计Prompt设计"],
                "C": ["综合审计报告", "多Agent年报审计", "舞弊识别方案", "数智审计方案", "独立性冲突分析", "行业风险对标"],
            },
        }
        problem_to_knowledge = {
            "证据充分适当性": "审计证据与工作底稿",
            "中国审计准则体系": "审计准则体系",
            "智能风险评估": "风险评估与应对",
            "七种审计程序": "审计程序方法",
            "AI识别财务异常": "AI审计工具应用",
            "Python函证程序": "Python审计编程",
            "审计Prompt设计": "AI指令设计",
            "独立性冲突分析": "职业伦理与道德",
            "综合审计报告": "审计报告撰写",
            "异常交易定位": "异常数据智能分析",
        }
        node_ids: dict[str, str] = {}
        for graph, branches in graph_seed.items():
            for branch, names in branches.items():
                for name in names:
                    node_id = _id()
                    node_ids.setdefault(name, node_id)
                    self.graph_nodes[node_id] = {
                        "id": node_id, "graph": graph, "branch": branch,
                        "name": name, "description": "演示节点，正式版由课程教师维护。", "map_from": None,
                        "created_by": teacher.id,
                    }
        for problem_name, knowledge_name in problem_to_knowledge.items():
            node = self.graph_nodes.get(node_ids.get(problem_name, ""))
            if node is not None:
                node["map_from"] = node_ids.get(knowledge_name)

    def add_user(self, username: str, display_name: str, password: str, role: str) -> User:
        user = User(_id(), username, display_name, hash_password(password), role)
        self.users[user.id] = user
        return user

    def add_question(self, question: Question) -> None:
        self.questions[question.id] = question

    def save(self) -> bool:
        if not self.persistence_enabled:
            return True
        temporary = self.persistence_path.with_name(f".{self.persistence_path.name}.{uuid4().hex}.tmp")
        try:
            with self._save_lock:
                with self.lock:
                    state = deepcopy(self._state())
                encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(encoded, encoding="utf-8")
                temporary.replace(self.persistence_path)
            return True
        except (OSError, TypeError, ValueError, RuntimeError):
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    async def save_async(self) -> bool:
        if not self.persistence_enabled:
            return True
        return await asyncio.get_running_loop().run_in_executor(_STORE_WRITER, self.save)

    def export_json(self) -> str:
        return json.dumps(self._state(include_runtime=False), ensure_ascii=False, indent=2)

    def export_archive(self) -> bytes:
        archive = BytesIO()
        root = Path(self.settings.data_dir).resolve()
        with ZipFile(archive, "w", ZIP_DEFLATED) as package:
            package.writestr("store.json", self.export_json())
            if root.exists():
                for path in root.rglob("*"):
                    if not path.is_file() or path.name == "store.json" or path.suffix.lower() == ".pkl":
                        continue
                    package.write(path, f"files/{path.relative_to(root).as_posix()}")
        return archive.getvalue()

    def restore_bytes(self, content: bytes) -> None:
        state = json.loads(content.decode("utf-8"))
        self._restore_state(state)

    def restore_archive(self, content: bytes) -> None:
        root = Path(self.settings.data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".restore-", dir=root))
        try:
            with ZipFile(BytesIO(content)) as package:
                names = package.namelist()
                if "store.json" not in names:
                    raise ValueError("backup store.json missing")
                total = 0
                for name in names:
                    if name.endswith("/"):
                        continue
                    if name != "store.json" and not name.startswith("files/"):
                        raise ValueError("invalid backup path")
                    if name != "store.json":
                        relative = name[6:]
                        if (
                            not self._safe_relative_path(relative)
                            or relative.replace("\\", "/").lower() == "store.json"
                            or Path(relative).suffix.lower() == ".pkl"
                        ):
                            raise ValueError("invalid backup path")
                    info = package.getinfo(name)
                    total += info.file_size
                    if total > self.settings.max_backup_bytes:
                        raise ValueError("backup too large")
                state = json.loads(package.read("store.json").decode("utf-8"))
                for name in names:
                    if name == "store.json" or name.endswith("/"):
                        continue
                    destination = temporary / name[6:]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(name) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
            self._restore_state(state)
            shutil.copytree(temporary, root, dirs_exist_ok=True)
        except (BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError, OSError, TypeError, ValueError):
            raise ValueError("invalid backup archive")
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _restore_state(self, state: dict[str, Any]) -> None:
        self._validate_state_paths(state)
        previous = self._state(include_runtime=True)
        try:
            self._apply_state(state, include_runtime=False)
            if not self.save():
                raise OSError("store state could not be persisted")
            self._needs_migration = False
        except Exception:
            self._apply_state(previous, include_runtime=True)
            raise

    def _validate_state_paths(self, state: dict[str, Any]) -> None:
        keys = []
        for item in state.get("documents", {}).values():
            keys.append(item.get("file_key"))
        keys.extend(item.get("file_key") for item in state.get("excel_snapshots", []))
        if any(key is not None and not self._safe_relative_path(str(key)) for key in keys):
            raise ValueError("invalid data path")

    def _safe_relative_path(self, value: str) -> bool:
        path = Path(value)
        if not value or path.is_absolute() or any(part in {".", ".."} for part in path.parts):
            return False
        root = Path(self.settings.data_dir).resolve()
        return (root / path).resolve().is_relative_to(root)

    def _state(self, *, include_runtime: bool = True) -> dict[str, Any]:
        with self.lock:
            state = {
                "version": 2,
                "users": {key: _user_state(value) for key, value in self.users.items()},
                "courses": {key: _asdict(value) for key, value in self.courses.items()},
                "enrollments": [[course_id, user_id, class_name] for (course_id, user_id), class_name in self.enrollments.items()],
                "questions": {key: _asdict(value) for key, value in self.questions.items()},
                "quiz_sessions": {key: _quiz_state(value) for key, value in self.quiz_sessions.items()},
                "assignments": self.assignments,
                "roll_calls": self.roll_calls,
                "excel_schedules": self.excel_schedules,
                "excel_snapshots": self.excel_snapshots,
                "results": {key: _result_state(value) for key, value in self.results.items()},
                "chat_history": self.chat_history,
                "usage_logs": self.usage_logs,
                "documents": self.documents,
                "graph_nodes": self.graph_nodes,
                "audit_logs": self.audit_logs,
                "mastery": [[user_id, course_id, point, value] for (user_id, course_id, point), value in self.mastery.items()],
                "reports": self.reports,
                "chat_quota": self.chat_quota,
            }
            if include_runtime:
                state["import_previews"] = self.import_previews
                state["sessions"] = {key: list(value) for key, value in self.sessions.items()}
                state["login_failures"] = {key: list(value) for key, value in self.login_failures.items()}
            return state

    def _load(self) -> bool:
        try:
            raw_state = self.persistence_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except (OSError, UnicodeDecodeError) as error:
            raise OSError("persistent store is unreadable; refusing to seed a new store") from error
        try:
            state = json.loads(raw_state)
            self._apply_state(state, include_runtime=True)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError, AttributeError) as error:
            raise OSError("persistent store is invalid; refusing to seed a new store") from error
        return True

    def _apply_state(self, state: dict[str, Any], *, include_runtime: bool = True) -> None:
        if not isinstance(state, dict):
            raise ValueError("invalid store state")
        version = state.get("version")
        if version not in {1, 2}:
            raise ValueError("unsupported store version")
        users = state.get("users", {})
        courses = state.get("courses", {})
        questions = state.get("questions", {})
        if not isinstance(users, dict) or not isinstance(courses, dict) or not isinstance(questions, dict):
            raise ValueError("invalid store state")
        with self.lock:
            self.users = {key: _user_from_state(value) for key, value in users.items()}
            self.courses = {key: Course(**value) for key, value in courses.items()}
            for course in self.courses.values():
                if not course.class_code:
                    course.class_code = self.new_class_code()
            self.enrollments = {
                (item[0], item[1]): item[2]
                for item in state.get("enrollments", [])
                if isinstance(item, list) and len(item) == 3
            }
            self.questions = {key: Question(**value) for key, value in questions.items()}
            self.quiz_sessions = {
                key: _quiz_from_state(value) for key, value in state.get("quiz_sessions", {}).items()
            }
            self.assignments = dict(state.get("assignments", {}))
            self.roll_calls = list(state.get("roll_calls", []))
            self.excel_schedules = dict(state.get("excel_schedules", {}))
            self.excel_snapshots = list(state.get("excel_snapshots", []))
            self.results = {
                key: _result_from_state(value) for key, value in state.get("results", {}).items()
            }
            self.chat_history = list(state.get("chat_history", []))
            self.usage_logs = list(state.get("usage_logs", []))
            self.documents = dict(state.get("documents", {}))
            self.graph_nodes = dict(state.get("graph_nodes", {}))
            self.audit_logs = list(state.get("audit_logs", []))
            self.mastery = {}
            for item in state.get("mastery", []):
                if not isinstance(item, list):
                    continue
                if len(item) == 4:
                    self.mastery[(item[0], item[1], item[2])] = item[3]
                elif len(item) == 3:
                    self.mastery[(item[0], None, item[1])] = item[2]
            self.reports = dict(state.get("reports", {}))
            self.chat_quota = dict(state.get("chat_quota", {}))
            if include_runtime:
                self.sessions = {
                    key: (value[0], float(value[1]))
                    for key, value in state.get("sessions", {}).items()
                    if isinstance(value, list) and len(value) == 2
                }
                self.login_failures = {
                    key: (int(value[0]), float(value[1]))
                    for key, value in state.get("login_failures", {}).items()
                    if isinstance(value, list) and len(value) == 2
                }
                self.import_previews = dict(state.get("import_previews", {}))
            else:
                self.sessions = {}
                self.login_failures = {}
                self.import_previews = {}
            if version == 1:
                self._migrate_learning_data()
                self.audit("learning_data_migrated", None, {"mastery_rebuilt": True})
                self._needs_migration = True
            else:
                self._needs_migration = False

    def _migrate_learning_data(self) -> None:
        demo_sessions = {
            item.get("detail", {}).get("session_id")
            for item in self.audit_logs
            if item.get("action") == "quiz_demo_random"
        }
        demo_sessions.discard(None)
        for session_id in demo_sessions:
            self.quiz_sessions.pop(session_id, None)
        self.results = {
            key: result for key, result in self.results.items()
            if result.session_id not in demo_sessions
        }
        self._rebuild_mastery()
        self.reports.clear()

    def rebuild_mastery(self) -> None:
        with self.lock:
            self._rebuild_mastery()
            self.reports.clear()

    def _rebuild_mastery(self) -> None:
        results_by_session = {result.session_id: result for result in self.results.values()}
        self.mastery = {}
        for session in sorted(self.quiz_sessions.values(), key=lambda item: item.started_at):
            result = results_by_session.get(session.id)
            if result is None:
                continue
            session_points: dict[str, list[float]] = {}
            for item in result.per_question:
                if item.get("method") == "manual_pending":
                    continue
                maximum = float(item.get("max_score", 0))
                rate = float(item.get("score", 0)) / maximum if maximum else 0
                for point in item.get("knowledge_points", []):
                    session_points.setdefault(point, []).append(rate)
            for point, rates in session_points.items():
                key = (session.user_id, session.course_id, point)
                current = self.mastery.get(key, {"mastery": 0.0, "attempts": 0})
                score_rate = sum(rates) / len(rates)
                current["mastery"] = round(
                    score_rate * 100 if not current["attempts"]
                    else 0.7 * current["mastery"] + 0.3 * score_rate * 100,
                    2,
                )
                current["attempts"] += 1
                current["last_updated"] = result.graded_at.isoformat()
                self.mastery[key] = current

    def create_session(self, sid: str, user_id: str, expires_at: float) -> None:
        with self.lock:
            self.sessions[sid] = (user_id, expires_at)

    def get_session(self, sid: str) -> tuple[str, float] | None:
        with self.lock:
            session = self.sessions.get(sid)
            if session and session[1] <= time.time():
                self.sessions.pop(sid, None)
                return None
            return session

    def pop_session(self, sid: str) -> tuple[str, float] | None:
        with self.lock:
            return self.sessions.pop(sid, None)

    def delete_user_sessions(self, user_id: str) -> None:
        with self.lock:
            for sid, session in list(self.sessions.items()):
                if session[0] == user_id:
                    del self.sessions[sid]

    def clear_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()

    def question_code_exists(self, code: str) -> bool:
        return any(question.code == code for question in self.questions.values())

    def find_user_by_username(self, username: str) -> User | None:
        return next((u for u in self.users.values() if u.username == username), None)

    def maxkb_api_keys(self) -> tuple[str, ...]:
        return (self.settings.maxkb_api_key,) if self.settings.maxkb_api_key else ()

    def login_lock_remaining(self, username: str) -> float:
        with self.lock:
            state = self.login_failures.get(username)
            if state is None:
                return 0.0
            _, locked_until = state
            now = time.time()
            if locked_until == 0:
                return 0.0
            if locked_until > now:
                return locked_until - now
            self.login_failures.pop(username, None)
            return 0.0

    def record_login_failure(self, username: str) -> tuple[int, bool]:
        with self.lock:
            if self.login_lock_remaining(username) > 0:
                state = self.login_failures[username]
                return state[0], True
            attempts = self.login_failures.get(username, (0, 0.0))[0] + 1
            locked_until = 0.0
            if attempts >= self.settings.login_failure_limit:
                locked_until = time.time() + self.settings.login_lock_seconds
            self.login_failures[username] = (attempts, locked_until)
            return attempts, bool(locked_until)

    def clear_login_failures(self, username: str) -> None:
        with self.lock:
            self.login_failures.pop(username, None)

    def can_access_course(self, user: User, course_id: str, *, teaching: bool = False) -> bool:
        course = self.courses.get(course_id)
        if course is None:
            return False
        if user.role == "admin":
            return True
        if user.role == "teacher":
            return course.teacher_id == user.id
        return not teaching and (course_id, user.id) in self.enrollments

    def courses_for_user(self, user: User) -> list[Course]:
        if user.role == "admin":
            return list(self.courses.values())
        if user.role == "teacher":
            return [course for course in self.courses.values() if course.teacher_id == user.id]
        return [
            course for course in self.courses.values()
            if (course.id, user.id) in self.enrollments
        ]

    def students_for_course(self, course_id: str) -> list[User]:
        student_ids = {
            user_id for enrolled_course, user_id in self.enrollments
            if enrolled_course == course_id
        }
        return [
            user for user in self.users.values()
            if user.id in student_ids and user.status == "active"
        ]

    # ---- 课堂代码与注册审核 ----

    def new_class_code(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        existing = {course.class_code for course in self.courses.values()}
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(6))
            if code not in existing:
                return code

    def find_course_by_class_code(self, code: str) -> Course | None:
        normalized = code.strip().upper()
        if not normalized:
            return None
        return next(
            (course for course in self.courses.values() if course.class_code and course.class_code.upper() == normalized),
            None,
        )

    def find_user_by_phone(self, phone: str) -> User | None:
        normalized = phone.strip()
        if not normalized:
            return None
        return next((user for user in self.users.values() if user.phone and user.phone == normalized), None)

    def registration_allowed(self, ip: str) -> tuple[bool, int]:
        """Per-IP registration throttle: (allowed, retry_after_seconds)."""
        if not ip:
            return True, 0
        now = time.time()
        window = self.settings.registration_window_seconds
        limit = self.settings.registration_rate_limit
        with self.lock:
            stamps = [stamp for stamp in self.registration_attempts.get(ip, []) if now - stamp < window]
            if len(stamps) >= limit:
                self.registration_attempts[ip] = stamps
                return False, int(window - (now - stamps[0])) + 1
            stamps.append(now)
            self.registration_attempts[ip] = stamps
            return True, 0

    def register_student(
        self,
        username: str,
        display_name: str,
        student_number: str,
        password: str,
        phone: str,
        course: Course,
        class_name: str = "",
    ) -> User:
        user = User(
            _id(), username, display_name, hash_password(password), "student",
            status="pending", phone=phone,
            requested_course_id=course.id, requested_class_name=class_name,
            requested_at=datetime.now(timezone.utc), student_number=student_number,
        )
        with self.lock:
            self.users[user.id] = user
        return user

    def pending_registrations_for(self, user: User) -> list[User]:
        allowed = (
            set(self.courses)
            if user.role == "admin"
            else {course.id for course in self.courses.values() if course.teacher_id == user.id}
        )
        rows = [
            item for item in self.users.values()
            if item.status == "pending" and item.requested_course_id in allowed
        ]
        return sorted(rows, key=lambda item: item.requested_at or datetime.min.replace(tzinfo=timezone.utc))

    def approve_registration(self, user_id: str, class_name: str | None = None) -> User | None:
        with self.lock:
            user = self.users.get(user_id)
            if user is None or user.status != "pending":
                return None
            course_id = user.requested_course_id
            if course_id and course_id in self.courses:
                name = (class_name or user.requested_class_name or "").strip() or "未分班"
                self.enrollments[(course_id, user.id)] = name
                user.requested_class_name = name
            user.status = "active"
            user.requested_at = None
            return user

    def reject_registration(self, user_id: str) -> User | None:
        with self.lock:
            user = self.users.get(user_id)
            if user is None or user.status != "pending":
                return None
            self.users.pop(user_id, None)
            return user

    def can_manage_registration(self, actor: User, target: User) -> bool:
        if actor.role == "admin":
            return True
        if actor.role != "teacher" or target.status != "pending":
            return False
        course = self.courses.get(target.requested_course_id or "")
        return course is not None and course.teacher_id == actor.id

    def audit(
        self,
        action: str,
        actor_id: str | None,
        detail: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        with self.lock:
            self.audit_logs.append({
                "id": len(self.audit_logs) + 1,
                "at": datetime.now(timezone.utc).isoformat(),
                "actor_id": actor_id,
                "action": action,
                "detail": detail or {},
                "ip": ip,
            })
            # 保留最近 N 条,防止内存无限增长
            max_logs = self.settings.log_max_entries
            if len(self.audit_logs) > max_logs:
                self.audit_logs[:] = self.audit_logs[-max_logs:]

    def record_usage(
        self,
        user_id: str,
        intent: str,
        model: str,
        latency_ms: int,
        outcome: str,
        course_id: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        with self.lock:
            self.usage_logs.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "course_id": course_id,
                "intent": intent,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": latency_ms,
                "outcome": outcome,
            })
            max_usage = self.settings.log_max_entries
            if len(self.usage_logs) > max_usage:
                self.usage_logs[:] = self.usage_logs[-max_usage:]

    def reserve_chat_call(self, user_id: str) -> bool:
        limit = self.settings.chat_daily_limit
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.lock:
            entry = self.chat_quota.get(user_id)
            if entry is None or entry.get("date") != today:
                self.chat_quota[user_id] = {"date": today, "count": 1}
                return True
            if int(entry.get("count", 0)) >= limit:
                return False
            entry["count"] += 1
            return True

    def release_chat_call(self, user_id: str) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.lock:
            entry = self.chat_quota.get(user_id)
            if entry is None or entry.get("date") != today:
                return
            count = max(int(entry.get("count", 0)) - 1, 0)
            if count:
                entry["count"] = count
            else:
                self.chat_quota.pop(user_id, None)

    def update_mastery(self, user_id: str, course_id: str | None, knowledge_point: str, score_rate: float) -> None:
        with self.lock:
            key = (user_id, course_id, knowledge_point)
            current = self.mastery.get(key, {"mastery": 0.0, "attempts": 0})
            current["mastery"] = round(
                score_rate * 100 if not current["attempts"]
                else 0.7 * current["mastery"] + 0.3 * score_rate * 100,
                2,
            )
            current["attempts"] += 1
            current["last_updated"] = datetime.now(timezone.utc).isoformat()
            self.mastery[key] = current

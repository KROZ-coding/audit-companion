from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import get_current_user, get_store, require_roles
from ..models import User
from ..schemas.api import CourseOut, EnrollmentCreate, RollCallCreate
from ..store import Store
from .helpers import user_out

router = APIRouter(prefix="/api/courses", tags=["courses"])


@router.get("", response_model=list[CourseOut])
def list_courses(user: User = Depends(get_current_user), store: Store = Depends(get_store)) -> list[CourseOut]:
    return [
        CourseOut(
            id=course.id, name=course.name, term=course.term,
            teacher_id=course.teacher_id, class_code=course.class_code,
        )
        for course in store.courses_for_user(user)
    ]


@router.post("/{course_id}/class-code", response_model=CourseOut)
def rotate_class_code(course_id: str, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> CourseOut:
    _check_teaching_access(course_id, user, store)
    course = store.courses[course_id]
    course.class_code = store.new_class_code()
    store.audit("class_code_rotate", user.id, {"course_id": course_id})
    return CourseOut(
        id=course.id, name=course.name, term=course.term,
        teacher_id=course.teacher_id, class_code=course.class_code,
    )


@router.get("/{course_id}/students")
def list_students(
    course_id: str,
    q: str = "",
    class_name: str | None = None,
    status_filter: str = Query(default="all", alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=1000),
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    _check_teaching_access(course_id, user, store)
    query = q.strip().casefold()
    students = []
    results_by_session = {item.session_id: item for item in store.results.values()}
    for student in store.students_for_course(course_id):
        if class_name and store.enrollments.get((course_id, student.id)) != class_name:
            continue
        if query and not any(query in value.casefold() for value in (student.display_name, student.username, student.student_number)):
            continue
        mastery = [
            value for (student_id, mastery_course, _), value in store.mastery.items()
            if student_id == student.id and mastery_course in {course_id, None}
        ]
        sessions = sorted(
            (item for item in store.quiz_sessions.values() if item.user_id == student.id and item.course_id == course_id),
            key=lambda item: item.started_at,
            reverse=True,
        )
        graded = [(session, results_by_session[session.id]) for session in sessions if session.id in results_by_session]
        pending_reviews = sum(result.status == "needs_review" for _, result in graded)
        weak_count = sum(item.get("attempts", 0) >= 2 and float(item.get("mastery", 0)) < 60 for item in mastery)
        overdue = sum(
            1 for session in sessions
            if session.assigned and session.status == "ongoing"
            and (assignment := store.assignments.get(session.assignment_id or "")) is not None
            and assignment.get("due_at")
            and datetime.fromisoformat(assignment["due_at"]) < datetime.now(timezone.utc)
        )
        attention = bool(weak_count or pending_reviews or overdue)
        if status_filter == "needs_attention" and not attention:
            continue
        if status_filter == "pending_review" and not pending_reviews:
            continue
        if status_filter == "overdue" and not overdue:
            continue
        if status_filter == "no_activity" and graded:
            continue
        latest = next((result for _, result in graded if result.status == "graded"), None)
        students.append({
            **user_out(student).model_dump(exclude={"phone", "requested_course_id", "requested_class_name", "requested_at"}),
            "class_name": store.enrollments.get((course_id, student.id), ""),
            "mastery_count": len(mastery),
            "weak_count": sum(item.get("attempts", 0) >= 2 and float(item.get("mastery", 0)) < 60 for item in mastery),
            "insufficient_count": sum(item.get("attempts", 0) < 2 for item in mastery),
            "pending_reviews": pending_reviews,
            "overdue_assignments": overdue,
            "completed_quizzes": len(graded),
            "latest_score": round(latest.total / latest.max_total * 100) if latest and latest.max_total else None,
            "attention": attention,
        })
    if status_filter not in {"all", "needs_attention", "pending_review", "overdue", "no_activity"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_student_filter", "message": "无效的学生筛选条件"})
    students.sort(key=lambda item: (item["display_name"].casefold(), item["student_number"], item["username"]))
    total = len(students)
    start = (page - 1) * page_size
    classes = sorted({name for (cid, _), name in store.enrollments.items() if cid == course_id and name})
    return {
        "course_id": course_id, "students": students[start:start + page_size],
        "page": page, "page_size": page_size, "total": total, "classes": classes,
    }


@router.get("/{course_id}/available-students")
def list_available_students(course_id: str, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> dict:
    _check_teaching_access(course_id, user, store)
    enrolled = {student_id for _, student_id in store.enrollments}
    current_course_students = {student.id for student in store.students_for_course(course_id)}
    teacher_course_ids = {course.id for course in store.courses_for_user(user)} if user.role == "teacher" else set()
    same_teacher_students = {
        student_id for enrolled_course, student_id in store.enrollments
        if enrolled_course in teacher_course_ids
    }
    return {
        "course_id": course_id,
        "students": [
            user_out(item).model_dump(exclude={"phone", "requested_course_id", "requested_class_name", "requested_at"})
            for item in store.users.values()
            if item.role == "student" and item.status == "active"
            and item.id not in current_course_students
            and (user.role == "admin" or item.id not in enrolled or item.id in same_teacher_students)
        ],
    }


@router.post("/{course_id}/students/{student_id}", status_code=status.HTTP_201_CREATED)
def enroll_student(
    course_id: str,
    student_id: str,
    payload: EnrollmentCreate,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    _check_teaching_access(course_id, user, store)
    student = store.users.get(student_id)
    if student is None or student.status != "active":
        raise HTTPException(status_code=404, detail={"code": "student_not_found", "message": "学生不存在或已禁用"})
    if student.role != "student":
        raise HTTPException(status_code=422, detail={"code": "not_student", "message": "只有学生可以加入课程"})
    key = (course_id, student_id)
    if key in store.enrollments:
        raise HTTPException(status_code=409, detail={"code": "already_enrolled", "message": "学生已经加入该课程"})
    store.enrollments[key] = payload.class_name.strip()
    store.audit("enrollment_create", user.id, {"course_id": course_id, "student_id": student_id})
    return {"course_id": course_id, "student": user_out(student), "class_name": store.enrollments[key]}


@router.delete("/{course_id}/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_student(
    course_id: str,
    student_id: str,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> None:
    _check_teaching_access(course_id, user, store)
    if store.enrollments.pop((course_id, student_id), None) is None:
        raise HTTPException(status_code=404, detail={"code": "enrollment_not_found", "message": "选课关系不存在"})
    store.audit("enrollment_delete", user.id, {"course_id": course_id, "student_id": student_id})


@router.get("/{course_id}/roll-call")
def list_roll_calls(course_id: str, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> list[dict]:
    _check_teaching_access(course_id, user, store)
    return [row for row in reversed(store.roll_calls) if row["course_id"] == course_id][:100]


@router.post("/{course_id}/roll-call", status_code=status.HTTP_201_CREATED)
def record_roll_call(
    course_id: str,
    payload: RollCallCreate,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Store = Depends(get_store),
) -> dict:
    _check_teaching_access(course_id, user, store)
    student = store.users.get(payload.student_id)
    if student is None or (course_id, student.id) not in store.enrollments:
        raise HTTPException(status_code=404, detail={"code": "student_not_found", "message": "该学生不属于当前课程"})
    row = {
        "id": str(uuid4()),
        "course_id": course_id,
        "student_id": student.id,
        "student_name": student.display_name,
        "student_number": student.student_number,
        "class_name": store.enrollments[(course_id, student.id)],
        "question": payload.question.strip(),
        "correct": payload.correct,
        "teacher_id": user.id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with store.lock:
        store.roll_calls.append(row)
        if len(store.roll_calls) > store.settings.log_max_entries:
            store.roll_calls[:] = store.roll_calls[-store.settings.log_max_entries:]
    store.audit("classroom_response_record", user.id, {"course_id": course_id, "student_id": student.id, "correct": payload.correct})
    return row


def _check_teaching_access(course_id: str, user: User, store: Store) -> None:
    if course_id not in store.courses:
        raise HTTPException(status_code=404, detail={"code": "course_not_found", "message": "课程不存在"})
    if not store.can_access_course(user, course_id, teaching=True):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权管理该课程"})

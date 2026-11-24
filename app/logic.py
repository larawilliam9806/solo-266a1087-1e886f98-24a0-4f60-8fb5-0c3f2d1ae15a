"""核心业务：底表导入、成绩单入账（上传/更正/撤销）、按学生重核并写入核定底表。

规则：
- 修读合规：开课前已结课（前置课结课日 < 本课开课日）且通过所有必修前置课，
  且作为依据的前置课成绩单本身也须修读合规（递归，由开课日先后自然成 DAG）。
- 合规且通过才计学分；同一学生同一门课只计一次（取结课最早、单号最小者）。
- 依据被撤销后重核：有合规旧单可替代则依据单号自动更新，否则受影响的后续课
  逐级重核为不合规，学分相应回收。
- 成绩与核定底表在同一事务入账；任何失败整体回滚，沿用原账。
"""
import json
from collections import defaultdict
from datetime import datetime, timezone


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value, field):
    if not isinstance(value, str):
        raise ApiError(f"{field} 必须是 YYYY-MM-DD 字符串")
    value = value.strip()
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ApiError(f"{field} 格式应为 YYYY-MM-DD：{value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(f"{field} 不是有效日期：{value!r}")
    return value


def parse_passed(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ApiError(f"passed 必须是 true/false：{value!r}")


# ---------------------------------------------------------------- 底表导入

def import_base_data(db, payload):
    """校验并导入课程/学分/前置课（幂等 upsert）。调用方负责事务。"""
    if not isinstance(payload, dict):
        raise ApiError("底表 JSON 顶层必须是对象")
    courses = payload.get("courses")
    if not isinstance(courses, list) or not courses:
        raise ApiError("courses 必须是非空数组")

    seen = set()
    rows = []
    for i, c in enumerate(courses, 1):
        if not isinstance(c, dict):
            raise ApiError(f"courses[{i}] 必须是对象")
        cid = c.get("course_id")
        name = c.get("name")
        if not isinstance(cid, str) or not cid.strip():
            raise ApiError(f"courses[{i}] 缺少 course_id")
        cid = cid.strip()
        if cid in seen:
            raise ApiError(f"课程号重复：{cid}")
        seen.add(cid)
        if not isinstance(name, str) or not name.strip():
            raise ApiError(f"课程 {cid} 缺少名称 name")
        try:
            credits = float(c.get("credits"))
        except (TypeError, ValueError):
            raise ApiError(f"课程 {cid} 的学分 credits 必须是数字")
        if credits < 0:
            raise ApiError(f"课程 {cid} 的学分不能为负")
        rows.append((cid, name.strip(), credits))

    prereqs = payload.get("prerequisites", [])
    if not isinstance(prereqs, list):
        raise ApiError("prerequisites 必须是数组")
    known = set(seen)
    known.update(r["course_id"] for r in db.execute("SELECT course_id FROM courses"))
    preq_rows = []
    for i, p in enumerate(prereqs, 1):
        if not isinstance(p, dict):
            raise ApiError(f"prerequisites[{i}] 必须是对象")
        cid, pcid = p.get("course_id"), p.get("prereq_course_id")
        if not isinstance(cid, str) or not isinstance(pcid, str):
            raise ApiError(f"prerequisites[{i}] 缺少 course_id / prereq_course_id")
        cid, pcid = cid.strip(), pcid.strip()
        if cid == pcid:
            raise ApiError(f"课程 {cid} 不能以自身为前置课")
        if cid not in known:
            raise ApiError(f"前置课定义中的课程 {cid} 不在课程底表内")
        if pcid not in known:
            raise ApiError(f"前置课 {pcid} 不在课程底表内")
        preq_rows.append((cid, pcid))

    for cid, name, credits in rows:
        db.execute(
            "INSERT INTO courses (course_id, name, credits) VALUES (?,?,?) "
            "ON CONFLICT(course_id) DO UPDATE SET name=excluded.name, credits=excluded.credits",
            (cid, name, credits),
        )
    for cid, pcid in preq_rows:
        db.execute(
            "INSERT OR IGNORE INTO prerequisites (course_id, prereq_course_id) VALUES (?,?)",
            (cid, pcid),
        )
    return {"courses": len(rows), "prerequisites": len(preq_rows)}


# ---------------------------------------------------------------- 重核引擎
#
# 内存边界：base context（课程学分 + 前置课规则）为 O(课程数 + 规则数)，
# 允许随底表增长；学生学号、该生成绩单均以键集/游标逐行流过，应用内不保留
# 随学生人数或单生单数增长的列表/字典。
#
# 取数复杂度：一项前置依据只走 idx_audit_evidence 覆盖索引做一次 LIMIT 1
# 的 B 树定位（等值：学生/前置课/合规/通过；有序：结课日, 单号），
# 取数与比较次数为 O(log 旧单数)，不随该前置课旧单总数线性增长。

# 取某前置课在“本单开课日”前已结课的最优依据单：
# 有效（核定表中只有有效单）、通过、本身合规；结课日最早，同日单号最小。
# 由 idx_audit_evidence 覆盖索引支撑，取一条即得，不随旧单数线性扫描。
EVIDENCE_SQL = """
SELECT record_id FROM record_audit
 WHERE student_id = ? AND course_id = ?
   AND compliant = 1 AND passed = 1 AND end_date < ?
 ORDER BY end_date ASC, record_id ASC
 LIMIT 1
"""

# 同一门课选计分单：合规且通过者中结课日最早、同日单号最小（不看开课先后）。
SCORING_SQL = """
SELECT record_id FROM record_audit
 WHERE student_id = ? AND course_id = ?
   AND compliant = 1 AND passed = 1
 ORDER BY end_date ASC, record_id ASC
 LIMIT 1
"""

ACTIVE_RECORDS_SQL = """
SELECT record_id, course_id, start_date, end_date, passed
  FROM records
 WHERE student_id = ? AND status = 'active'
 ORDER BY start_date ASC, record_id ASC
"""


def load_base_context(db):
    """底表上下文：学分表 + 每门课的必修前置课。规模 O(课程 + 规则)。"""
    credits_of = {
        r["course_id"]: r["credits"]
        for r in db.execute("SELECT course_id, credits FROM courses")
    }
    prereqs = defaultdict(list)
    # 不显式排序：沿用旧版按底表写入（rowid）顺序输出逐前置课依据，保持接口形态稳定。
    for r in db.execute("SELECT course_id, prereq_course_id FROM prerequisites"):
        prereqs[r["course_id"]].append(r["prereq_course_id"])
    return credits_of, dict(prereqs)


def iter_student_ids(db):
    """键集逐一生成学号：应用内只保留“上一个学号”标量，不一次载入全部学号。"""
    last_id = None
    while True:
        if last_id is None:
            row = db.execute(
                "SELECT student_id FROM records ORDER BY student_id LIMIT 1"
            ).fetchone()
        else:
            row = db.execute(
                "SELECT student_id FROM records WHERE student_id > ? "
                "ORDER BY student_id LIMIT 1",
                (last_id,),
            ).fetchone()
        if row is None:
            return
        last_id = row["student_id"]
        yield last_id


def recompute_all_students(db, base=None):
    """底表导入后的全量重核：学号键集流过，底表上下文只载一次。调用方负责事务。"""
    if base is None:
        base = load_base_context(db)
    count = 0
    for student_id in iter_student_ids(db):
        recompute_student(db, student_id, base=base)
        count += 1
    return count


def recompute_student(db, student_id, base=None):
    """重核一名学生并整体重写其核定底表。调用方负责事务（失败需回滚）。

    全程逐行流式处理该生成绩单，不在应用内存堆积该生的全部旧单。
    """
    now = now_iso()
    if base is None:
        base = load_base_context(db)
    credits_of, prereqs = base

    db.execute("DELETE FROM record_audit WHERE student_id = ?", (student_id,))
    db.execute("DELETE FROM course_credits WHERE student_id = ?", (student_id,))
    db.execute("DELETE FROM student_credits WHERE student_id = ?", (student_id,))

    # 第一遍：按（开课日, 单号）逐行核定合规性与前置依据，并立即写入核定表
    # （counted 先置 0，第二遍再标记计分单）。
    # 依据单须满足 end_date < 本课 start_date，而合法单据 start_date <= end_date，
    # 故依据单开课日必然更早、已在本遍先处理并写入合规结论，递归合规自然成立。
    records_checked = 0
    audit_cur = db.execute(ACTIVE_RECORDS_SQL, (student_id,))
    for r in audit_cur:
        rid = r["record_id"]
        start = r["start_date"]
        evidence, ok = {}, True
        for pc in prereqs.get(r["course_id"], ()):  # 规则数，O(前置规则)
            ev = db.execute(EVIDENCE_SQL, (student_id, pc, start)).fetchone()
            ev_rid = ev["record_id"] if ev is not None else None
            evidence[pc] = ev_rid
            if ev_rid is None:
                ok = False
        compliant = 1 if ok else 0
        db.execute(
            "INSERT INTO record_audit (record_id, student_id, course_id, passed,"
            " compliant, counted, evidence, end_date, updated_at)"
            " VALUES (?,?,?,?,?,0,?,?,?)",
            (rid, student_id, r["course_id"], r["passed"], compliant,
             json.dumps(evidence, ensure_ascii=False), r["end_date"], now),
        )
        records_checked += 1

    # 第二遍：逐门出现过的课程做一次索引 LIMIT 1 选计分单。
    # DISTINCT 结果规模不超过底表课程数；这里同样逐行流过，不 fetchall 缓冲。
    source_of = {}
    total = 0.0
    course_cur = db.execute(
        "SELECT DISTINCT course_id FROM records "
        "WHERE student_id = ? AND status = 'active'",
        (student_id,),
    )
    for c_row in course_cur:
        cid = c_row["course_id"]
        win = db.execute(SCORING_SQL, (student_id, cid)).fetchone()
        if win is None:
            continue
        rid = win["record_id"]
        source_of[cid] = rid
        db.execute(
            "UPDATE record_audit SET counted = 1 WHERE record_id = ?", (rid,)
        )
        db.execute(
            "INSERT INTO course_credits (student_id, course_id, source_record_id)"
            " VALUES (?,?,?)",
            (student_id, cid, rid),
        )
        total += credits_of.get(cid, 0.0)

    total = round(total, 6)
    db.execute(
        "INSERT INTO student_credits (student_id, total_credits, updated_at)"
        " VALUES (?,?,?)",
        (student_id, total, now),
    )
    return {
        "student_id": student_id,
        "total_credits": total,
        "credited_courses": len(source_of),
        "records_checked": records_checked,
    }


# ---------------------------------------------------------------- 成绩单入账

def _validate_record_payload(db, rec, idx, seen_in_batch):
    if not isinstance(rec, dict):
        raise ApiError(f"records[{idx}] 必须是对象")
    rid = rec.get("record_id")
    if not isinstance(rid, str) or not rid.strip():
        raise ApiError(f"records[{idx}] 缺少单号 record_id")
    rid = rid.strip()
    if rid in seen_in_batch:
        raise ApiError(f"批内单号重复：{rid}")
    seen_in_batch.add(rid)

    cid = rec.get("course_id")
    if not isinstance(cid, str) or not cid.strip():
        raise ApiError(f"单号 {rid} 缺少课程 course_id")
    cid = cid.strip()
    if not db.execute("SELECT 1 FROM courses WHERE course_id = ?", (cid,)).fetchone():
        raise ApiError(f"单号 {rid} 的课程 {cid} 不在课程底表内")

    start = parse_date(rec.get("start_date"), f"单号 {rid} 的开课日 start_date")
    end = parse_date(rec.get("end_date"), f"单号 {rid} 的结课日 end_date")
    if end < start:
        raise ApiError(f"单号 {rid} 结课日早于开课日")
    passed = parse_passed(rec.get("passed"))
    return rid, cid, start, end, passed


def apply_transcript_batch(db, payload, batch_id):
    """分批上传：新单入账、同号单更正；随后只复核该生。调用方负责事务。"""
    if not isinstance(payload, dict):
        raise ApiError("成绩单 JSON 顶层必须是对象")
    student_id = payload.get("student_id")
    if not isinstance(student_id, str) or not student_id.strip():
        raise ApiError("缺少学号 student_id")
    student_id = student_id.strip()
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ApiError("records 必须是非空数组")

    now = now_iso()
    seen, added, corrected = set(), 0, 0
    for i, rec in enumerate(records, 1):
        rid, cid, start, end, passed = _validate_record_payload(db, rec, i, seen)
        old = db.execute("SELECT student_id, status FROM records WHERE record_id = ?", (rid,)).fetchone()
        if old is None:
            db.execute(
                "INSERT INTO records (record_id, student_id, course_id, start_date, end_date, passed,"
                " status, batch_id, created_at, updated_at) VALUES (?,?,?,?,?,?,'active',?,?,?)",
                (rid, student_id, cid, start, end, passed, batch_id, now, now),
            )
            added += 1
        else:
            if old["student_id"] != student_id:
                raise ApiError(f"单号 {rid} 属于其他学生，不能更正")
            if old["status"] != "active":
                raise ApiError(f"单号 {rid} 已撤销，不能更正")
            db.execute(
                "UPDATE records SET course_id=?, start_date=?, end_date=?, passed=?, batch_id=?, updated_at=?"
                " WHERE record_id = ?",
                (cid, start, end, passed, batch_id, now, rid),
            )
            corrected += 1

    result = recompute_student(db, student_id)
    result.update({"batch_id": batch_id, "added": added, "corrected": corrected})
    return result


def revoke_record(db, record_id):
    """按单号撤销，随后只复核该生（替代/连锁重核在 recompute 内完成）。"""
    row = db.execute("SELECT student_id, status FROM records WHERE record_id = ?", (record_id,)).fetchone()
    if row is None:
        raise ApiError(f"单号 {record_id} 不存在", 404)
    if row["status"] == "revoked":
        raise ApiError(f"单号 {record_id} 已是撤销状态", 409)
    db.execute(
        "UPDATE records SET status='revoked', updated_at=? WHERE record_id = ?",
        (now_iso(), record_id),
    )
    return recompute_student(db, row["student_id"])


# ---------------------------------------------------------------- 查询（只读核定底表）

def student_snapshot(db, student_id):
    courses = {r["course_id"]: r for r in db.execute("SELECT * FROM courses")}
    sc = db.execute(
        "SELECT total_credits, updated_at FROM student_credits WHERE student_id = ?", (student_id,)
    ).fetchone()
    cc = db.execute(
        "SELECT course_id, source_record_id FROM course_credits WHERE student_id = ? ORDER BY course_id",
        (student_id,),
    ).fetchall()
    recs = db.execute(
        "SELECT r.record_id, r.course_id, r.start_date, r.end_date, r.passed, r.status, r.batch_id,"
        "       a.compliant, a.counted, a.evidence"
        " FROM records r LEFT JOIN record_audit a ON a.record_id = r.record_id"
        " WHERE r.student_id = ? ORDER BY r.start_date, r.record_id",
        (student_id,),
    ).fetchall()

    out_records = []
    for r in recs:
        evidence = []
        if r["evidence"]:
            for pc, rid in json.loads(r["evidence"]).items():
                prow = courses.get(pc)
                evidence.append({
                    "prereq_course_id": pc,
                    "prereq_name": prow["name"] if prow else pc,
                    "record_id": rid,          # None 表示缺项
                    "missing": rid is None,
                })
        out_records.append({
            "record_id": r["record_id"],
            "course_id": r["course_id"],
            "course_name": courses[r["course_id"]]["name"] if r["course_id"] in courses else r["course_id"],
            "start_date": r["start_date"],
            "end_date": r["end_date"],
            "passed": bool(r["passed"]),
            "status": r["status"],
            "compliant": None if r["compliant"] is None else bool(r["compliant"]),
            "counted": None if r["counted"] is None else bool(r["counted"]),
            "evidence": evidence,
        })

    return {
        "student_id": student_id,
        "total_credits": sc["total_credits"] if sc else 0,
        "ledger_updated_at": sc["updated_at"] if sc else None,
        "credited_courses": [
            {
                "course_id": row["course_id"],
                "name": courses[row["course_id"]]["name"] if row["course_id"] in courses else row["course_id"],
                "credits": courses[row["course_id"]]["credits"] if row["course_id"] in courses else None,
                "source_record_id": row["source_record_id"],
            }
            for row in cc
        ],
        "records": out_records,
    }

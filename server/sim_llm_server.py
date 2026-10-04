"""模拟学期用的假 OpenAI 兼容模型服务(零真实 API 消耗)。

compose 里作为 sim-llm 服务运行,应用通过 LLM_BASE_URL 指向它。
按请求提示词里的特征词返回不同形态的回答:
  阅卷教师   → rubric 逐点评分 JSON(_llm_rubric_grade 的契约)
  题库教师   → AI 出题 JSON(bank/generate 的契约)
  命助/练习  → AI 练习 JSON(practice/generate 的契约)
  学情报告   → Markdown 文本(progress/report 的契约)
  教学助教   → 简短 Markdown 文本(progress/ai-insight 的契约)
  其余       → 答疑结构化 JSON(chat_service 的契约)
"""

import argparse
import hashlib
import json
import random
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHAT_TEMPLATES = [
    ("审计证据是指注册会计师为了得出审计结论、形成审计意见而使用的必要信息。"
     "按外部证据优于内部证据、直接获取优于间接获取的原则评价其可靠性。"),
    ("该程序的执行需要保持职业怀疑,关注管理层凌驾于内部控制之上的风险,"
     "并对异常项目扩大样本或执行追加程序。"),
    ("相关准则要求注册会计师在计划和执行审计工作时,结合重要性水平评估"
     "识别出的错报是否重大,并考虑其性质与金额两个维度。"),
]
MIND_MAP = ["核心概念", "├─ 定义与适用情形", "├─ 执行要点", "├─ 常见误区", "└─ 准则依据"]
FILLER_SHORT = "大概就是数量和质量的区别,具体展开记不清了。"


def _points_from_prompt(text: str) -> list[dict]:
    names = re.findall(r"'point':\s*'([^']+)'", text)
    scores = re.findall(r"'max_score':\s*([0-9.]+)", text)
    points = []
    for index, name in enumerate(names):
        try:
            max_score = float(scores[index]) if index < len(scores) else 5.0
        except ValueError:
            max_score = 5.0
        points.append({"point": name, "max_score": max_score})
    return points


def _question_count(text: str, default: int = 3) -> int:
    match = re.search(r"生成\s*(\d+)\s*道", text)
    return min(max(int(match.group(1)), 1), 10) if match else default


def _stable(text: str) -> int:
    return int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)


def rubric_json(text: str) -> str:
    points = _points_from_prompt(text)
    # 回答像样(长度足)给满分,简短作答给部分分,让教师复核有修正空间
    generous = "学生答案：" in text and len(text.split("学生答案：", 1)[1].strip()) >= 24
    graded = [
        {"point": item["point"], "score": item["max_score"] if generous else round(item["max_score"] * 0.4, 1),
         "matched": generous}
        for item in points
    ]
    return json.dumps({"points": graded, "why": "模型按评分要点批改(模拟)"}, ensure_ascii=False)


def bank_questions_json(text: str) -> str:
    count = _question_count(text, 3)
    match = re.search(r"生成\s*\d+\s*道\s*(\w+)", text)
    qtype = match.group(1) if match else "single_choice"
    questions = []
    for index in range(count):
        stem = f"【模拟出题{index + 1}】下列关于该知识点的说法,正确的是:"
        if qtype in {"single_choice", "multi_choice"}:
            questions.append({
                "stem": stem, "options": ["说法甲(正确表述)", "说法乙", "说法丙", "说法丁"],
                "answer": [0] if qtype == "multi_choice" else 0,
                "reference_answer": "说法甲正确,其余均违背准则要求。",
                "rubric": [], "explanation": "依据准则相关条款(模拟)。",
            })
        elif qtype == "judge":
            questions.append({"stem": f"【模拟出题{index + 1}】职业怀疑贯穿审计过程始终。", "options": [],
                              "answer": True, "reference_answer": "正确", "rubric": [], "explanation": "准则要求(模拟)。"})
        elif qtype == "fill":
            questions.append({"stem": f"【模拟出题{index + 1}】可靠性最高的外部证据是____。", "options": [],
                              "answer": "银行询证函回函", "reference_answer": "银行询证函回函", "rubric": [],
                              "explanation": "外部直接获取证据(模拟)。"})
        else:
            questions.append({"stem": f"【模拟出题{index + 1}】请简述该知识点的核心要求。", "options": [],
                              "answer": None, "reference_answer": "要点一、要点二、要点三。",
                              "rubric": [{"point": "要点一", "score": 5}, {"point": "要点二", "score": 5}],
                              "explanation": "见教材对应章节(模拟)。"})
    return json.dumps({"questions": questions}, ensure_ascii=False)


def practice_questions_json(text: str) -> str:
    count = _question_count(text, 3)
    types = ["single_choice", "judge", "fill", "multi_choice"]
    questions = []
    for index in range(count):
        qtype = types[index % len(types)]
        if qtype == "single_choice":
            item = {"type": qtype, "stem": f"【模拟练习{index + 1}】审计证据可靠性的判断依据是:",
                    "options": ["来源与性质", "取得时间", "纸张质量", "份数多少"], "answer": 0,
                    "reference_answer": "证据可靠性取决于来源与性质。", "knowledge_points": ["审计证据可靠性"]}
        elif qtype == "multi_choice":
            item = {"type": qtype, "stem": f"【模拟练习{index + 1}】下列属于审计程序的有:",
                    "options": ["检查", "观察", "函证", "重新计算"], "answer": [0, 1, 2, 3],
                    "reference_answer": "四项均属于审计程序。", "knowledge_points": ["审计程序"]}
        elif qtype == "judge":
            item = {"type": qtype, "stem": f"【模拟练习{index + 1}】重要性水平越高,所需审计证据越少。",
                    "options": [], "answer": True, "reference_answer": "正确。", "knowledge_points": ["重要性"]}
        else:
            item = {"type": qtype, "stem": f"【模拟练习{index + 1}】可靠性最高的审计证据是____。",
                    "options": [], "answer": "银行询证函回函", "reference_answer": "银行询证函回函。",
                    "knowledge_points": ["审计证据"]}
        questions.append(item)
    return json.dumps({"questions": questions}, ensure_ascii=False)


def report_markdown(text: str) -> str:
    return (
        "# 学情报告(模拟)\n\n## 掌握情况\n"
        "- 部分知识点掌握度稳步上升,客观题正确率高于主观题。\n"
        "- 个别知识点作答次数不足,样本偏少。\n\n## 薄弱环节\n"
        "- 审计证据充分性与适当性的辨析。\n\n## 学习建议\n"
        "- 每讲结束后完成对应客观题并复述主观题要点。\n"
        "- 对错题涉及的知识点向助教追问一次。\n"
    )


def insight_markdown(text: str) -> str:
    return (
        "整体来看,班级测验完成度尚可,答疑活跃。建议优先关注待复核名单与"
        "薄弱知识点(见服务端统计),对长期未作答的学生单独提醒。(模拟解读)"
    )


def chat_json(text: str) -> str:
    question = ""
    if "学生问题：" in text:
        question = text.split("学生问题：", 1)[1].strip()[:40]
    index = _stable(question) % len(CHAT_TEMPLATES)
    payload = {
        "answer_markdown": f"关于「{question}」:{CHAT_TEMPLATES[index]}",
        "sections": {
            "conclusion": CHAT_TEMPLATES[index],
            "standards": "依据中国注册会计师审计准则相关条款(模拟出处)。",
            "case": "实务中可结合上市公司年报审计案例理解(模拟案例)。",
            "ideology": "诚信为本、保持职业怀疑,是审计人员的职业底线。",
        },
        "mind_map": [MIND_MAP[0], *MIND_MAP[1:]],
    }
    return json.dumps(payload, ensure_ascii=False)


def build_content(body: dict) -> str:
    text = "\n".join(str(message.get("content") or "") for message in body.get("messages", []))
    if "阅卷教师" in text:
        return rubric_json(text)
    if "题库教师" in text:
        return bank_questions_json(text)
    if "命题助手" in text or "练习题" in text:
        return practice_questions_json(text)
    if "学情报告" in text and "掌握情况" in text:
        return report_markdown(text)
    if "教学助教" in text:
        return insight_markdown(text)
    return chat_json(text)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in {"/healthz", "/"}:
            self._reply(200, json.dumps({"status": "ok"}))
        else:
            self._reply(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(400, json.dumps({"error": "bad json"}))
            return
        content = build_content(body)
        time.sleep(random.uniform(0.05, 0.25))  # 模拟真实延迟,让 usage_logs 的 latency 有分布
        self._reply(200, json.dumps({
            "id": f"chatcmpl-sim-{_stable(content) % 10**10}",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": max(len(str(body.get("messages", ""))) // 2, 16),
                      "completion_tokens": max(len(content) // 2, 8)},
        }, ensure_ascii=False))

    def _reply(self, code: int, text: str):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 静默访问日志
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"sim-llm listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

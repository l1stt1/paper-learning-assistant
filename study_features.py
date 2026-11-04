"""Persistent study preparation without changing personal learning records."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlencode
import uuid

import fitz

from library_features import ANSWER_FIELDS, COMPARISON_FIELDS
from zotero_integration import ZoteroError, attachment_title, item_key
from profile_store import profile_run_path


ARTICLE_MODULES = ("sources", "guide", "concepts", "comparison")
CROSS_MODULES = ("relations", "summary")
DONE = {"completed", "existing"}


def study_time():
    return datetime.now(timezone.utc).isoformat()


def module_complete(content, module):
    if module == "guide":
        return (isinstance(content.get("overview"), dict) and isinstance(content.get("sections"), list)
                and bool(content["sections"]) and all(isinstance(row, dict) and row.get("id") and row.get("title") for row in content["sections"]))
    if module == "concepts":
        return isinstance(content.get("concepts"), list) and bool(content["concepts"]) and all(isinstance(row, dict) and row.get("id") and row.get("term") for row in content["concepts"])
    if module == "comparison":
        value = content.get("comparison", {})
        return isinstance(value, dict) and all(isinstance(value.get(key), dict) and isinstance(value[key].get("text"), str)
                                              and value[key]["text"].strip() and "尚未整理" not in value[key]["text"] for key in COMPARISON_FIELDS)
    if module == "sources":
        coverage = content.get("source_coverage")
        return (isinstance(coverage, dict) and bool(coverage)) or all(module_complete(content, key) for key in ARTICLE_MODULES[1:])
    raise ValueError("学习内容模块无效")


def module_state(status="pending", message="等待整理"):
    return {"status": status, "message": message}


class StudyFeatures:
    def init_study(self):
        self.study_job_path = self.data_root / "study-jobs.json"
        self.study_network_path = self.data_root / "study-network.json"
        stored = json.loads(self.study_job_path.read_text(encoding="utf-8")) if self.study_job_path.exists() else {"jobs": []}
        self.study_jobs = {job["id"]: job for job in stored["jobs"]}
        self.study_network = json.loads(self.study_network_path.read_text(encoding="utf-8")) if self.study_network_path.exists() else {"relations": [], "comparisons": []}
        self.study_active_job = None
        interrupted = deepcopy(self.study_jobs)
        changed = False
        for job in interrupted.values():
            if job["status"] in {"queued", "running"}:
                changed = True
                job.update(status="interrupted", message="服务已重启，成功模块已保留；请主动继续。", updated_at=study_time())
                for states in [*[row["modules"] for row in job["articles"]], job["cross"]]:
                    for state in states.values():
                        if state["status"] == "running":
                            state.update(status="pending", message="服务中断，待继续")
        if changed:
            self._study_write_json(self.study_job_path, {"jobs": list(interrupted.values())})
            self.study_jobs = interrupted

    @staticmethod
    def _study_write_json(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(path.name + ".pending-" + uuid.uuid4().hex)
        pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Windows may briefly deny an atomic replacement. Never remove the old file.
        for attempt in range(4):
            try:
                os.replace(pending, path)
                return
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _study_update_job(self, job_id, update):
        with self.lock:
            values = deepcopy(self.study_jobs)
            update(values[job_id])
            values[job_id]["updated_at"] = study_time()
            self._study_write_json(self.study_job_path, {"jobs": list(values.values())})
            self.study_jobs = values

    def _study_public_job(self, job):
        value = deepcopy({key: item for key, item in job.items() if not key.startswith("_")})
        if value["status"] in {"queued", "running"} and self.study_active_job != value["id"]:
            value.update(status="interrupted", message="任务未在运行；成功内容已保留，可主动继续。")
        return value

    def study_job(self, job_id):
        with self.lock:
            if job_id not in self.study_jobs:
                raise ValueError("学习内容整理任务不存在，请刷新状态")
            return {"job": self._study_public_job(self.study_jobs[job_id])}

    def study_article_modules(self, aid):
        self.article(aid)
        with self.lock:
            content = self.content[aid]
            states = {key: module_state("existing", "已有学习内容，本次默认保留") if module_complete(content, key) else module_state() for key in ARTICLE_MODULES}
            for job in sorted(self.study_jobs.values(), key=lambda row: row["created_at"]):
                row = next((row for row in job["articles"] if row["article_id"] == aid), None)
                if row is None:
                    continue
                for key, state in row["modules"].items():
                    if state["status"] == "completed" and module_complete(content, key):
                        states[key] = deepcopy(state)
                    elif self.study_active_job == job["id"] and state["status"] == "running":
                        states[key] = deepcopy(state)
                    elif not module_complete(content, key) and state["status"] == "failed":
                        states[key] = deepcopy(state)
            return states

    def study_status(self):
        with self.lock:
            articles = []
            for aid in self.articles:
                content = self.content[aid]
                review = content.get("study_review")
                review_status = ((review.get("label") or review.get("message")) if isinstance(review, dict) else None) or (
                    "AI整理；程序来源定位检查不等于人工科学核查" if review else
                    "已有学习内容，本轮未重新核查原文" if module_complete(content, "guide") else "尚未整理")
                updated = max((job["updated_at"] for job in self.study_jobs.values() if aid in job["article_ids"]), default="")
                articles.append({"article_id": aid, "modules": self.study_article_modules(aid), "review_status": review_status, "updated_at": updated})
            return {"articles": articles, "jobs": [self._study_public_job(job) for job in sorted(self.study_jobs.values(), key=lambda row: row["created_at"], reverse=True)],
                    "ai_available": self.ai_available}

    def _study_material_catalog(self, aid, include_zotero=True):
        article = self.article(aid)
        rows, paths, warnings = [], {}, []
        for asset in self.asset_records(aid):
            key = asset["id"]
            if key in paths:
                raise ValueError("文章附件标识重复，请先核对材料清单")
            path = (self.root / asset["path"]).resolve()
            is_pdf = asset.get("kind") == "pdf"
            row = {"id": key, "label": asset.get("label") or path.name, "kind": "pdf" if is_pdf else "other",
                   "available": path.is_file(), "selected": key == "reading",
                   "url": "/api/asset?" + urlencode({"id": aid, "asset": key})}
            if is_pdf and isinstance(asset.get("pages"), int):
                row["pages"] = asset["pages"]
            rows.append(row)
            paths[key] = path
            if not is_pdf:
                warnings.append(row["label"] + "：非PDF附件，本次不解析。")
        link = self.zotero_links["articles"].get(aid)
        if include_zotero and link:
            try:
                api = self.zotero_client(bound=True)
                parent = item_key(link["item_key"])
                for entry in api.get_all(f"items/{parent}/children?itemType=attachment"):
                    data = entry["data"]
                    if data.get("itemType") != "attachment" or data.get("deleted") or data.get("parentItem") != parent or data.get("contentType") != "application/pdf":
                        continue
                    key = "zotero:" + item_key(data["key"])
                    if key in paths:
                        raise ValueError("文章附件标识重复，请先核对材料清单")
                    row = {"id": key, "label": "Zotero附件：" + attachment_title(data), "kind": "pdf", "available": False,
                           "selected": False, "url": "/api/zotero/file?" + urlencode({"id": aid, "attachment": data["key"]})}
                    try:
                        path = api.local_file(data["key"])
                        row["available"] = path.is_file()
                        paths[key] = path
                    except (ZoteroError, OSError):
                        paths[key] = None
                    rows.append(row)
            except (ZoteroError, OSError):
                warnings.append("Zotero当前不可用；可仅使用助手已有主阅读PDF整理。")
        if not any(row["id"] == "reading" and row["available"] for row in rows):
            warnings.append("主阅读PDF当前不可读取，整理前需恢复该文件。")
        return rows, paths, warnings

    def study_materials(self, aid, selected_ids=None):
        selected = ["reading"] if selected_ids is None else selected_ids
        if (not isinstance(selected, list) or any(not isinstance(key, str) for key in selected)
                or len(set(selected)) != len(selected) or "reading" not in selected):
            raise ValueError("请保留主阅读PDF，并选择不重复的已知附件")
        rows, paths, _ = self._study_material_catalog(aid, include_zotero=any(key.startswith("zotero:") for key in selected))
        known = {row["id"]: row for row in rows}
        materials = []
        for key in ["reading", *[key for key in selected if key != "reading"]]:
            if key not in known:
                raise ValueError("所选材料不在此文章的可读附件清单中；不能指定任意文件路径")
            row, path = known[key], paths[key]
            if row["kind"] != "pdf":
                raise ValueError("当前仅解析PDF，请取消选择非PDF附件")
            if not row["available"] or path is None:
                raise ValueError("所选PDF当前不可读取，请恢复附件或仅选择主阅读PDF")
            with fitz.open(path) as pdf:
                if pdf.needs_pass or not len(pdf):
                    raise ValueError("所选PDF需要密码或没有可读页面")
                pages = len(pdf)
            if key == "reading" and pages != self.article(aid)["pages"]:
                raise ValueError("主阅读PDF页数已变化，请先核对原有来源与批注对应关系")
            materials.append({"id": key, "label": row["label"], "path": path, "pages": pages, "primary": key == "reading"})
        return materials

    def _study_scope(self, article_ids, topic_id="", cross_article_ids=None):
        if not isinstance(article_ids, list) or any(not isinstance(aid, str) for aid in article_ids) or len(set(article_ids)) != len(article_ids):
            raise ValueError("请选择不重复的文章")
        cross = [] if cross_article_ids is None else cross_article_ids
        if not isinstance(cross, list) or (cross and (not 2 <= len(cross) <= 5 or any(not isinstance(aid, str) for aid in cross) or len(set(cross)) != len(cross))):
            raise ValueError("跨文整理需明确选择2至5篇不同文章")
        for aid in [*article_ids, *cross]:
            self.article(aid)
        topic = self.topic(topic_id) if topic_id else None
        if topic and not set([*article_ids, *cross]).issubset(topic["article_ids"]):
            raise ValueError("所选文章不全属于当前主题；跨主题整理需明确使用全部文章范围")
        return topic

    @staticmethod
    def _study_cross_scope(article_ids, topic_id):
        return json.dumps([topic_id, sorted(article_ids)], separators=(",", ":"))

    def study_cross_fingerprint(self, article_ids):
        """Identity of actual shared content/PDF inputs, never personal records."""
        from study_generation import cross_inputs
        self._study_scope([], cross_article_ids=article_ids)
        _, _, identity = cross_inputs(self, article_ids)
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()

    def _study_reusable_cross(self, article_ids, topic_id, signature, only_job=None):
        scope = self._study_cross_scope(article_ids, topic_id)
        reusable = {}
        versions = self.study_network.get("versions", {})
        for job_id, version in sorted(versions.items(), key=lambda item: (item[1]["created_at"], item[0]), reverse=True):
            if only_job is not None and job_id != only_job:
                continue
            if version["input_signature"] != signature or self._study_cross_scope(version["article_ids"], version["topic_id"]) != scope:
                continue
            for module in version["modules"]:
                reusable.setdefault(module, job_id)
        return reusable

    def _study_select_cross(self, network, article_ids, topic_id, selected):
        scope = self._study_cross_scope(article_ids, topic_id)
        network.setdefault("current", {}).setdefault(scope, {}).update(selected)

    def study_adopt_cross(self, job_id):
        """Explicit upgrade of a separately verified legacy result; never automatic."""
        with self.lock:
            job = self.study_jobs[job_id]
            ids = job["cross_article_ids"]
            if not ids or any(job["cross"][module]["status"] not in DONE for module in CROSS_MODULES):
                raise ValueError("只能为已核查且完整保存的旧跨文结果补充输入身份")
            rows = [row for row in self.study_network["relations"] if row.get("study_job_id") == job_id]
            summaries = [row for row in self.study_network["comparisons"] if row.get("study_job_id") == job_id]
            if (len(summaries) != 1 or summaries[0].get("status") != "completed" or not summaries[0].get("answer")
                    or any(set(row.get("article_ids", [])) != set(ids) or row.get("topic_id", "") != job["topic_id"] for row in [*rows, *summaries])):
                raise ValueError("旧跨文结果的文章范围、主题或已保存总结不完整")
            signature = self.study_cross_fingerprint(ids)
            version = {"article_ids": sorted(ids), "topic_id": job["topic_id"], "created_at": job["created_at"],
                       "input_signature": signature, "modules": list(CROSS_MODULES), "adopted_legacy": True}
            existing = self.study_network.get("versions", {}).get(job_id)
            if existing:
                if existing["input_signature"] != signature:
                    raise ValueError("该结果已有输入身份且当前输入已变化，不能重新定义旧结果；请新建整理")
                return deepcopy(existing)
            network = deepcopy(self.study_network)
            network.setdefault("versions", {})[job_id] = version
            self._study_select_cross(network, ids, job["topic_id"], {module: job_id for module in CROSS_MODULES})
            self._study_write_json(self.study_network_path, network)
            self.study_network = network
            return deepcopy(version)

    def _study_prepare_cross(self, job_id):
        job = self.study_jobs[job_id]
        signature = self.study_cross_fingerprint(job["cross_article_ids"])
        with self.lock:
            job = self.study_jobs[job_id]
            if job.get("_cross_signature") and job["_cross_signature"] != signature:
                raise ValueError("跨文输入已变化，请开始新的整理；已保存结果保留为历史")
            reusable = self._study_reusable_cross(job["cross_article_ids"], job["topic_id"], signature,
                                                  only_job=job_id if job["_regenerate"] else None)
            if any(state["status"] in DONE and module not in reusable for module, state in job["cross"].items()):
                raise ValueError("旧任务缺少可核对的跨文输入身份，请新建整理；原结果继续保留")
            if reusable:
                network = deepcopy(self.study_network)
                self._study_select_cross(network, job["cross_article_ids"], job["topic_id"], reusable)
                if network != self.study_network:
                    self._study_write_json(self.study_network_path, network)
                    self.study_network = network
            def update(value):
                value["_cross_signature"] = signature
                for module, source in reusable.items():
                    value["cross"][module] = {**module_state("completed" if source == job_id else "existing", "已有相同输入的跨文内容，本次保留"),
                                               "source_job_id": source}
            self._study_update_job(job_id, update)

    def study_preview(self, body):
        ids = body.get("article_ids", [])
        self._study_scope(ids, body.get("topic_id", ""))
        rows = []
        for aid in ids:
            materials, _, warnings = self._study_material_catalog(aid)
            rows.append({"article_id": aid, "title": self.article(aid)["title"], "modules": self.study_article_modules(aid),
                         "materials": materials, "warnings": warnings})
        return {"articles": rows, "topic_id": body.get("topic_id", ""), "ai_available": self.ai_available}

    def study_start(self, body):
        ids, cross = body.get("article_ids", []), body.get("cross_article_ids", [])
        topic = self._study_scope(ids, body.get("topic_id", ""), cross)
        if not ids and not cross:
            raise ValueError("请至少选择一篇需要整理的文章或2至5篇跨文文章")
        regenerate = body.get("regenerate", False)
        if not isinstance(regenerate, bool):
            raise ValueError("重新整理选项格式不正确")
        selection = body.get("materials", {})
        if not isinstance(selection, dict) or any(aid not in ids for aid in selection):
            raise ValueError("材料选择只能对应本次整理的文章")
        material_ids = {aid: [material["id"] for material in self.study_materials(aid, selection.get(aid))] for aid in ids}
        with self.lock:
            if self.study_active_job:
                raise ValueError("已有学习内容整理任务正在运行，请等待完成或停止后续处理")
            articles = []
            for aid in ids:
                states = {key: module_state() if regenerate or not module_complete(self.content[aid], key) else module_state("existing", "已有内容，本次保留") for key in ARTICLE_MODULES}
                if any(states[key]["status"] not in DONE for key in ARTICLE_MODULES[1:]):
                    states["sources"] = module_state("pending", "读取本次所选材料并更新实际覆盖范围")
                articles.append({"article_id": aid, "title": self.article(aid)["title"], "modules": states, "error": ""})
            ready = (cross and all(module_complete(self.content[aid], key) for aid in cross for key in ARTICLE_MODULES[1:])
                     and not any(row["article_id"] in cross and any(state["status"] not in DONE for state in row["modules"].values()) for row in articles))
            signature = self.study_cross_fingerprint(cross) if ready else None
            reusable = self._study_reusable_cross(cross, body.get("topic_id", ""), signature) if signature and not regenerate else {}
            cross_states = {key: {**module_state("existing", "已有相同输入的跨文内容，本次保留"), "source_job_id": reusable[key]}
                            if key in reusable else module_state() if cross else module_state("existing", "本次未选择跨文整理") for key in CROSS_MODULES}
            if (any(state["status"] not in DONE for state in cross_states.values()) or any(state["status"] not in DONE for row in articles for state in row["modules"].values())) and not self.ai_available:
                raise ValueError("智能整理暂不可用，请检查模型设置与运行环境")
            now, job_id = study_time(), "study-" + uuid.uuid4().hex
            job = {"id": job_id, "status": "queued", "topic_id": topic["id"] if topic else "", "topic_name": topic["name"] if topic else "",
                   "article_ids": list(ids), "cross_article_ids": list(cross), "created_at": now, "updated_at": now,
                   "stop_requested": False, "current_article_id": "", "current_module": "", "message": "等待开始整理", "error": "",
                   "articles": articles, "cross": cross_states,
                   "_materials": material_ids, "_regenerate": regenerate, "_cross_signature": signature}
            values = deepcopy(self.study_jobs)
            values[job_id] = job
            self._study_write_json(self.study_job_path, {"jobs": list(values.values())})
            self.study_jobs = values
            self._study_launch(job_id)
            return self.study_job(job_id)

    def _study_run_root(self, job_id):
        run = profile_run_path(self.root, "learning", "study-runs", job_id)
        job = self.study_jobs[job_id]
        if job.get("id") != job_id:
            raise ValueError("整理任务标识不一致，未启动处理；请检查文库记录。")
        for aid in job["article_ids"]:
            profile_run_path(self.root, "learning", "study-runs", job_id, aid)
        if job["cross_article_ids"]:
            profile_run_path(self.root, "learning", "study-runs", job_id, "cross")
        return run

    def _study_launch(self, job_id):
        self._study_run_root(job_id)
        self.study_active_job = job_id
        try:
            threading.Thread(target=self._study_run, args=(job_id,), daemon=True).start()
        except RuntimeError:
            try:
                self._study_update_job(job_id, lambda job: job.update(status="failed", error="无法启动整理任务，可主动继续", message="整理尚未启动"))
            finally:
                self.study_active_job = None

    def study_stop(self, job_id):
        with self.lock:
            job = self.study_job(job_id)["job"]
            if job["status"] in {"queued", "running"}:
                self._study_update_job(job_id, lambda job: job.update(stop_requested=True, message="将停止后续处理；当前请求完成后保留结果"))
            return self.study_job(job_id)

    def study_resume(self, job_id):
        with self.lock:
            job = self.study_job(job_id)["job"]
            self._study_run_root(job_id)
            if self.study_active_job == job_id or job["status"] == "completed":
                return self.study_job(job_id)
            if self.study_active_job:
                raise ValueError("另一个整理任务正在运行，请等待完成后再继续")
            if not self.ai_available:
                raise ValueError("智能整理暂不可用，请检查模型设置与运行环境")
            def reset(value):
                value.update(status="queued", stop_requested=False, error="", message="等待继续未完成模块")
                for row in value["articles"]:
                    row["error"] = ""
                    for state in row["modules"].values():
                        if state["status"] not in DONE:
                            state.update(status="pending", message="等待继续")
                for state in value["cross"].values():
                    if state["status"] not in DONE:
                        state.update(status="pending", message="等待继续")
            self._study_update_job(job_id, reset)
            self._study_launch(job_id)
            return self.study_job(job_id)

    def _study_states(self, job, aid):
        return next(row["modules"] for row in job["articles"] if row["article_id"] == aid) if aid else job["cross"]

    def _study_report(self, job_id, aid, module, status, message):
        if module not in (ARTICLE_MODULES if aid else CROSS_MODULES) or status not in {"pending", "running", "completed", "failed", "existing"}:
            raise ValueError("生成器返回了无效的模块状态")
        def update(job):
            state = self._study_states(job, aid)[module]
            if state["status"] not in DONE:
                if status == "completed":
                    state.update(status="running", message="内容已生成，等待保存")
                elif status == "existing":
                    if not aid or not module_complete(self.content[aid], module):
                        raise ValueError("缺少可复用的学习模块")
                    state.update(status="existing", message=str(message))
                else:
                    state.update(status=status, message=str(message))
            elif status in DONE:
                state["message"] = str(message)
            job.update(current_article_id=aid, current_module=module, message=str(message))
        self._study_update_job(job_id, update)

    def _study_publish_article(self, job_id, aid, module, payload, run_dir):
        fields = {"sources": {"source_coverage": dict, "study_review": dict}, "guide": {"overview": dict, "sections": list},
                  "concepts": {"concepts": list}, "comparison": {"comparison": dict}}
        if module not in fields or not isinstance(payload, dict) or set(payload) != set(fields[module]) or any(not isinstance(payload[key], expected) for key, expected in fields[module].items()):
            raise ValueError("生成模块的字段或类型不正确，原内容保留")
        if module == "sources" and (not payload["source_coverage"] or not payload["study_review"]):
            raise ValueError("来源覆盖或核查状态缺失，原内容保留")
        with self.lock:
            if self._study_states(self.study_jobs[job_id], aid)[module]["status"] in DONE:
                return
            target = (self.root / self.article(aid)["content"]).resolve()
            if not target.is_relative_to(self.root.resolve()):
                raise ValueError("学习内容文件不在本项目内，停止发布")
            original = target.read_bytes()
            current = json.loads(original)
            for key in ("sections", "concepts"):
                if key in payload:
                    entries = payload[key]
                    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] for row in entries):
                        raise ValueError("章节或概念缺少稳定标识，原内容保留")
                    ids = [row["id"] for row in entries]
                    if len(ids) != len(set(ids)) or not {row["id"] for row in current.get(key, [])}.issubset(ids):
                        raise ValueError("已有章节或概念标识不能丢失或重复，个人记录保持原样")
            content = {**current, **deepcopy(payload)}
            if not module_complete(content, module):
                raise ValueError("生成模块没有完整的可加载内容，原内容保留")
            backup = run_dir / "original-learning.json"
            if not backup.exists():
                run_dir.mkdir(parents=True, exist_ok=True)
                with backup.open("xb") as handle:
                    handle.write(original)
            self._study_write_json(target, content)
            self.content[aid] = content
            self._study_update_job(job_id, lambda job: self._study_states(job, aid)[module].update(status="completed", message="已保存并加载；AI整理内容仍需科学核查"))

    def _study_publish_cross(self, job_id, module, payload):
        if module not in CROSS_MODULES or not isinstance(payload, dict) or set(payload) != {module}:
            raise ValueError("跨文模块格式不正确")
        with self.lock:
            job = self.study_jobs[job_id]
            if job["cross"][module]["status"] in DONE:
                return
            if not job.get("_cross_signature"):
                raise ValueError("跨文输入身份尚未确认，停止发布并保留已有结果")
            ids = job["cross_article_ids"]
            network = deepcopy(self.study_network)
            if module == "relations":
                relations = payload[module]
                if not isinstance(relations, list):
                    raise ValueError("跨文关系列表格式不正确")
                values = []
                for index, relation in enumerate(relations):
                    if (not isinstance(relation, dict) or relation.get("source") not in ids or relation.get("target") not in ids or relation["source"] == relation["target"]
                            or relation.get("type") not in {"complement", "method", "definition", "hypothesis"}
                            or any(not isinstance(relation.get(key), str) for key in ("title", "explanation", "boundary"))
                            or not isinstance(relation.get("evidence"), list)):
                        raise ValueError("跨文关系的文章或字段不正确")
                    referenced = set()
                    for evidence in relation["evidence"]:
                        if not isinstance(evidence, dict) or evidence.get("article_id") not in {relation["source"], relation["target"]} or not isinstance(evidence.get("quote"), str):
                            raise ValueError("跨文关系来源不正确")
                        self.check_reference(evidence["article_id"], evidence.get("page"))
                        referenced.add(evidence["article_id"])
                    if referenced != {relation["source"], relation["target"]}:
                        raise ValueError("跨文关系需要双方来源")
                    values.append({**deepcopy(relation), "id": job_id + "-relation-" + str(index + 1), "origin": "generated", "study_job_id": job_id,
                                   "topic_id": job["topic_id"], "topic_name": job["topic_name"], "article_ids": list(ids), "created_at": job["created_at"]})
                network["relations"] = [row for row in network["relations"] if row.get("study_job_id") != job_id] + values
            else:
                answer = payload[module]
                if not isinstance(answer, dict) or any(not isinstance(answer.get(key), str) for key in ANSWER_FIELDS) or not isinstance(answer.get("citations"), list):
                    raise ValueError("跨文总结格式不正确")
                cited = set()
                for citation in answer["citations"]:
                    if not isinstance(citation, dict) or citation.get("article_id") not in ids or not isinstance(citation.get("quote"), str):
                        raise ValueError("跨文总结来源不正确")
                    self.check_reference(citation["article_id"], citation.get("page"))
                    cited.add(citation["article_id"])
                if cited != set(ids):
                    raise ValueError("跨文总结需覆盖所有所选文章的来源")
                record = {"id": job_id + "-summary", "origin": "study", "study_job_id": job_id, "topic_id": job["topic_id"], "topic_name": job["topic_name"],
                          "article_ids": list(ids), "created_at": job["created_at"], "question": "所选文章的研究主线、方法与发现有何联系？",
                          "status": "completed", "answer": deepcopy(answer), "error": ""}
                network["comparisons"] = [row for row in network["comparisons"] if row.get("study_job_id") != job_id] + [record]
            version = network.setdefault("versions", {}).setdefault(job_id, {
                "article_ids": sorted(ids), "topic_id": job["topic_id"], "created_at": job["created_at"],
                "input_signature": job["_cross_signature"], "modules": []})
            if module not in version["modules"]:
                version["modules"].append(module)
            self._study_select_cross(network, ids, job["topic_id"], {module: job_id})
            self._study_write_json(self.study_network_path, network)
            self.study_network = network
            self._study_update_job(job_id, lambda job: job["cross"][module].update(status="completed", message="已保存；AI整理内容仍需科学核查"))

    def _study_network_records(self, key, module, include_history):
        with self.lock:
            current = {value[module] for value in self.study_network.get("current", {}).values() if module in value}
            return [{**deepcopy(row), "is_current": row.get("study_job_id") in current} for row in self.study_network[key]
                    if include_history or row.get("study_job_id") in current]

    def study_relations(self, include_history=False):
        return self._study_network_records("relations", "relations", include_history)

    def study_comparisons(self, include_history=False):
        return self._study_network_records("comparisons", "summary", include_history)

    def _study_fail_module(self, job_id, aid, error):
        def update(job):
            states = self._study_states(job, aid)
            current = job["current_module"] if job["current_article_id"] == aid else ""
            module = current if current in states and states[current]["status"] not in DONE else next((key for key, state in states.items() if state["status"] not in DONE), None)
            if module:
                states[module].update(status="failed", message=error)
            elif not aid:
                # Reuse was provisional at enqueue time; failed input checks must
                # not leave those modules looking successfully reused.
                for state in states.values():
                    if state["status"] == "existing":
                        state.update(status="failed", message=error)
            if aid:
                next(row for row in job["articles"] if row["article_id"] == aid)["error"] = error
            job["error"] = error
        self._study_update_job(job_id, update)

    def _study_run(self, job_id):
        generation = None
        stopped = False
        try:
            import study_generation as generation
            self._study_run_root(job_id)
            self._study_update_job(job_id, lambda job: job.update(status="running", message="开始整理所选材料"))
            def should_stop():
                with self.lock:
                    return self.study_jobs[job_id]["stop_requested"]
            job = self.study_jobs[job_id]
            for aid in job["article_ids"]:
                if should_stop():
                    raise generation.StudyStopped()
                with self.lock:
                    if all(state["status"] in DONE for state in self._study_states(self.study_jobs[job_id], aid).values()):
                        continue
                try:
                    materials = self.study_materials(aid, job["_materials"][aid])
                    run_dir = profile_run_path(self.root, "learning", "study-runs", job_id, aid)
                    run_dir.mkdir(parents=True, exist_ok=True)
                    generation.generate_article(self, aid, materials, run_dir,
                                                lambda module, status, message: self._study_report(job_id, aid, module, status, message),
                                                lambda module, payload: self._study_publish_article(job_id, aid, module, payload, run_dir),
                                                should_stop, regenerate=job["_regenerate"])
                    if any(state["status"] not in DONE for state in self._study_states(self.study_jobs[job_id], aid).values()):
                        raise RuntimeError("尚有学习模块未完成发布，成功模块已保留")
                except generation.StudyStopped:
                    raise
                except Exception as exc:
                    self._study_fail_module(job_id, aid, str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "材料读取、生成或保存未完成，成功模块已保留")
            if job["cross_article_ids"]:
                if should_stop():
                    raise generation.StudyStopped()
                try:
                    if any(not module_complete(self.content[aid], key) for aid in job["cross_article_ids"] for key in ARTICLE_MODULES[1:]):
                        raise ValueError("跨文所选文章尚有导读、概念或对照资料未完成；补齐后可继续")
                    self._study_prepare_cross(job_id)
                    modules = [module for module, state in self.study_jobs[job_id]["cross"].items() if state["status"] not in DONE]
                    if modules:
                        run_dir = profile_run_path(self.root, "learning", "study-runs", job_id, "cross")
                        run_dir.mkdir(parents=True, exist_ok=True)
                        generation.generate_cross(self, job["cross_article_ids"], run_dir,
                                                  lambda module, status, message: self._study_report(job_id, "", module, status, message),
                                                  lambda module, payload: self._study_publish_cross(job_id, module, payload), should_stop,
                                                  modules=modules, expected_signature=self.study_jobs[job_id]["_cross_signature"])
                    if any(state["status"] not in DONE for state in self.study_jobs[job_id]["cross"].values()):
                        raise RuntimeError("尚有跨文模块未完成发布，成功内容已保留")
                except generation.StudyStopped:
                    raise
                except Exception as exc:
                    self._study_fail_module(job_id, "", str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "跨文内容未能完成保存，成功模块已保留")
        except Exception as exc:
            stopped = generation is not None and isinstance(exc, generation.StudyStopped)
            if not stopped:
                try:
                    self._study_update_job(job_id, lambda job: job.update(error=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "整理任务未能继续，成功内容已保留"))
                except OSError:
                    pass
        finally:
            try:
                def finish(job):
                    states = [state for row in job["articles"] for state in row["modules"].values()] + ([*job["cross"].values()] if job["cross_article_ids"] else [])
                    complete = all(state["status"] in DONE for state in states) and not job.get("error")
                    status = "completed" if complete else "stopped" if stopped or job["stop_requested"] else "partial" if any(state["status"] in DONE for state in states) else "failed"
                    for state in states:
                        if state["status"] == "running":
                            state.update(status="pending" if status == "stopped" else "failed", message="等待继续" if status == "stopped" else "模块未完成，成功内容已保留")
                    job.update(status=status, current_article_id="", current_module="", message={"completed": "所选学习内容已整理并加载，AI内容仍需科学核查",
                               "stopped": "已停止后续处理，成功模块已保留", "partial": "部分内容已完成，可继续未完成模块", "failed": "整理未完成，可检查问题后继续"}[status])
                    if complete:
                        job["error"] = ""
                self._study_update_job(job_id, finish)
            except OSError:
                pass
            finally:
                with self.lock:
                    if self.study_active_job == job_id:
                        self.study_active_job = None

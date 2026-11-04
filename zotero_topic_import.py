"""Preview Zotero sources and add selected papers to local learning topics."""
from __future__ import annotations

from copy import deepcopy
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlencode

from library_features import clean_doi
from topic_features import source_identity
from zotero_integration import ZoteroError, attachment_title, item_key


BIBLIOGRAPHIC_FILTER = urlencode([("itemType", "-note"), ("itemType", "-attachment"), ("itemType", "-annotation")])


class ZoteroTopicImports:
    def init_zotero_topics(self):
        self.zotero_topic_previews = {}
        self.zotero_topic_jobs = {}
        self.zotero_topic_active_job = None

    def _zotero_source_collections(self, api):
        return {row["data"]["key"]: row["data"] for row in api.get_all("collections")}

    def _zotero_tag_names(self, api, collection=""):
        route = f"collections/{collection}/items/top/tags" if collection else "items/top/tags"
        return sorted({row["tag"] for row in api.get_all(route + "?" + BIBLIOGRAPHIC_FILTER)}, key=str.casefold)

    def zotero_tags(self, collection=""):
        api = self.zotero_client(bound=True)
        if collection:
            collection = item_key(collection)
            if collection not in self._zotero_source_collections(api):
                raise ZoteroError("Zotero分类已不存在，请重新选择来源；已有文章保留")
        return {"tags": self._zotero_tag_names(api, collection)}

    def _zotero_resolve_source(self, api, requested):
        if not isinstance(requested, dict) or requested.get("kind") not in {"collection", "tag"}:
            raise ZoteroError("请选择Zotero分类或标签作为来源")
        if requested.get("server_id") and requested["server_id"] != api.server_id:
            raise ZoteroError("主题来源属于另一个Zotero文库，请恢复原文库后重试")
        include_children = requested.get("include_children", False)
        if not isinstance(include_children, bool):
            raise ZoteroError("是否包含子分类的选项不正确")
        collection = requested.get("collection_key") or ""
        if requested["kind"] == "collection" and not collection:
            raise ZoteroError("请选择Zotero分类")
        collections, keys, label = {}, [], ""
        if collection:
            collection = item_key(collection)
            collections = self._zotero_source_collections(api)
            if collection not in collections:
                raise ZoteroError("Zotero分类已不存在，请重新选择来源；不会将其视为全部文章移出")
            keys = [collection]
            if include_children:
                for parent in keys:
                    keys.extend(key for key, row in collections.items() if row.get("parentCollection") == parent and key not in keys)
            row, names, seen = collections[collection], [], set()
            while row["key"] not in seen:
                seen.add(row["key"])
                names.insert(0, row["name"])
                parent = row.get("parentCollection")
                if parent not in collections:
                    break
                row = collections[parent]
            label = "/".join(names)
        tag = requested.get("tag") or ""
        if requested["kind"] == "tag":
            if not isinstance(tag, str) or not tag.strip():
                raise ZoteroError("请选择Zotero标签")
            # Exact metadata matching avoids interpreting a tag containing || or - as a search expression.
            if tag not in self._zotero_tag_names(api):
                raise ZoteroError("Zotero标签已不存在或没有书目使用，请重新选择；不会将其视为全部文章移出")
            label = (label + " · " if label else "") + "标签：" + tag
        source = {"kind": requested["kind"], "server_id": api.server_id, "label": label,
                  "include_children": include_children if collection else False}
        if collection:
            source["collection_key"] = collection
        if source["kind"] == "tag":
            source["tag"] = tag
        name = tag if source["kind"] == "tag" else collections[collection]["name"]
        return source, keys, name

    @staticmethod
    def _zotero_source_contains(data, source, collection_keys):
        if data.get("deleted") or data.get("parentItem") or data.get("itemType") in {"note", "attachment", "annotation"}:
            return False
        if collection_keys and not set(data.get("collections", [])).intersection(collection_keys):
            return False
        return source["kind"] != "tag" or source["tag"] in {tag["tag"] for tag in data.get("tags", [])}

    def _zotero_source_items(self, api, source, collection_keys):
        found = {}
        for collection in collection_keys or [""]:
            route = f"collections/{collection}/items/top" if collection else "items/top"
            for row in api.get_all(route + "?" + BIBLIOGRAPHIC_FILTER):
                data = row["data"]
                if self._zotero_source_contains(data, source, collection_keys):
                    found[data["key"]] = data
        return list(found.values())

    def _zotero_topic_match(self, api, data):
        linked = self.zotero_linked_article(api.server_id, data["key"])
        if linked:
            return linked
        doi = clean_doi(data.get("DOI", ""))
        if not doi:
            return None
        matches = [aid for aid, article in self.articles.items() if clean_doi(article.get("doi", "")) == doi]
        if len(matches) > 1:
            raise ZoteroError("同DOI匹配多篇已有文章，请先核对文章身份")
        if not matches:
            return None
        aid = matches[0]
        link = self.zotero_links["articles"].get(aid)
        if link and link["item_key"] != data["key"]:
            raise ZoteroError("同DOI文章已关联另一个Zotero条目，请先核对；不自动改绑")
        return aid

    def _zotero_topic_row(self, api, data):
        row = {"item_key": data["key"], "title": data.get("title", "") or "未命名文献",
               "doi": data.get("DOI", "") or "", "date": data.get("date", "") or "", "pdfs": []}
        try:
            aid = self._zotero_topic_match(api, data)
        except ValueError as exc:
            row.update(status="conflict", message=str(exc))
            return row, []
        attachments = [value["data"] for value in api.get_all(f"items/{item_key(data['key'])}/children?itemType=attachment")]
        pdf_keys = []
        for attachment in attachments:
            if (attachment.get("itemType") != "attachment" or attachment.get("parentItem") != data["key"]
                    or attachment.get("deleted") or attachment.get("contentType") != "application/pdf"):
                continue
            pdf_keys.append(attachment["key"])
            try:
                path = api.local_file(attachment["key"])
            except (ZoteroError, OSError):
                continue
            row["pdfs"].append({"key": attachment["key"], "title": attachment_title(attachment, path.name), "filename": path.name})
        if aid:
            row.update(article_id=aid, status="existing", message="复用已有文章和个人学习记录")
        elif len(row["pdfs"]) == 1:
            row.update(status="ready", selected_attachment_key=row["pdfs"][0]["key"])
        elif row["pdfs"]:
            row.update(status="choose_pdf", message="请为阅读入口选择正文PDF；补充材料仍可在Zotero附件中查看")
        else:
            row.update(status="missing_pdf", message="没有本机可读取的PDF，请先在Zotero下载或恢复附件")
        return row, pdf_keys

    def zotero_topic_preview(self, body):
        topic = self.topic(body["topic_id"]) if body.get("topic_id") else None
        requested = body.get("source") if "source" in body else (topic or {}).get("source")
        api = self.zotero_client(bound=True)
        source, collection_keys, name = self._zotero_resolve_source(api, requested)
        data = self._zotero_source_items(api, source, collection_keys)
        items, changed, warnings = [], [], []
        same_source = bool(topic and source_identity(topic.get("source")) == source_identity(source))
        for value in data:
            row, pdf_keys = self._zotero_topic_row(api, value)
            items.append(row)
            old_aid = topic.get("source_items", {}).get(row["item_key"]) if same_source else None
            if old_aid:
                article = self.article(old_aid)
                differences = []
                if row["title"].strip() != article["title"].strip() or row["doi"].strip().casefold() != (article.get("doi") or "").strip().casefold():
                    differences.append("Zotero当前标题或DOI与助手记录不同")
                link = self.zotero_links["articles"].get(old_aid, {})
                if link.get("attachment_key") and link["attachment_key"] not in pdf_keys:
                    differences.append("原关联PDF已不在此条目的附件列表中")
                elif link.get("attachment_key") and link["attachment_key"] not in {pdf["key"] for pdf in row["pdfs"]}:
                    differences.append("原关联PDF当前在本机不可读取")
                if differences:
                    changed.append({"item_key": row["item_key"], "article_id": old_aid, "title": row["title"],
                                    "message": "；".join(differences) + "；已有阅读版、导读和个人记录保留。"})
        current_keys = {row["item_key"] for row in items}
        removed = [{"item_key": key, "article_id": aid, "title": self.article(aid)["title"]}
                   for key, aid in topic.get("source_items", {}).items() if key not in current_keys] if same_source else []
        if topic and topic.get("source") and not same_source:
            warnings.append("接纳后将更新来源绑定，原有主题文章全部保留。")
        if not items:
            warnings.append("该来源当前没有符合条件的书目；已有主题文章保留。")
        if removed:
            warnings.append("来源移出的文章仅在此提示，仍保留在主题及阅读助手中。")
        preview = {"preview_id": uuid.uuid4().hex, "topic_id": topic["id"] if topic else "", "source": source,
                   "name_suggestion": name, "items": items, "removed": removed, "changed": changed, "warnings": warnings}
        with self.lock:
            self.zotero_topic_previews = {key: saved for key, saved in self.zotero_topic_previews.items()
                                          if saved.get("job_id") or time.monotonic() - saved["time"] < 1800}
            self.zotero_topic_previews[preview["preview_id"]] = {"time": time.monotonic(), "preview": deepcopy(preview),
                                                                "previous_source": deepcopy((topic or {}).get("source"))}
        return preview

    def zotero_topic_job(self, job_id):
        with self.lock:
            job = self.zotero_topic_jobs.get(job_id)
            if job is None:
                raise ZoteroError("导入任务状态已不存在或服务已重启；请重新查看来源，已成功文章会复用")
            return {"job": deepcopy(job)}

    def zotero_topic_import(self, body):
        with self.lock:
            saved = self.zotero_topic_previews.get(body.get("preview_id"))
            if saved and saved.get("job_id"):
                return self.zotero_topic_job(saved["job_id"])
            if not saved or time.monotonic() - saved["time"] >= 1800:
                raise ZoteroError("来源预览已过期，请重新查看来源")
            if self.zotero_topic_active_job:
                raise ZoteroError("另一组文章正在导入，请等待完成后再加入本组")
            preview = saved["preview"]
            selections = body.get("selections")
            if not isinstance(selections, list):
                raise ZoteroError("请选择要加入主题的文章")
            rows = {row["item_key"]: row for row in preview["items"]}
            chosen, seen = [], set()
            for selection in selections:
                key = item_key(selection.get("item_key")) if isinstance(selection, dict) else item_key(None)
                if key in seen or key not in rows:
                    raise ZoteroError("文章选择与预览不一致，请重新选择")
                seen.add(key)
                row = rows[key]
                if row["status"] in {"missing_pdf", "conflict"}:
                    raise ZoteroError("所选文章有缺失PDF或身份冲突，请先处理或取消勾选")
                attachment = selection.get("attachment_key") or row.get("selected_attachment_key")
                if row["status"] != "existing" and attachment not in {pdf["key"] for pdf in row["pdfs"]}:
                    raise ZoteroError("请为所选文章选择预览中可用的正文PDF")
                chosen.append({"row": deepcopy(row), "attachment_key": attachment})
            name = body.get("name", "")
            if not preview["topic_id"] and (not isinstance(name, str) or not name.strip() or len(name.strip()) > 120):
                raise ZoteroError("请填写新主题名称（最多120字符）")
            job = {"id": uuid.uuid4().hex, "status": "running", "topic_id": preview["topic_id"],
                   "total": len(chosen), "processed": 0, "added_count": 0, "reused_count": 0, "failed": [], "results": []}
            self.zotero_topic_jobs[job["id"]] = job
            saved["job_id"] = job["id"]
            self.zotero_topic_active_job = job["id"]
            try:
                threading.Thread(target=self._run_zotero_topic_import,
                                 args=(job["id"], deepcopy(saved), chosen, name), daemon=True).start()
            except RuntimeError:
                job.update(status="failed", error="无法启动导入任务；请重新查看来源后重试")
                self.zotero_topic_active_job = None
            return {"job": deepcopy(job)}

    def _run_zotero_topic_import(self, job_id, saved, chosen, name):
        job = self.zotero_topic_jobs[job_id]
        preview = saved["preview"]
        try:
            api = self.zotero_client(bound=True)
            source, collection_keys, _ = self._zotero_resolve_source(api, preview["source"])
            with self.lock:
                if job["topic_id"]:
                    topic = self.topic(job["topic_id"])
                    if source_identity(topic.get("source")) != source_identity(saved["previous_source"]):
                        raise ZoteroError("主题来源在预览后已改变，请重新查看；原有文章保留")
                else:
                    topic = self.save_topic({"name": name})
                    job["topic_id"] = topic["id"]
                job["topic"] = self.add_topic_articles(topic["id"], [], source_items={}, source=source)
            for selection in chosen:
                row, key = selection["row"], selection["row"]["item_key"]
                api = self.zotero_client(bound=True)
                if api.server_id != source["server_id"]:
                    raise ZoteroError("Zotero文库已改变，本组停止；已成功加入的文章保留")
                with self.lock:
                    if source_identity(self.topic(job["topic_id"]).get("source")) != source_identity(source):
                        raise ZoteroError("主题来源已被修改，本组停止；已成功加入的文章保留")
                aid = None
                try:
                    data = api.get_item(key)
                    if not self._zotero_source_contains(data, source, collection_keys):
                        raise ZoteroError("文章已不在预览的来源中，请重新查看来源后选择")
                    aid = self._zotero_topic_match(api, data)
                    if row.get("article_id") and aid != row["article_id"]:
                        raise ZoteroError("文章身份在预览后已变化，请重新核对")
                    action = "reused"
                    if not aid:
                        if (data.get("DOI", "") or "") != row["doi"] or (data.get("title", "") or "未命名文献") != row["title"]:
                            raise ZoteroError("书目信息在预览后已变化，请重新查看来源后导入")
                        single = self.zotero_import_preview({"item_key": key, "attachment_key": selection["attachment_key"]})
                        if single["identity_warnings"]:
                            raise ZoteroError("PDF与书目DOI不一致，请通过单篇预览核对后再加入主题")
                        result = self.zotero_import({"preview_id": single["preview_id"]})
                        aid = result["article"]["id"]
                        action = "reused" if result["linked_existing"] else "imported"
                    # A prior import may have published its article before SQLite initialization failed.
                    self.initialize_article_db(aid, recover_running=False)
                    with self.lock:
                        if source_identity(self.topic(job["topic_id"]).get("source")) != source_identity(source):
                            raise ZoteroError("主题来源已被修改，文章保留在助手中，请重新查看来源")
                        topic = self.add_topic_articles(job["topic_id"], [aid], source_items={key: aid}, source=source)
                        job["topic"] = topic
                        job["results"].append({"article_id": aid, "item_key": key, "title": row["title"], "action": action})
                        job["added_count" if action == "imported" else "reused_count"] += 1
                except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
                    error = str(exc) if isinstance(exc, ValueError) else "本机读取或保存未完成，请检查后重新查看来源；已成功内容保留"
                    if aid:
                        error += "；文章仍在助手中，可重新加入主题"
                    with self.lock:
                        job["failed"].append({"item_key": key, "title": row["title"], "error": error})
                finally:
                    with self.lock:
                        job["processed"] += 1
            with self.lock:
                job["status"] = "completed"
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            with self.lock:
                job.update(status="failed", error=str(exc) if isinstance(exc, ValueError) else "导入任务未能继续；已成功文章保留，请重新查看来源后重试")
        finally:
            with self.lock:
                if self.zotero_topic_active_job == job_id:
                    self.zotero_topic_active_job = None

"""Read local Zotero sources and explicitly save existing guides as child notes."""
from __future__ import annotations

import hashlib
import html
from http.client import IncompleteRead
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
import time
import uuid
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import fitz

from library_features import clean_doi


class ZoteroError(ValueError):
    """A user-readable local API error; never includes authorization material."""


def item_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{8}", value):
        raise ZoteroError("Zotero条目标识无效")
    return value


def file_digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "md5").hexdigest()


def attachment_title(data, filename=""):
    title = (data.get("title") or "").strip()
    if title and title.casefold() not in {"pdf", "full text", "full text pdf", "full-text pdf"}:
        return title
    name = Path(filename or data.get("filename") or "").stem
    name = re.sub(r"_[a-f0-9]{24}_main$", "", name, flags=re.I)
    return name.replace("_", " ").strip() or "PDF附件"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Every request belongs to the loopback API. File locations are read separately.
        return None


class LocalZotero:
    base = "http://127.0.0.1:23119/api/"
    _authorization = None  # (server_id, key), process memory only
    _authorization_lock = threading.Lock()

    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.server_id = None
        _, headers = self.request("GET", "")
        self.server_id = headers.get("Zotero-Server-ID")
        if not self.server_id:
            raise ZoteroError("需要Zotero 10或更新版本的本地接口")
        with self._authorization_lock:
            cached = type(self)._authorization
            if cached and cached[0] != self.server_id:
                type(self)._authorization = None

    def request(self, method, route, body=None, key=None, timeout=15):
        headers = {"Zotero-API-Version": "3"}
        if self.server_id:
            headers["Zotero-Server-ID"] = self.server_id
        if key:
            headers["Zotero-API-Key"] = key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            with self.opener.open(Request(self.base + route, data=data, headers=headers, method=method), timeout=timeout) as response:
                raw = response.read()
                value = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
                return value, dict(response.headers)
        except HTTPError as exc:
            if exc.code == 401:
                with self._authorization_lock:
                    if type(self)._authorization == (self.server_id, key):
                        type(self)._authorization = None
            messages = {403: "Zotero未允许本次操作；请检查本地接口设置或授权弹窗", 404: "Zotero条目或附件已不存在",
                        412: "当前Zotero文库与已关联文库不同，请恢复原文库后重试", 401: "Zotero写入授权失效，请重新点击保存并在Zotero中允许",
                        429: "Zotero授权请求过于频繁，请稍后重试"}
            raise ZoteroError(messages.get(exc.code, f"Zotero本地接口返回错误（{exc.code}），已有资料保留")) from None
        except (URLError, TimeoutError, ConnectionError, IncompleteRead):
            raise ZoteroError("无法连接Zotero或等待已超时；请打开Zotero并启用允许本机其他应用通信") from None

    def get(self, route):
        return self.request("GET", "users/0/" + route)[0]

    def get_all(self, route):
        """Read every page, rejecting an incomplete or changing source listing."""
        parts = urlsplit(route)
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in {"start", "limit"}]
        rows, start, version, total = [], 0, None, None
        while True:
            page, headers = self.request("GET", "users/0/" + parts.path + "?" + urlencode([*query, ("limit", 100), ("start", start)]))
            if not isinstance(page, list):
                raise ZoteroError("Zotero列表格式不正确，请重新查看来源")
            headers = {key.lower(): value for key, value in headers.items()}
            current_version = headers.get("last-modified-version")
            current_total = headers.get("total-results")
            try:
                current_total = int(current_total) if current_total is not None else None
            except (TypeError, ValueError):
                raise ZoteroError("Zotero列表数量无法确认，请重新查看来源") from None
            if (version is not None and current_version != version) or (total is not None and current_total != total):
                raise ZoteroError("读取期间Zotero来源已变化，请重新查看来源")
            version, total = current_version, current_total
            if not page:
                if total is not None and start < total:
                    raise ZoteroError("Zotero列表未完整返回，请重新查看来源")
                return rows
            rows.extend(page)
            start += len(page)
            if total is not None and start >= total:
                if start != total:
                    raise ZoteroError("Zotero列表数量不一致，请重新查看来源")
                return rows
            if total is None and len(page) < 100:
                return rows

    def get_item(self, key):
        return self.get("items/" + item_key(key))["data"]

    def local_file(self, attachment_key):
        value = self.get("items/" + item_key(attachment_key) + "/file/view/url")
        parsed = urlparse(value.decode("utf-8").strip())
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            raise ZoteroError("该附件没有可读取的本机文件")
        raw = unquote(parsed.path)
        path = Path(raw[1:] if len(raw) > 2 and raw[0] == "/" and raw[2] == ":" else raw)
        if not path.is_absolute() or not path.is_file():
            raise ZoteroError("附件尚未下载到本机或原文件已移动")
        return path

    def create_note(self, note, authorized):
        # Only an explicit "Always Allow" response permits reuse in this process.
        # Neither the key nor the authorization response is written or returned.
        with self._authorization_lock:
            cached = type(self)._authorization
            secret = cached[1] if cached and cached[0] == self.server_id else None
        response = None
        if secret is None:
            response, _ = self.request("POST", "local/authorize", {"appName": "文献阅读助手"}, timeout=600)
            secret = response.get("key") if isinstance(response, dict) else None
            if not secret:
                raise ZoteroError("Zotero未授予写入权限")
            if response.get("remember") is True:
                with self._authorization_lock:
                    type(self)._authorization = (self.server_id, secret)
        try:
            authorized()
            result, _ = self.request("POST", "users/0/items", [note], key=secret, timeout=60)
            if not isinstance(result, dict) or result.get("failed") or len(result.get("successful", {})) + len(result.get("unchanged", {})) != 1:
                raise ZoteroError("Zotero未确认导读保存；请重新检查，已有笔记不会被覆盖")
        finally:
            secret = None
            response = None


class NoteText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")


def note_text(value):
    parser = NoteText()
    parser.feed(value or "")
    return "".join(parser.parts).strip()


def catalog_abstract(value):
    class AbstractText(NoteText):
        # Plain text must not turn a superscript such as 10^3 into the number 103.
        def handle_starttag(self, tag, attrs):
            if tag in {"sup", "sub"}:
                self.parts.append("^(" if tag == "sup" else "_(")
            else:
                super().handle_starttag(tag, attrs)

        def handle_endtag(self, tag):
            if tag in {"sup", "sub"}:
                self.parts.append(")")
            else:
                super().handle_endtag(tag)

    parser = AbstractText()
    parser.feed(value or "")
    text = "".join(parser.parts)
    text = re.sub(r"\u00ad[ \t]*\r?\n[ \t]*", "", text).replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-[ \t]*\r?\n[ \t]*(?=\w)", "-", text)
    return re.sub(r"\s+", " ", text).strip()


class ZoteroFeatures:
    zotero_client_class = LocalZotero

    def init_zotero(self):
        self.zotero_link_path = self.data_root / "zotero-links.json"
        self.zotero_links = json.loads(self.zotero_link_path.read_text(encoding="utf-8")) if self.zotero_link_path.exists() else {"server_id": None, "articles": {}}
        self.zotero_previews = {}
        self.zotero_export_jobs = {}
        self.zotero_export_lock = threading.Lock()

    def zotero_client(self, bound=False):
        api = self.zotero_client_class()
        if bound and self.zotero_links["server_id"] not in (None, api.server_id):
            raise ZoteroError("当前Zotero文库与已关联文库不同，请恢复原文库后重试")
        return api

    def save_zotero_links(self):
        pending = self.data_root / ("zotero-links.pending-" + uuid.uuid4().hex + ".json")
        pending.write_text(json.dumps(self.zotero_links, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(pending, self.zotero_link_path)

    def zotero_status(self):
        try:
            api = self.zotero_client(bound=True)
            return {"available": True, "message": "已连接本机Zotero", "server_id": api.server_id}
        except ZoteroError as exc:
            return {"available": False, "message": str(exc)}

    def zotero_collections(self):
        rows = [row["data"] for row in self.zotero_client().get_all("collections")]
        by_key = {row["key"]: row for row in rows}
        def path(row):
            names, seen = [row["name"]], {row["key"]}
            parent = row.get("parentCollection")
            while parent in by_key and parent not in seen:
                seen.add(parent)
                row = by_key[parent]
                names.insert(0, row["name"])
                parent = row.get("parentCollection")
            return "/".join(names)
        return {"collections": sorted([{"key": row["key"], "name": row["name"], "parent_key": row.get("parentCollection") or "", "path": path(row)} for row in rows], key=lambda row: row["path"].casefold())}

    def zotero_item_summary(self, data, server_id):
        linked = next((aid for aid, link in self.zotero_links["articles"].items() if link["item_key"] == data["key"]), None) if server_id == self.zotero_links["server_id"] else None
        return {"key": data["key"], "title": data.get("title", ""), "doi": data.get("DOI", ""), "date": data.get("date", ""),
                "item_type": data["itemType"], "collections": data.get("collections", []), "tags": [t["tag"] for t in data.get("tags", [])], "linked_article_id": linked}

    def zotero_linked_article(self, server_id, key):
        with self.lock:
            matches = {aid for aid, link in self.zotero_links["articles"].items() if link["item_key"] == key} if self.zotero_links["server_id"] == server_id else set()
            for topic in getattr(self, "topic_data", {}).get("topics", []):
                if (topic.get("source") or {}).get("server_id") == server_id and key in topic.get("source_items", {}):
                    matches.add(topic["source_items"][key])
        if len(matches) > 1:
            raise ZoteroError("同一Zotero条目关联了多篇文章，请先核对文章身份")
        if matches:
            aid = next(iter(matches))
            self.article(aid)
            return aid
        return None

    def zotero_items(self, collection=""):
        api = self.zotero_client()
        route = f"collections/{item_key(collection)}/items/top" if collection else "items/top"
        route += "?" + urlencode([("itemType", "-note"), ("itemType", "-attachment"), ("itemType", "-annotation")])
        rows = [row["data"] for row in api.get_all(route)]
        return {"items": [self.zotero_item_summary(row, api.server_id) for row in rows if row["itemType"] not in {"note", "attachment", "annotation"} and not row.get("deleted")], "collection": collection}

    def zotero_attachment(self, api, parent_key, attachment_key):
        data = api.get_item(attachment_key)
        if data["itemType"] != "attachment" or data.get("parentItem") != parent_key:
            raise ZoteroError("附件不属于当前文章")
        return data

    def zotero_item(self, key, api=None):
        api = api or self.zotero_client()
        key = item_key(key)
        data = api.get_item(key)
        if data["itemType"] in {"note", "attachment", "annotation"} or data.get("deleted"):
            raise ZoteroError("请选择文献条目")
        item = self.zotero_item_summary(data, api.server_id)
        attachments, notes, annotations, available_paths = [], [], [], {}
        for record in api.get(f"items/{key}/children"):
            child = record["data"]
            if child.get("deleted"):
                continue
            if child["itemType"] == "note":
                text = note_text(child.get("note"))
                notes.append({"key": child["key"], "title": text.splitlines()[0] if text else "空笔记", "text": text})
            elif child["itemType"] == "attachment":
                attachment_key = child["key"]
                is_pdf = child.get("contentType") == "application/pdf"
                try:
                    path = api.local_file(attachment_key)
                    available = True
                    filename = path.name
                    available_paths[attachment_key] = path
                except ZoteroError:
                    available, filename = False, child.get("filename", "")
                attachments.append({"key": attachment_key, "title": attachment_title(child, filename), "filename": filename,
                                    "content_type": child.get("contentType", ""), "is_pdf": is_pdf, "available": available,
                                    "url": "/api/zotero/file?" + urlencode({"item": key, "attachment": attachment_key}),
                                    "open_url": f"zotero://open-pdf/library/items/{attachment_key}" if is_pdf else f"zotero://select/library/items/{attachment_key}"})
                for row in api.get(f"items/{attachment_key}/children") if is_pdf else []:
                    annotation = row["data"]
                    if annotation["itemType"] != "annotation" or annotation.get("deleted"):
                        continue
                    try:
                        position = json.loads(annotation.get("annotationPosition", "{}"))
                        index = position.get("pageIndex")
                        page = index + 1 if type(index) is int and index >= 0 else None
                    except (ValueError, TypeError):
                        page = None
                    annotations.append({"key": annotation["key"], "attachment_key": attachment_key, "attachment_title": attachment_title(child, filename),
                                        "type": annotation.get("annotationType", ""), "text": annotation.get("annotationText", ""), "comment": annotation.get("annotationComment", ""),
                                        "color": annotation.get("annotationColor", ""), "page": page, "page_label": annotation.get("annotationPageLabel", ""),
                                        "open_url": f"zotero://open-pdf/library/items/{attachment_key}?" + urlencode({"page": page or 1, "annotation": annotation["key"]})})
        aid = item["linked_article_id"]
        link = self.zotero_links["articles"].get(aid, {})
        source_path = available_paths.get(link.get("attachment_key"))
        matches = bool(source_path and file_digest(source_path) == file_digest(self.root / self.article(aid)["pdf"])) if aid else False
        return {"linked": bool(aid), "article_id": aid, "item": item, "attachments": attachments, "notes": notes, "annotations": annotations,
                "reader_matches_attachment": matches}

    def zotero_article(self, aid):
        article = self.article(aid)
        link = self.zotero_links["articles"].get(aid)
        api = self.zotero_client(bound=True)
        if link:
            return self.zotero_item(link["item_key"], api)
        doi = clean_doi(article.get("doi", ""))
        candidates = []
        if doi:
            for row in api.get("items/top"):
                data = row["data"]
                try:
                    if data["itemType"] not in {"note", "attachment", "annotation"} and clean_doi(data.get("DOI", "")) == doi:
                        candidates.append(self.zotero_item_summary(data, api.server_id))
                except ValueError:
                    continue
        return {"linked": False, "article_id": aid, "candidates": candidates}

    def zotero_file(self, aid, attachment_key, parent_key="", metadata=False):
        api = self.zotero_client(bound=bool(aid))
        if aid:
            self.article(aid)
            link = self.zotero_links["articles"].get(aid)
            if not link:
                raise ZoteroError("此文章尚未关联Zotero")
            parent_key = link["item_key"]
        data = self.zotero_attachment(api, item_key(parent_key), attachment_key)
        path = api.local_file(attachment_key)
        result = (path, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        return (*result, data) if metadata else result

    def zotero_page(self, aid, attachment_key, page, expected_digest=None, image=False):
        path, mime, attachment = self.zotero_file(aid, attachment_key, metadata=True)
        if mime != "application/pdf":
            raise ZoteroError("只有PDF附件可按页阅读和提问")
        # Hash and extract the same bytes, even if a file is replaced while reading.
        data = path.read_bytes()
        digest = hashlib.md5(data).hexdigest()
        if expected_digest and expected_digest != digest:
            raise ZoteroError("这份Zotero附件已改变，请重新选择原文后提问；旧问题仍保留")
        with self.pdf_lock, fitz.open(stream=data, filetype="pdf") as pdf:
            if type(page) is not int or not 1 <= page <= len(pdf):
                raise ZoteroError("Zotero附件PDF页码无效")
            if image:
                return pdf[page-1].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False).tobytes("png")
            return {"text": self.extract_page_text(pdf[page-1]), "page": page, "total": len(pdf), "attachment_title": attachment_title(attachment, path.name),
                    "content_md5": digest, "source_pages": [{"pdf_page": n, "text": self.extract_page_text(pdf[n-1])[:11500]} for n in range(max(1, page-1), min(len(pdf), page+1)+1)]}

    def zotero_question_source(self, aid, source):
        if not isinstance(source, dict):
            raise ZoteroError("Zotero来源格式无效")
        key, page = item_key(source.get("attachment_key")), source.get("page")
        value = self.zotero_page(aid, key, page, expected_digest=source.get("content_md5"))
        return {"attachment_key": key, "page": page, "attachment_title": value["attachment_title"], "content_md5": value["content_md5"],
                "open_url": f"zotero://open-pdf/library/items/{key}?page={page}"}

    def zotero_import_preview(self, body):
        api = self.zotero_client(bound=True)
        parent_key, attachment_key = item_key(body.get("item_key")), item_key(body.get("attachment_key"))
        parent = api.get_item(parent_key)
        if parent["itemType"] in {"note", "attachment", "annotation"} or parent.get("deleted"):
            raise ZoteroError("请选择文献条目")
        attachment = self.zotero_attachment(api, parent_key, attachment_key)
        if attachment.get("contentType") != "application/pdf":
            raise ZoteroError("请为阅读入口选择一份PDF附件")
        path = api.local_file(attachment_key)
        preview = self.preview_import(path.read_bytes(), path.name)
        doi = clean_doi(parent.get("DOI", ""))
        existing = self.zotero_linked_article(api.server_id, parent_key)
        if not existing:
            duplicate = self.duplicate_articles(self.import_previews[preview["preview_id"]]["digest"], doi)
            if len(duplicate) > 1:
                raise ZoteroError("同一来源匹配到多篇已有文章，请先核对文章身份")
            existing = duplicate[0]["id"] if duplicate else None
        old_link = self.zotero_links["articles"].get(existing, {})
        if old_link and old_link["item_key"] != parent_key:
            raise ZoteroError("同DOI或PDF的文章已关联另一个Zotero条目，请先核对；原关联保留")
        title = parent.get("title", "").strip() or preview["title"]
        warnings = []
        if preview["pdf_identity"]["doi"] and doi and preview["pdf_identity"]["doi"] != doi:
            warnings.append({"field": "doi", "pdf_value": preview["pdf_identity"]["doi"], "catalog_value": doi,
                             "message": "PDF识别的DOI与Zotero书目不同，请核对所选附件归属。"})
        value = {**preview, "title": title, "doi": doi, "item_key": parent_key, "attachment_key": attachment_key, "existing_article_id": existing,
                 "metadata": {"source": "Zotero 书目", "journal": parent.get("publicationTitle", "") or "", "date": parent.get("date", "") or "",
                              "abstract": catalog_abstract(parent.get("abstractNote", ""))}, "identity_warnings": warnings}
        with self.lock:
            self.zotero_previews = {key: row for key, row in self.zotero_previews.items() if time.monotonic() - row["time"] < 1800}
            self.zotero_previews[preview["preview_id"]] = {"time": time.monotonic(), "preview": value, "server_id": api.server_id, "path": str(path), "content_md5": file_digest(path)}
        return value

    def zotero_import(self, body):
        with self.lock:
            preview_id = body.get("preview_id")
            saved = self.zotero_previews.get(preview_id)
            if not saved or time.monotonic() - saved["time"] >= 1800:
                raise ZoteroError("Zotero预览已过期，请重新选择附件")
            preview = saved["preview"]
            api = self.zotero_client(bound=True)
            if api.server_id != saved["server_id"]:
                raise ZoteroError("Zotero文库已改变，请重新预览")
            self.zotero_attachment(api, preview["item_key"], preview["attachment_key"])
            if file_digest(api.local_file(preview["attachment_key"])) != saved["content_md5"]:
                raise ZoteroError("Zotero附件在预览后已变化，请重新预览")
            aid = preview["existing_article_id"]
            if not aid:
                article = self.commit_import({**body, "title": body.get("title") or preview["title"], "doi": preview["doi"]})
                aid = article["id"]
            else:
                article = self.article(aid)
            old = self.zotero_links["articles"].get(aid, {})
            if old and old["item_key"] != preview["item_key"]:
                raise ZoteroError("文章已关联另一个Zotero条目；原关联保留")
            matches = file_digest(self.root / article["pdf"]) == saved["content_md5"]
            self.zotero_links["server_id"] = api.server_id
            self.zotero_links["articles"][aid] = {**old, "item_key": preview["item_key"], "attachment_key": preview["attachment_key"], "reader_matches_attachment": matches}
            self.save_zotero_links()
            return {"article": article, "linked_existing": bool(preview["existing_article_id"])}

    def zotero_guide_preview(self, aid):
        article, content = self.article(aid), self.content[aid]
        link = self.zotero_links["articles"].get(aid)
        source_key, source_digest, source_error = None, None, ""
        if link:
            try:
                source_path, _ = self.zotero_file(aid, link["attachment_key"])
                source_digest = file_digest(source_path)
                if source_digest == file_digest(self.root / article["pdf"]):
                    source_key = link["attachment_key"]
            except (ZoteroError, OSError) as exc:
                source_error = str(exc) if isinstance(exc, ZoteroError) else "阅读版或Zotero附件暂时无法读取"
        title = article["short_title"] + " · 阅读导读"
        parts = ["<h1>" + html.escape(title) + "</h1>", "<p>" + html.escape(article["title"]) + "</p>"]
        def paragraph(label, value):
            if value:
                parts.append("<p><strong>" + html.escape(label) + "</strong> " + html.escape(str(value)).replace("\n", "<br/>") + "</p>")
        def source_pages(values):
            locators = []
            for page in values:
                label = html.escape(str(page))
                if source_key and type(page) is int and 1 <= page <= article["pages"]:
                    label = f'<a href="zotero://open-pdf/library/items/{source_key}?page={page}">{label}</a>'
                locators.append(label)
            if locators:
                parts.append("<p><strong>阅读版PDF页码：</strong> " + "、".join(locators) + "</p>")
        paragraph("页码说明：", content.get("page_convention", "页码指阅读助手保存的阅读版PDF。"))
        if link and not source_key:
            paragraph("来源定位：", "导读引用的是阅读助手原阅读版，其页码不能直接套用到Zotero其他附件。")
        overview = content.get("overview", {})
        for field, label in (("question", "科学问题："), ("logic", "研究逻辑："), ("approach", "研究思路：")):
            paragraph(label, overview.get(field))
        for field, label in (("findings", "主要发现"), ("limits", "解释限制")):
            if overview.get(field):
                parts.append("<h2>" + label + "</h2><ul>" + "".join("<li>" + html.escape(str(value)) + "</li>" for value in overview[field]) + "</ul>")
        for section in content.get("sections", []):
            parts.append("<h2>" + html.escape(section["title"]) + "</h2>")
            source_pages(section.get("pages", []))
            for field, label in (("guiding_question", "科学问题："), ("rationale", "研究动机："), ("approach", "分析思路："), ("summary", "主要发现："), ("evidence", "原文依据："), ("caution", "解释限制："), ("transition", "下一步问题：")):
                paragraph(label, section.get(field))
            for question in section.get("questions", []):
                paragraph("带着问题读原文：", question)
        if content.get("comparison_notes"):
            parts.append("<h2>与相关方法放在一起理解</h2>")
            for comparison in content["comparison_notes"]:
                parts.append("<h3>" + html.escape(comparison.get("topic", "")) + "</h3>")
                paragraph("说明性质：", comparison.get("type"))
                paragraph("对照说明：", comparison.get("note"))
                source_pages(comparison.get("pages", []))
        if content.get("concepts"):
            parts.append("<h2>概念与论文语境</h2>")
            for concept in content["concepts"]:
                parts.append("<h3>" + html.escape(concept["term"]) + "</h3>")
                for field, label in (("plain", "释义："), ("technical", "专业解释："), ("paper_context", "论文语境："), ("boundary", "解释限制：")):
                    paragraph(label, concept.get(field))
                source_pages(concept.get("pages", []))
        coverage = content.get("source_coverage", {})
        coverage_sections = (
            ("正文", coverage.get("main_text", [])),
            ("图表", coverage.get("figures", [])),
            ("补充材料", coverage.get("supplementary", [])),
            ("已纳入资料", coverage.get("included_assets", [])),
            ("缺件与尚未核查部分", coverage.get("missing_assets", []) + coverage.get("missing", [])),
            ("阅读说明", coverage.get("reading_notes", []) + coverage.get("notes", [])),
        )
        if any(values for _, values in coverage_sections):
            parts.append("<h2>资料范围与阅读说明</h2>")
            for label, values in coverage_sections:
                if values:
                    parts.append("<h3>" + label + "</h3><ul>" + "".join("<li>" + html.escape(str(value)) + "</li>" for value in values) + "</ul>")
        review = content.get("study_review", {})
        if any(review.get(field) for field in ("label", "scope", "limitations")):
            parts.append("<h2>核查说明</h2>")
            for field, label in (("label", "核查状态："), ("scope", "核查范围："), ("limitations", "核查限制：")):
                paragraph(label, review.get(field))
        note_html = "\n".join(parts)
        digest = hashlib.sha256(note_html.encode("utf-8")).hexdigest()
        record = (link or {}).get("guide_exports", {}).get(digest, {})
        return {"title": title, "note_html": note_html, "already_exported": record.get("status") == "completed", "note_key": record.get("key"),
                "can_export": bool(link and content.get("sections") and not source_error), "content_digest": digest,
                "source_attachment_key": source_key, "source_attachment_md5": source_digest if source_key else None, "source_error": source_error}

    def zotero_start_guide_export(self, aid):
        preview = self.zotero_guide_preview(aid)
        if not preview["can_export"]:
            raise ZoteroError(preview.get("source_error") or "请先关联Zotero条目，并完成文章导读整理")
        if not self.zotero_export_lock.acquire(blocking=False):
            raise ZoteroError("已有导读正在等待Zotero授权或保存，请先完成该操作")
        job = {"id": uuid.uuid4().hex, "status": "waiting_authorization", "message": "请在Zotero的本地API授权弹窗中允许保存导读"}
        with self.lock:
            self.zotero_export_jobs[job["id"]] = job
        try:
            threading.Thread(target=self.zotero_export_guide, args=(aid, preview, job), daemon=True).start()
        except Exception:
            with self.lock:
                job.update(status="failed", message="导读保存未能启动，请稍后重试")
            self.zotero_export_lock.release()
            raise
        return {"job": dict(job)}

    def zotero_guide_job(self, job_id):
        with self.lock:
            if job_id not in self.zotero_export_jobs:
                raise ZoteroError("保存状态已过期，请重新检查导读；不会覆盖原笔记")
            return {"job": dict(self.zotero_export_jobs[job_id])}

    def zotero_export_guide(self, aid, preview, job):
        try:
            api = self.zotero_client(bound=True)
            link = self.zotero_links["articles"][aid]
            parent = api.get_item(link["item_key"])
            if parent["itemType"] in {"attachment", "note", "annotation"} or parent.get("deleted"):
                raise ZoteroError("关联的Zotero文献已不存在")
            digest = preview["content_digest"]
            with self.lock:
                exports = link.setdefault("guide_exports", {})
                if digest not in exports:
                    exports[digest] = {"key": "".join(secrets.choice("23456789ABCDEFGHIJKLMNPQRSTUVWXYZ") for _ in range(8)), "status": "pending"}
                    self.save_zotero_links()
                record = exports[digest]
            note = {"key": record["key"], "version": 0, "itemType": "note", "parentItem": link["item_key"], "note": preview["note_html"], "tags": [{"tag": "阅读助手导读", "type": 0}], "relations": {}}
            children = {row["key"]: row["data"] for row in api.get(f"items/{link['item_key']}/children")}
            existing = children.get(note["key"])
            if existing is not None:
                if existing.get("itemType") != "note" or existing.get("note") != note["note"]:
                    raise ZoteroError("已保存导读在Zotero中有修改，保留现有笔记；请另行核对")
            else:
                if record["status"] == "completed":
                    raise ZoteroError("以前导出的导读已被移走或删除；保留该状态，不自动重建")
                def authorized():
                    if preview.get("source_attachment_key"):
                        source_path, _ = self.zotero_file(aid, preview["source_attachment_key"])
                        if file_digest(source_path) != preview["source_attachment_md5"]:
                            raise ZoteroError("Zotero附件在等待授权时已变化，请重新预览导读后保存")
                    with self.lock:
                        job.update(status="writing", message="正在Zotero中添加独立导读笔记")
                api.create_note(note, authorized)
                actual = api.get_item(note["key"])
                if actual.get("parentItem") != note["parentItem"] or actual.get("note") != note["note"]:
                    raise ZoteroError("导读写入后的内容尚未核对一致，请保留现状并检查")
            with self.lock:
                record["status"] = "completed"
                self.save_zotero_links()
                job.update(status="completed", message="导读已保存为独立子笔记，原有笔记保持原样", note_key=note["key"], open_url=f"zotero://select/library/items/{note['key']}")
        except (OSError, ValueError, RuntimeError) as exc:
            with self.lock:
                job.update(status="failed", message=str(exc) if isinstance(exc, ZoteroError) else "保存或核对未完成；已有资料保留，重新点击前会检查是否已经写入")
        finally:
            self.zotero_export_lock.release()

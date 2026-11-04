import { PdfReader } from "./pdf-reader.js";
import { renderImportMetadata } from "./import-preview.js";

export function createZoteroManager({ api, getArticleId, refreshLibrary, openArticle, activate, takeSource, toast, loadAssets, prepareImportTopics, addToTopic, chooseArticleTopic, afterImport }) {
  const $ = (id) => document.getElementById(id);
  const list = (value) => Array.isArray(value) ? value : [];
  const text = (value) => value == null ? "" : String(value);
  const query = (values) => new URLSearchParams(values).toString();
  const state = {
    loaded: false, available: false, mode: "add", items: [], detail: null, selectedKey: "",
    refreshSerial: 0, listSerial: 0, detailSerial: 0, viewerSerial: 0, guideSerial: 0,
    importBusy: false, preview: null, guide: null, exportBusy: false, job: null, pollTimer: null, pdfReader: null,
    resizeObserver: null, resizeTimer: null,
  };
  const node = (tag, className, value) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value != null) result.textContent = text(value);
    return result;
  };
  const action = (label, className, handler) => {
    const result = node("button", className, label);
    result.type = "button";
    result.addEventListener("click", handler);
    return result;
  };
  const status = (id, message, error = false) => {
    $(id).textContent = text(message);
    $(id).hidden = !message;
    $(id).classList.toggle("is-error", error);
  };
  const detailArticle = (detail = state.detail) => detail?.article_id || detail?.item?.linked_article_id || "";
  const attachmentName = (attachment) => {
    const title = text(attachment?.title).trim();
    if (title && !/^(?:pdf|full text(?: pdf)?|全文)$/i.test(title)) return title;
    return text(attachment?.filename).replace(/_[a-f0-9]{24}_main(?=\.pdf$)/i, "").replaceAll("_", " ") || title || "PDF 附件";
  };
  const physicalPage = (value) => Number.isInteger(Number(value)) && Number(value) > 0 ? Number(value) : null;

  function fileUrl(attachment, detail = state.detail) {
    const article = detailArticle(detail);
    return `/api/zotero/file?${query({ ...(article ? { id: article } : { item: detail.item.key }), attachment: attachment.key })}`;
  }

  function nativeLink(url, label) {
    let parsed;
    try { parsed = new URL(url); } catch { return null; }
    if (parsed.protocol !== "zotero:") return null;
    const link = node("a", "text-button", label);
    link.href = parsed.href;
    return link;
  }

  function setMode(mode) {
    state.mode = mode;
    const adding = mode === "add";
    $("zotero-heading").textContent = adding ? "添加文章" : "当前文章资料";
    $("zotero-intro").textContent = adding ? "导入 PDF，或从 Zotero 选择文章" : "附件、原文来源与学习记录导出";
    $("zotero-selector-tools").hidden = !adding;
    $("zotero-catalog").hidden = !adding;
    $("direct-pdf-add").hidden = !adding;
    $("article-materials").hidden = adding;
    $("zotero-layout").classList.toggle("article-material-layout", !adding);
    document.querySelector(".workspace").classList.toggle("adding-mode", adding);
    document.querySelector(".reading-layout").classList.toggle("adding-mode", adding);
    activate();
  }

  function showSelector() {
    state.detailSerial += 1; state.viewerSerial += 1; state.detail = null; state.selectedKey = "";
    state.pdfReader?.resetDocument();
    setMode("add");
    $("zotero-details").replaceChildren(node("p", "empty-state", "选择要加入学习的文献，再核对具体 PDF。"));
    if (!state.loaded || !state.available) refresh(); else renderItems();
  }

  async function refresh() {
    const serial = ++state.refreshSerial;
    $("zotero-refresh").disabled = true;
    status("zotero-status", "正在连接本机 Zotero…");
    try {
      const connection = await api("/api/zotero/status");
      if (serial !== state.refreshSerial) return;
      state.available = Boolean(connection.available);
      state.loaded = true;
      status("zotero-status", connection.message || (state.available ? "已连接本机 Zotero" : "请打开 Zotero，并允许本机应用连接。"), !state.available);
      if (!state.available) return;
      const payload = await api("/api/zotero/collections");
      if (serial !== state.refreshSerial) return;
      const previous = $("zotero-collection").value;
      const all = node("option", "", "全部文献"); all.value = "";
      $("zotero-collection").replaceChildren(all);
      list(payload.collections).forEach((collection) => {
        const option = node("option", "", collection.path || collection.name);
        option.value = collection.key;
        $("zotero-collection").append(option);
      });
      if ([...$("zotero-collection").options].some((option) => option.value === previous)) $("zotero-collection").value = previous;
      await loadItems();
    } catch (error) { if (serial === state.refreshSerial) status("zotero-status", error.message, true); }
    finally { if (serial === state.refreshSerial) $("zotero-refresh").disabled = false; }
  }

  async function loadItems() {
    const serial = ++state.listSerial;
    $("zotero-item-count").textContent = "正在读取文献…";
    try {
      const payload = await api(`/api/zotero/items?${query({ collection: $("zotero-collection").value })}`);
      if (serial !== state.listSerial) return;
      state.items = list(payload.items);
      renderItems();
    } catch (error) { if (serial === state.listSerial) $("zotero-item-count").textContent = error.message; }
  }

  function renderItems() {
    const search = $("zotero-search").value.trim().toLocaleLowerCase();
    const matches = state.items.filter((item) => !search || `${item.title} ${item.doi || ""}`.toLocaleLowerCase().includes(search));
    $("zotero-item-count").textContent = search ? `${matches.length} / ${state.items.length} 篇` : `${state.items.length} 篇文献`;
    const root = $("zotero-items"); root.replaceChildren();
    if (!matches.length) { root.append(node("p", "field-help", search ? "没有匹配的文章。" : "这个分类中暂无文献。")); return; }
    matches.forEach((item) => {
      const entry = action("", "zotero-item", () => selectItem(item.key));
      entry.classList.toggle("selected", state.selectedKey === item.key);
      entry.setAttribute("aria-pressed", String(state.selectedKey === item.key));
      entry.append(node("strong", "", item.title || "未命名文献"));
      entry.append(node("span", "", [item.date, item.linked_article_id ? "已在阅读助手中" : "可预览加入"].filter(Boolean).join(" · ")));
      root.append(entry);
    });
  }

  async function selectItem(key) {
    const serial = ++state.detailSerial;
    state.viewerSerial += 1; state.selectedKey = key; state.detail = null;
    state.pdfReader?.resetDocument();
    renderItems();
    $("zotero-details").replaceChildren(node("p", "field-help", "正在读取这篇文献的资料…"));
    try {
      const payload = await api(`/api/zotero/item?${query({ key })}`);
      if (serial !== state.detailSerial) return;
      state.detail = payload; renderDetail();
    } catch (error) { if (serial === state.detailSerial) $("zotero-details").replaceChildren(node("p", "question-error", error.message)); }
  }

  async function showArticle(articleId = getArticleId()) {
    if (!articleId) { toast("请先打开一篇阅读助手中的文章。"); return null; }
    setMode("article");
    loadAssets(articleId);
    status("zotero-status", "Zotero 资料只读；新的笔记与批注请在 Zotero 中维护。");
    const serial = ++state.detailSerial;
    state.viewerSerial += 1; state.detail = null;
    state.pdfReader?.resetDocument();
    $("zotero-details").replaceChildren(node("p", "field-help", "正在查找当前文章的 Zotero 资料…"));
    try {
      const payload = await api(`/api/zotero/article?${query({ id: articleId })}`);
      if (serial !== state.detailSerial) return null;
      if (payload.linked) {
        state.detail = payload; state.selectedKey = payload.item.key;
        renderItems(); renderDetail();
        return payload;
      }
      const root = $("zotero-details"); root.replaceChildren(node("h3", "", "当前文章尚未关联 Zotero"));
      root.append(node("p", "field-help", "选择同一文章的 PDF，预览后确认关联。已有导读、阅读版与个人记录会继续保留。"));
      if (list(payload.candidates).length) {
        root.append(node("h4", "", "发现相同 DOI 的文献"));
        payload.candidates.forEach((item) => root.append(action(item.title || "查看匹配文章", "zotero-candidate", () => { showSelector(); selectItem(item.key); })));
      } else root.append(node("p", "field-help", "尚未存入 Zotero 的文章需先在 Zotero 中添加。"));
      root.append(action("从 Zotero 选择同一文章", "button subtle", showSelector));
      return null;
    } catch (error) { if (serial === state.detailSerial) $("zotero-details").replaceChildren(node("p", "question-error", error.message)); return null; }
  }

  function renderDetail() {
    const detail = state.detail;
    if (!detail?.item) return;
    state.resizeObserver?.disconnect();
    clearTimeout(state.resizeTimer);
    const root = $("zotero-details"); root.replaceChildren();
    root.append(node("h3", "zotero-detail-title", detail.item.title || "未命名文献"));
    root.append(node("p", "field-help", [detail.item.date, detail.item.doi ? `DOI ${detail.item.doi}` : ""].filter(Boolean).join(" · ")));
    const articleId = detailArticle();
    const tools = node("div", "zotero-actions");
    if (articleId) {
      if (state.mode === "add") {
        tools.append(action("打开已有学习文章", "button subtle", async () => { await openArticle(articleId); }));
        tools.append(action("加入学习主题", "button subtle", () => chooseArticleTopic(articleId)));
      } else tools.append(action("预览导读并保存到 Zotero", "text-button", () => previewGuide(articleId)));
      root.append(node("p", "field-help", detail.reader_matches_attachment ? "已关联阅读助手。附件来源会按实际文件标明。" : "已关联阅读助手。Zotero 附件与原阅读版分别计页，请按来源名称核对页码。"));
    } else root.append(node("p", "field-help", "先预览一个正文 PDF，再确认加入或关联阅读助手。导入不会自动调用 AI。"));
    if (state.mode === "article") {
      const link = nativeLink(`zotero://select/library/items/${detail.item.key}`, "在 Zotero 中管理笔记与批注 ↗");
      if (link) tools.append(link);
    }
    root.append(tools);
    const attachments = list(detail.attachments);
    root.append(node("h4", "zotero-subheading", `附件 · ${attachments.length}`));
    if (!attachments.length) root.append(node("p", "field-help", "这篇文献暂无附件，请先在 Zotero 中添加 PDF。"));
    attachments.forEach((attachment) => {
      const card = node("section", "zotero-attachment");
      card.append(node("strong", "", attachmentName(attachment)));
      const actions = node("div", "zotero-actions");
      if (attachment.available && attachment.is_pdf) {
        actions.append(action("查看 PDF", "text-button", () => openAttachment(attachment, 1)));
        if (!articleId) {
          const importer = action("预览后加入阅读助手", "button subtle", () => previewImport(detail.item.key, attachment.key));
          importer.dataset.zoteroImport = "true"; importer.disabled = state.importBusy;
          actions.append(importer);
        }
      } else if (attachment.available) {
        const link = node("a", "text-button", "打开附件 ↗");
        link.href = fileUrl(attachment); link.target = "_blank"; link.rel = "noopener";
        actions.append(link);
      } else card.append(node("p", "field-help", "本机文件暂不可用，请先在 Zotero 中下载或恢复附件。"));
      const native = nativeLink(attachment.open_url, "在 Zotero 中打开 ↗");
      if (native) actions.append(native);
      card.append(actions);
      const annotations = list(detail.annotations).filter((annotation) => annotation.attachment_key === attachment.key);
      const group = node("details", "zotero-annotations");
      group.append(node("summary", "", `此附件的高亮与批注 · ${annotations.length}`));
      if (!annotations.length) group.append(node("p", "field-help", "暂无 Zotero 批注。阅读助手中的原有本地标注仍在原文页下方。"));
      annotations.forEach((annotation) => {
        const entry = node("article", "zotero-annotation");
        const page = physicalPage(annotation.page);
        entry.append(node("p", "zotero-source-name", page ? `PDF 第 ${page} 页${annotation.page_label && String(annotation.page_label) !== String(page) ? ` · 标示页 ${annotation.page_label}` : ""}` : "PDF 位置尚未确认"));
        if (annotation.text) entry.append(node("blockquote", "", annotation.text));
        if (annotation.comment) entry.append(node("p", "zotero-note-text", annotation.comment));
        if (!annotation.text && !annotation.comment) entry.append(node("p", "field-help", "图像或区域标注；可返回附件查看。"));
        const actions = node("div", "zotero-actions");
        const locate = action(page ? "查看来源页" : "打开附件", "text-button", () => openAttachment(attachment, page || 1));
        locate.disabled = !attachment.available || !attachment.is_pdf;
        actions.append(locate);
        const bring = action("带入提问", "text-button", async () => {
          if (!articleId || !page) return;
          await takeSource({ articleId, source: { attachment_key: attachment.key, attachment_title: attachmentName(attachment), page, page_label: annotation.page_label }, quote: text(annotation.text), comment: text(annotation.comment) });
        });
        bring.disabled = !articleId || !page || !attachment.available || !attachment.is_pdf;
        bring.title = !articleId ? "先关联阅读助手中的文章" : !page ? "缺少可确认的 PDF 页码" : "只带入这条批注，不自动发送";
        actions.append(bring); entry.append(actions); group.append(entry);
      });
      if (state.mode === "article") card.append(group);
      root.append(card);
    });
    const viewer = node("section", "zotero-attachment-viewer"); viewer.id = "zotero-attachment-viewer"; viewer.hidden = true; root.append(viewer);
    if (state.mode !== "article") return;
    const notes = list(detail.notes);
    root.append(node("h4", "zotero-subheading", `文献笔记 · ${notes.length}`));
    if (!notes.length) root.append(node("p", "field-help", "暂无 Zotero 文献笔记。"));
    notes.forEach((note) => {
      const entry = node("details", "zotero-note");
      entry.append(node("summary", "", note.title || "文献笔记"), node("p", "zotero-note-text", note.text || "这条笔记暂无可显示的文字。"));
      root.append(entry);
    });
  }

  async function openAttachment(attachment, requestedPage) {
    const detail = state.detail;
    if (!detail || !attachment.available || !attachment.is_pdf) return;
    const serial = ++state.viewerSerial;
    state.resizeObserver?.disconnect();
    clearTimeout(state.resizeTimer);
    state.pdfReader?.cancel();
    const root = $("zotero-attachment-viewer");
    if (!root) return;
    root.hidden = false; root.replaceChildren(node("p", "field-help", "正在打开附件来源页…"));
    const articleId = detailArticle(detail);
    let page = physicalPage(requestedPage) || 1;
    try {
      const data = articleId && state.mode === "article" ? await api(`/api/zotero/page?${query({ id: articleId, attachment: attachment.key, page })}`) : null;
      if (serial !== state.viewerSerial || detail !== state.detail) return;
      page = data ? physicalPage(data.page) || page : page;
      root.replaceChildren(node("h4", "", attachmentName(attachment)));
      let total = data?.total || 0;
      const source = node("p", "zotero-source-name", `此附件 PDF 第 ${page} 页${total ? ` / ${total}` : ""} · 与主阅读版独立计页`);
      const controls = node("div", "zotero-actions");
      const previous = action("上一页", "text-button", () => openAttachment(attachment, page - 1)); previous.disabled = page <= 1;
      const input = node("input"); input.type = "number"; input.min = "1"; input.value = String(page); input.className = "zotero-page-input"; input.setAttribute("aria-label", "附件 PDF 页码"); input.disabled = true;
      input.addEventListener("change", () => { const requested = physicalPage(input.value); if (requested && requested <= total) openAttachment(attachment, requested); else input.value = String(page); });
      const next = action("下一页", "text-button", () => openAttachment(attachment, page + 1)); next.disabled = true;
      controls.append(previous, input, next);
      if (articleId && state.mode === "article") controls.append(action("以本页作为提问来源", "text-button", () => takeSource({ articleId, source: { attachment_key: attachment.key, attachment_title: attachmentName(attachment), page }, quote: "", comment: "" })));
      const message = node("p", "field-help", "正在渲染附件 PDF…");
      const host = node("div", "pdf-scroll zotero-pdf-preview");
      root.append(source, controls, message, host);
      const url = fileUrl(attachment, detail);
      const link = node("a", "text-button", "在新窗口查看此附件 ↗"); link.href = `${url}#page=${page}`; link.target = "_blank"; link.rel = "noopener"; root.append(link);
      if (data) {
        const excerpt = node("details", "zotero-note");
        excerpt.append(node("summary", "", "查看本页提取文字"), node("p", "zotero-note-text", data.text || "此页未提取到文字，可查看 PDF 原图。")); root.append(excerpt);
      }
      root.scrollIntoView({ behavior: "smooth", block: "nearest" });
      const onState = (result) => {
        if (serial !== state.viewerSerial || detail !== state.detail) return;
        if (result.status === "ready") {
          total = result.totalPages;
          source.textContent = `此附件 PDF 第 ${page} 页 / ${total} · 与主阅读版独立计页`;
          input.max = String(total); input.disabled = false; next.disabled = page >= total;
          message.textContent = result.hasText ? "可直接选取附件原文；页码属于此附件。" : "此页未提取到可选择文字，可查看 PDF 原图。";
        } else if (result.status === "error") {
          message.className = "question-error"; message.textContent = `附件 PDF 无法显示：${result.message}`;
        }
      };
      if (!state.pdfReader) state.pdfReader = new PdfReader(host, onState);
      else { state.pdfReader.host = host; state.pdfReader.onState = onState; }
      let renderedWidth = 0;
      const renderPage = () => {
        if (serial !== state.viewerSerial || detail !== state.detail || !host.isConnected || !host.clientWidth) return;
        renderedWidth = host.clientWidth;
        return state.pdfReader.render({ url, pageNumber: page, pageLabel: `${attachmentName(attachment)} · PDF` });
      };
      state.resizeObserver = new ResizeObserver(() => {
        if (!host.clientWidth || Math.abs(host.clientWidth - renderedWidth) < 2) return;
        clearTimeout(state.resizeTimer);
        state.resizeTimer = setTimeout(renderPage, 120);
      });
      state.resizeObserver.observe(host);
      await renderPage();
    } catch (error) { if (serial === state.viewerSerial) root.replaceChildren(node("p", "question-error", error.message)); }
  }

  function setImportBusy(value) {
    state.importBusy = value;
    $("zotero-import-confirm").disabled = value;
    $("zotero-import-close").disabled = value;
    $("zotero-import-topic").disabled = value;
    $("zotero-study-after").disabled = value;
    document.querySelectorAll("[data-zotero-import]").forEach((button) => { button.disabled = value; });
  }

  async function previewImport(itemKey, attachmentKey) {
    if (state.importBusy) return;
    setImportBusy(true);
    status("zotero-status", "正在核对所选 PDF 与已有文章…");
    try {
      const preview = await api("/api/zotero/import-preview", { item_key: itemKey, attachment_key: attachmentKey });
      state.preview = preview;
      $("zotero-import-title").value = preview.title || "";
      $("zotero-import-doi").value = preview.doi || "";
      prepareImportTopics($("zotero-import-topic"));
      $("zotero-study-after").checked = false;
      renderImportMetadata($("zotero-import-metadata"), preview, `/api/zotero/file?${query({ item: itemKey, attachment: attachmentKey })}`);
      $("zotero-import-info").textContent = `${preview.pages} 页 PDF · 标题与 DOI 来自 Zotero 书目`;
      $("zotero-import-match").hidden = !preview.existing_article_id;
      $("zotero-import-match").textContent = "发现已在阅读助手中的同一文章。确认后只建立 Zotero 关联，不替换原阅读版或个人记录。";
      $("zotero-import-confirm").textContent = preview.existing_article_id ? "确认关联现有文章" : "确认加入阅读助手";
      status("zotero-import-status", ""); status("zotero-status", "PDF 已完成本机预览，请核对后确认。");
      if (!$("zotero-import-dialog").open) $("zotero-import-dialog").showModal();
    } catch (error) { status("zotero-status", error.message, true); }
    finally { setImportBusy(false); }
  }

  async function commitImport(event) {
    event.preventDefault();
    if (!state.preview || state.importBusy) return;
    const itemKey = state.preview.item_key;
    const topicId = $("zotero-import-topic").value;
    const studyAfter = $("zotero-study-after").checked;
    setImportBusy(true);
    let imported = null;
    try {
      imported = await api("/api/zotero/import", { preview_id: state.preview.preview_id, title: $("zotero-import-title").value.trim(), doi: $("zotero-import-doi").value.trim() });
      state.preview = null;
      $("zotero-import-dialog").close();
      await addToTopic(imported.article.id, topicId);
      await loadItems();
      await openArticle(imported.article.id);
      await selectItem(itemKey);
      toast(imported.linked_existing ? "Zotero 已关联；现有阅读版与学习记录继续保留。" : "文章已加入阅读助手，可阅读原文并开始记录。");
      if (studyAfter) await afterImport({ articleIds: [imported.article.id], topicId });
    } catch (error) {
      if (imported) toast(`文章已加入或关联，但加入主题或打开未完成：${error.message}。可在“编辑主题”中加入已有文章，无需重复导入。`, true);
      else status("zotero-import-status", error.message, true);
    } finally { setImportBusy(false); }
  }

  async function previewGuide(articleId) {
    const serial = ++state.guideSerial;
    $("zotero-guide-save").disabled = true;
    status("zotero-guide-status", "正在准备已有导读的笔记预览…");
    $("zotero-guide-preview").removeAttribute("srcdoc");
    $("zotero-guide-title").textContent = "";
    if (!$("zotero-guide-dialog").open) $("zotero-guide-dialog").showModal();
    try {
      const guide = await api(`/api/zotero/guide-preview?${query({ id: articleId })}`);
      if (serial !== state.guideSerial) return;
      state.guide = { ...guide, articleId };
      $("zotero-guide-title").textContent = guide.title || "文章导读";
      $("zotero-guide-preview").srcdoc = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><style>body{font:14px/1.9 system-ui,sans-serif;padding:14px;color:#243e34;overflow-wrap:anywhere}h1{font-size:22px}h2{font-size:18px}h3{font-size:16px}table{border-collapse:collapse}td,th{border:1px solid #dbe4d4;padding:6px}a{color:#406b56}</style><body>${guide.note_html || "<p>这篇文章尚无可保存的导读。</p>"}</body></html>`;
      $("zotero-guide-save").disabled = !guide.can_export || guide.already_exported || state.exportBusy;
      status("zotero-guide-status", guide.already_exported ? "这份导读已保存到 Zotero，无需重复创建。" : guide.can_export ? "请核对内容，再点击保存。" : "这篇文章尚无可保存的导读，或尚未建立 Zotero 关联。");
    } catch (error) { if (serial === state.guideSerial) status("zotero-guide-status", error.message, true); }
  }

  function showJob(job) {
    state.job = job;
    const labels = { waiting_authorization: "等待你在 Zotero 原生弹窗中授权…", writing: "正在保存独立导读笔记…", completed: "导读已保存到 Zotero。", failed: "导读保存未完成。" };
    const message = job.message || labels[job.status] || "正在准备保存导读…";
    $("zotero-write-state").hidden = false;
    status("zotero-write-status", message, job.status === "failed");
    if (state.guide?.articleId === job.articleId) status("zotero-guide-status", message, job.status === "failed");
    if (["completed", "failed"].includes(job.status)) {
      state.exportBusy = false; clearTimeout(state.pollTimer);
      if (job.status === "completed" && state.guide?.articleId === job.articleId) state.guide.already_exported = true;
      $("zotero-guide-save").disabled = !state.guide?.can_export || state.guide.already_exported;
      $("zotero-write-refresh").hidden = true;
    } else {
      state.exportBusy = true;
      $("zotero-guide-save").disabled = true;
      clearTimeout(state.pollTimer);
      state.pollTimer = setTimeout(pollJob, 1800);
    }
  }

  async function pollJob() {
    if (!state.job?.id) return;
    const job = state.job;
    clearTimeout(state.pollTimer);
    try {
      const payload = await api(`/api/zotero/guide-export?${query({ job: job.id })}`);
      if (state.job?.id === job.id) showJob({ ...payload.job, articleId: job.articleId });
    } catch (error) {
      if (state.job?.id !== job.id) return;
      status("zotero-write-status", `暂时无法读取保存进度：${error.message}。请刷新状态；不会重新发送保存请求。`, true);
      $("zotero-write-refresh").hidden = false;
    }
  }

  async function exportGuide() {
    if (!state.guide?.can_export || state.guide.already_exported || state.exportBusy) return;
    const guide = state.guide;
    state.exportBusy = true; $("zotero-guide-save").disabled = true;
    status("zotero-guide-status", "正在申请保存；请留意 Zotero 原生授权弹窗…");
    try {
      const payload = await api("/api/zotero/guide-export", { article_id: guide.articleId });
      showJob({ ...payload.job, articleId: guide.articleId });
    } catch (error) {
      state.exportBusy = false;
      status("zotero-guide-status", `保存请求未确认：${error.message}。请重新预览并核对 Zotero 中的笔记后再决定是否保存。`, true);
    }
  }

  async function openSource(source, articleId = getArticleId()) {
    activate();
    const detail = await showArticle(articleId);
    if (!detail) return;
    const attachment = list(detail.attachments).find((item) => item.key === source.attachment_key);
    if (!attachment) { toast("这条记录的附件当前未在关联文章中找到。", true); return; }
    await openAttachment(attachment, physicalPage(source.page) || 1);
  }

  $("zotero-refresh").addEventListener("click", () => state.mode === "article" ? showArticle() : refresh());
  $("zotero-collection").addEventListener("change", loadItems);
  $("zotero-search").addEventListener("input", renderItems);
  $("zotero-import-close").addEventListener("click", () => $("zotero-import-dialog").close());
  $("zotero-import-dialog").addEventListener("cancel", (event) => { if (state.importBusy) event.preventDefault(); });
  $("zotero-import-form").addEventListener("submit", commitImport);
  $("zotero-guide-close").addEventListener("click", () => $("zotero-guide-dialog").close());
  $("zotero-guide-save").addEventListener("click", exportGuide);
  $("zotero-write-refresh").addEventListener("click", pollJob);

  return { show: showSelector, showArticle, openSource };
}

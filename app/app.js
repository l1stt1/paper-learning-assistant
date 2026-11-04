import { PdfReader } from "/pdf-reader.js";
import { createLibraryManager } from "/library.js";
import { createZoteroManager } from "/zotero.js";
import { createTopicsManager } from "/topics.js";
import { createStudyManager } from "/study.js";
import { createModelSettingsManager } from "/model-settings.js";

"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const model = {
    library: null, article: null, content: {}, state: {}, articleId: "", view: "guide",
    page: 1, activeQuestionId: "", parentId: "", conceptId: "", zoteroSource: null,
    pollTimer: null, toastTimer: null, switchSerial: 0, pageSerial: 0, submitting: false,
    lastSelection: "", selectedSection: "", pdfWidth: 0, resizeTimer: null,
    annotations: [], legacyRecords: null, legacyError: "",
  };
  const statusLabels = { unassessed: "尚未收藏", learning: "正在学习", mastered: "已掌握", ignored: "已忽略" };
  const questionLabels = { saved: "已保存", running: "正在解释", completed: "已回答", failed: "解释未完成" };
  const depthLabels = { plain: "通俗", standard: "标准", deep: "深入" };
  const array = (value) => Array.isArray(value) ? value : [];
  const string = (value) => value == null ? "" : String(value);
  const query = (values) => Object.entries(values).map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(string(value))}`).join("&");
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = string(text);
    return node;
  };
  const button = (label, className, action) => {
    const node = el("button", className, label);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  };
  const cleanState = (state) => ({ concepts: {}, progress: {}, note: "", questions: [], ...state });
  const currentConcepts = () => array(model.content.concepts);
  const currentQuestions = () => array(model.state.questions);
  const pageCount = () => Math.max(1, Number(model.article?.pages) || 1);
  const pageNumber = (number) => Math.min(pageCount(), Math.max(1, Number.parseInt(number, 10) || 1));
  const draftKey = (kind, id = model.articleId) => `paper-learning:${model.library?.library_id}:${kind}:${id}`;
  const currentDraftKey = (id = model.articleId) => draftKey("draft", id);
  const noteDraftKey = (id = model.articleId) => draftKey("note-draft", id);
  function readDraft(kind) {
    const key = draftKey(kind);
    let value = sessionStorage.getItem(key);
    // Read the original installation's drafts once without deleting its saved keys.
    if (value === null && model.library?.legacy_drafts === true) {
      value = sessionStorage.getItem(`herv-paper-learning:${kind}:${model.articleId}`);
      if (value !== null) sessionStorage.setItem(key, value);
    }
    return value;
  }
  const questionPanelKey = "herv-paper-learning:question-panel-open";
  let libraryManager, zoteroManager, topicsManager, studyManager, modelSettingsManager;
  const attachmentPage = (value) => Math.max(1, Number.parseInt(value, 10) || 1);
  const cleanZoteroSource = (value) => value && /^[A-Z0-9]{8}$/.test(value.attachment_key || "") ? {
    attachment_key: value.attachment_key, attachment_title: value.attachment_title || value.title || "Zotero 附件",
    page: attachmentPage(value.page), ...(value.page_label ? { page_label: value.page_label } : {}),
  } : null;
  const sourceLabel = (value) => {
    const source = cleanZoteroSource(value);
    return source ? `${source.attachment_title} · PDF 第 ${source.page} 页` : "";
  };
  const usingPage = () => $("question-scope").value === "page";
  const referenceLabel = (question) => question.zotero_source ? sourceLabel(question.zotero_source) : `阅读版第 ${question.page} 页`;
  const pdfReader = new PdfReader($("pdf-scroll"), (state) => {
    $("pdf-error").hidden = state.status !== "error";
    $("pdf-status").hidden = state.status === "error" || (state.status === "ready" && state.hasText);
    $("pdf-status").textContent = state.status === "loading" ? `正在载入第 ${state.pageNumber} 页…` : "这一页没有可选文字。仍可查看完整原文，并在右侧手动提问或附上本页原图。";
    $("pdf-error-detail").textContent = state.status === "error" ? state.message : "";
    $("pdf-scroll").setAttribute("aria-busy", string(state.status === "loading"));
  });

  function toast(message, isError = false) {
    clearTimeout(model.toastTimer);
    $("toast").textContent = string(message);
    $("toast").classList.toggle("error", isError);
    $("toast").hidden = false;
    model.toastTimer = setTimeout(() => { $("toast").hidden = true; }, isError ? 8500 : 4000);
  }

  async function api(path, body) {
    const response = await fetch(path, {
      method: body ? "POST" : "GET", cache: "no-store",
      headers: body ? { "Content-Type": "application/json", "X-Learning-Token": model.library.token } : {},
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    let result;
    try { result = await response.json(); } catch { throw new Error("本地服务没有返回可读取的内容，请重新打开学习助手。"); }
    if (!response.ok) throw new Error(result.error || "操作没有完成，请重试。");
    return result;
  }

  function draftSnapshot() {
    return {
      question: $("question-input").value, quote: $("quote-input").value,
      sourceScope: $("question-scope").value, page: $("question-page").value, readingPage: model.page, depth: $("question-depth").value,
      includeImage: $("include-image").checked, captureConcept: $("capture-concept").checked,
      term: $("concept-term").value, parentId: model.parentId, conceptId: model.conceptId,
      zoteroSource: model.zoteroSource ? { ...model.zoteroSource, page: attachmentPage($("question-page").value) } : null,
    };
  }

  function saveDraft() {
    if (!model.articleId) return;
    try { sessionStorage.setItem(currentDraftKey(), JSON.stringify(draftSnapshot())); } catch { /* The visible draft remains usable if browser storage is unavailable. */ }
  }

  function restoreDraft() {
    let draft = {};
    try { draft = JSON.parse(readDraft("draft") || "{}"); } catch { /* Ignore an unreadable browser draft. */ }
    $("question-input").value = string(draft.question);
    $("quote-input").value = string(draft.quote);
    model.zoteroSource = cleanZoteroSource(draft.zoteroSource);
    if (draft.readingPage) model.page = pageNumber(draft.readingPage);
    const legacyReference = !draft.sourceScope && Boolean($("quote-input").value.trim() || draft.includeImage || model.zoteroSource);
    $("question-scope").value = draft.sourceScope === "page" || legacyReference ? "page" : "article";
    $("question-page").value = model.zoteroSource ? attachmentPage(draft.page || model.zoteroSource.page) : pageNumber(draft.page || model.page);
    $("question-depth").value = ["plain", "standard", "deep"].includes(draft.depth) ? draft.depth : "standard";
    $("include-image").checked = Boolean(draft.includeImage);
    $("capture-concept").checked = Boolean(draft.captureConcept);
    $("concept-term").value = string(draft.term).slice(0, 150);
    model.parentId = string(draft.parentId);
    model.conceptId = string(draft.conceptId);
    updateComposerContext();
  }

  function updateComposerContext() {
    const concept = currentConcepts().find((item) => item.id === model.conceptId);
    const parent = currentQuestions().find((item) => item.id === model.parentId);
    $("concept-context").hidden = !model.conceptId;
    $("concept-context-label").textContent = concept ? `关联概念：${concept.term}` : "关联已有概念";
    $("followup-banner").hidden = !model.parentId;
    $("followup-summary").textContent = parent ? parent.question : "沿用此前问题的上下文";
    const canReuse = parent && parent.source_scope !== "article";
    $("followup-source-reuse").hidden = !canReuse || usingPage();
    if (canReuse) $("followup-source-reuse").textContent = `沿用上次引用 · ${referenceLabel(parent)}`;
    $("page-reference").hidden = !usingPage();
    $("page-reference").disabled = !usingPage();
    $("question-scope-help").textContent = usingPage()
      ? "优先解释所指定的页。改选整篇可解除这次引用。"
      : `按问题检索整篇阅读版，选取相关段落。阅读位置记在第 ${model.page} 页。`;
    if (model.zoteroSource) {
      model.zoteroSource.page = attachmentPage($("question-page").value);
      $("question-page").removeAttribute("max");
    } else $("question-page").max = pageCount();
    $("zotero-source-banner").hidden = !usingPage() || !model.zoteroSource;
    $("zotero-source-label").textContent = sourceLabel(model.zoteroSource);
    $("quote-page-label").textContent = model.zoteroSource ? sourceLabel(model.zoteroSource) : `阅读版第 ${pageNumber($("question-page").value)} 页`;
    $("term-field").hidden = !$("capture-concept").checked;
    $("concept-term").required = $("capture-concept").checked;
  }

  function syncReadingLocation() {
    const topic = topicsManager?.topic();
    const article = model.article;
    let label = article?.short_title || article?.title;
    if (model.view === "network") label = "跨文学习";
    if (model.view === "zotero") label = document.querySelector(".workspace").classList.contains("adding-mode") ? "添加文章" : label && `资料与导出 · ${label}`;
    document.title = [label, topic?.name, "文献学习助手"].filter(Boolean).join(" · ");

    const url = new URL(location.href);
    if (topic) url.searchParams.set("topic", topic.id); else url.searchParams.delete("topic");
    if (model.articleId) url.searchParams.set("article", model.articleId); else url.searchParams.delete("article");
    if (url.href !== location.href) history.replaceState(history.state, "", url);
  }

  function showView(view) {
    model.view = view;
    if (view !== "zotero") { document.querySelector(".workspace").classList.remove("adding-mode"); document.querySelector(".reading-layout").classList.remove("adding-mode"); }
    document.querySelector(".workspace").classList.toggle("network-mode", view === "network");
    document.querySelector(".reading-layout").classList.toggle("network-mode", view === "network");
    document.querySelectorAll(".view").forEach((node) => { node.hidden = node.id !== `view-${view}`; });
    document.querySelectorAll(".tab").forEach((node) => {
      const active = node.dataset.view === view;
      node.classList.toggle("active", active);
      if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
    });
    $("selection-action").hidden = true;
    if (view === "reader") loadPage(model.page);
    else { model.pageSerial += 1; model.lastSelection = ""; pdfReader.cancel(); }
    if (view === "concepts") refreshArticleData().catch((error) => toast(error.message, true));
    if (view === "history") { renderQuestions(); renderLegacyRecords(); }
    syncReadingLocation();
    if (view === "network") libraryManager?.show(); else libraryManager?.hide();
  }

  async function switchArticle(id) {
    if (!id) return false;
    if (model.submitting) { toast("请等待当前问题保存完成，再切换文章。"); return false; }
    await topicsManager?.ensureArticleVisible(id);
    saveDraft();
    clearTimeout(model.pollTimer);
    model.pageSerial += 1;
    pdfReader.cancel();
    const serial = ++model.switchSerial;
    $("article-select").disabled = true;
    $("load-error").hidden = true;
    try {
      const payload = await api(`/api/article?${query({ id })}`);
      if (serial !== model.switchSerial) return false;
      document.querySelector(".workspace").classList.remove("topic-empty");
      document.querySelector(".app-shell").classList.remove("topic-empty");
      pdfReader.resetDocument();
      model.articleId = id;
      model.article = payload.article || {};
      model.content = payload.content || {};
      model.state = cleanState(payload.state);
      model.page = 1;
      model.activeQuestionId = "";
      model.selectedSection = "";
      model.lastSelection = "";
      model.legacyRecords = null; model.legacyError = "";
      $("article-select").querySelector('option[value=""]:disabled')?.remove();
      $("article-select").value = id;
      $("article-title").textContent = model.article.title || model.article.short_title || "文章阅读";
      $("article-subtitle").textContent = [model.article.doi ? `DOI ${model.article.doi}` : "", `${pageCount()} 页原始文献`, "学习记录按文章独立保存"].filter(Boolean).join(" · ");
      $("article-tag").textContent = model.article.short_title || "本地阅读";
      $("pdf-link").href = `/api/pdf?${query({ id })}`;
      $("export-markdown").href = `/api/export?${query({ id, format: "markdown" })}`;
      $("export-json").href = `/api/export?${query({ id, format: "json" })}`;
      $("page-number").max = pageCount();
      $("question-page").max = pageCount();
      $("page-total").textContent = `/ ${pageCount()}`;
      $("concept-search").value = "";
      $("history-search").value = "";
      restoreDraft();
      renderAll();
      renderLegacyRecords();
      loadLegacyRecords(id);
      studyManager?.syncArticle(model.content);
      libraryManager?.syncLibrary();
      showView("guide");
      schedulePoll();
      return true;
    } catch (error) {
      if (serial !== model.switchSerial) return false;
      $("load-error").textContent = error.message;
      $("load-error").hidden = false;
      if (topicsManager.visibleArticles().some((article) => article.id === model.articleId)) {
        $("article-select").value = model.articleId;
        if (model.view === "reader") loadPage(model.page);
        schedulePoll();
      } else {
        showTopicOverview("请从文章列表重新选择要打开的文章。");
        const select = $("article-select");
        if (!select.querySelector('option[value=""]')) {
          const retry = el("option", "", "选择文章重试"); retry.value = ""; retry.disabled = true; select.prepend(retry);
        }
        select.value = "";
      }
      return false;
    } finally {
      if (serial === model.switchSerial) $("article-select").disabled = false;
    }
  }

  async function refreshArticleData() {
    const id = model.articleId;
    const payload = await api(`/api/article?${query({ id })}`);
    if (id !== model.articleId) return;
    model.content = payload.content || model.content;
    model.state = cleanState(payload.state);
    renderConcepts();
    renderQuestions();
    renderProgress();
    renderActiveAnswer();
  }

  function renderAll() {
    renderOverview(); renderSections(); renderProgress(); renderConcepts(); renderQuestions(); renderActiveAnswer();
  }

  function appendList(target, values) {
    if (!array(values).length) { target.append(el("p", "field-help", "尚未整理")); return; }
    const list = el("ul", "info-list");
    array(values).forEach((value) => { list.append(el("li", "", textValue(value))); });
    target.append(list);
  }

  function textValue(value) {
    if (value == null) return "";
    if (typeof value === "string" || typeof value === "number") return string(value);
    if (Array.isArray(value)) return value.map(textValue).filter(Boolean).join("；");
    return Object.entries(value).map(([key, item]) => `${key}：${textValue(item)}`).join("\n");
  }

  function renderOverview() {
    const overview = model.content.overview || {};
    const prepared = !(model.article.imported && !array(model.content.sections).length) && Boolean(overview.question || overview.logic || overview.approach || array(overview.findings).length || array(model.content.sections).length);
    $("guide-status").textContent = prepared ? model.content.study_review ? "AI 导读 · 请核对原文" : "文章导读" : "尚未整理";
    const root = $("overview"); root.replaceChildren();
    root.append(el("div", "overview-question", overview.question || "研究问题导读尚未整理"));
    const logic = textValue(overview.logic) || textValue(overview.approach);
    if (logic) {
      const route = el("div", "overview-route");
      route.append(el("h3", "", "作者的研究路线"), el("p", "overview-approach", logic)); root.append(route);
    } else {
      root.append(el("p", "overview-approach", "研究思路尚未整理。可以先阅读 PDF 原文，选取具体句段提问。"));
    }
    if (array(overview.findings).length) {
      const findings = el("section", "overview-findings");
      findings.append(el("h3", "", "主要发现")); appendList(findings, overview.findings); root.append(findings);
    }
    if (array(overview.limits).length) {
      const limits = el("details", "guide-supplement");
      limits.append(el("summary", "", "补充：理解与使用的边界")); appendList(limits, overview.limits); root.append(limits);
    }
    const comparison = $("comparison-notes"); comparison.replaceChildren();
    if (model.content.comparison_notes && textValue(model.content.comparison_notes)) {
      const card = el("section", "comparison-card"); card.append(el("h3", "", "与相关方法放在一起理解"));
      renderComparisons(card, model.content.comparison_notes);
      comparison.append(card);
    }
  }

  function renderComparisons(target, value) {
    const entries = Array.isArray(value) ? value : value && typeof value === "object" && !value.topic && !value.note ? Object.values(value) : [value];
    entries.forEach((item) => {
      if (item == null) return;
      if (typeof item !== "object") { target.append(el("p", "", item)); return; }
      const section = el("section", "comparison-item");
      if (item.topic) section.append(el("h4", "", item.topic));
      if (item.type) section.append(el("span", "pill comparison-type", item.type));
      if (item.note) section.append(el("p", "", textValue(item.note)));
      const pages = array(item.pages).map(Number).filter((page) => Number.isInteger(page) && page > 0 && page <= pageCount());
      if (pages.length) {
        const links = el("div", "comparison-pages");
        [...new Set(pages)].forEach((page) => links.append(button(`PDF 第 ${page} 页 ↗`, "text-button", () => openPage(page))));
        section.append(links);
      }
      if (section.childNodes.length) target.append(section);
    });
  }

  function renderSections() {
    const root = $("section-cards"); root.replaceChildren();
    if (!array(model.content.sections).length) {
      const empty = el("div", "empty-state");
      empty.append(el("p", "", "章节导读尚未整理。"), button("阅读 PDF 原文 ↗", "button soft", () => openPage(1)));
      root.append(empty);
    }
    array(model.content.sections).forEach((section, index) => {
      const card = el("section", "section-card"); card.id = `section-${section.id}`;
      const top = el("div", "section-card-top"); top.append(el("span", "section-index", index + 1), el("h3", "", section.title)); card.append(top);
      if (section.guiding_question) card.append(el("p", "section-question", textValue(section.guiding_question)));
      [["为什么做这一步", section.rationale], ["分析思路", section.approach]].forEach(([title, value]) => {
        if (!textValue(value)) return;
        const paragraph = el("p", "section-reasoning");
        paragraph.append(el("strong", "", `${title}：`), document.createTextNode(textValue(value))); card.append(paragraph);
      });
      const reading = el("div", "section-reading");
      if (array(section.questions).length) {
        reading.append(el("h4", "", "带着这些问题读原文"));
        const list = el("ul", "reading-questions");
        section.questions.forEach((item) => list.append(el("li", "", textValue(item)))); reading.append(list);
      }
      const pages = [...new Set(array(section.pages).map(Number).filter((page) => Number.isInteger(page) && page > 0 && page <= pageCount()))];
      const links = el("div", "section-pages");
      if (pages.length) pages.forEach((page) => links.append(button(`PDF 第 ${page} 页 ↗`, "text-button", () => openPage(page))));
      else links.append(button("阅读原文 ↗", "text-button", () => openPage(1)));
      reading.append(links); card.append(reading);
      if (textValue(section.summary)) {
        const result = el("div", "section-result");
        result.append(el("h4", "", "这一分析得到什么"), el("p", "", textValue(section.summary))); card.append(result);
      }
      if (section.evidence) { const block = el("div", "evidence-block"); block.append(el("strong", "", "原文依据"), el("p", "", textValue(section.evidence))); card.append(block); }
      if (section.transition) {
        const transition = el("p", "section-transition");
        transition.append(el("strong", "", "接下来："), document.createTextNode(textValue(section.transition))); card.append(transition);
      }
      if (section.caution) {
        const details = el("details", "guide-supplement");
        details.append(el("summary", "", "补充：解读时的边界"), el("p", "", textValue(section.caution))); card.append(details);
      }
      const footer = el("div", "card-footer");
      const label = el("label"); const check = el("input"); check.type = "checkbox"; check.checked = Boolean(model.state.progress[section.id]); check.dataset.sectionId = section.id;
      check.addEventListener("change", async () => {
        const previous = !check.checked; check.disabled = true;
        try { await updateState({ action: "progress", section_id: section.id, completed: check.checked }); toast(check.checked ? "已记下这一节的阅读进度。" : "已取消这一节的完成标记。"); }
        catch (error) { check.checked = previous; toast(error.message, true); }
        finally { check.disabled = false; }
      });
      label.append(check, document.createTextNode("这一节读完了")); footer.append(label); card.append(footer); root.append(card);
    });
  }

  function renderProgress() {
    const sections = array(model.content.sections);
    const completed = sections.filter((item) => model.state.progress[item.id]).length;
    $("progress-count").textContent = `${completed} / ${sections.length}`;
    $("reading-progress").max = Math.max(1, sections.length); $("reading-progress").value = completed;
    const nav = $("section-nav"); nav.replaceChildren();
    sections.forEach((section, index) => {
      const done = Boolean(model.state.progress[section.id]);
      const node = button("", `route-item${done ? " completed" : ""}${section.id === model.selectedSection ? " current" : ""}`, () => {
        model.selectedSection = section.id; showView("guide"); renderProgress();
        document.querySelectorAll(".section-card").forEach((card) => card.classList.toggle("highlight", card.id === `section-${section.id}`));
        $("section-" + section.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      node.append(el("span", "route-number", done ? "✓" : index + 1), el("span", "", section.title)); nav.append(node);
    });
    document.querySelectorAll("input[data-section-id]").forEach((node) => { node.checked = Boolean(model.state.progress[node.dataset.sectionId]); });
  }

  async function updateState(action) {
    const id = model.articleId;
    const payload = await api("/api/state", { article_id: id, ...action });
    if (id === model.articleId) { model.state = cleanState(payload.state); renderProgress(); renderConcepts(); }
    return payload;
  }

  function conceptStatus(id) { return model.state.concepts[id]?.status || "unassessed"; }
  function isSavedConcept(id) { return Boolean(model.state.concepts[id]?.saved_at) && ["learning", "mastered"].includes(conceptStatus(id)); }

  function renderConcepts() {
    const root = $("concept-list"); root.replaceChildren();
    const concepts = currentConcepts();
    $("concept-count").textContent = concepts.length;
    const saved = concepts.filter((item) => isSavedConcept(item.id)).length;
    $("saved-count").textContent = saved ? `已收藏 ${saved} 个` : "尚未收藏";
    const search = $("concept-search").value.trim().toLocaleLowerCase();
    const filter = $("concept-filter").value;
    const matches = concepts.filter((item) => {
      const status = conceptStatus(item.id);
      const searchable = [item.term, ...array(item.aliases), item.plain, item.technical, item.paper_context].join(" ").toLocaleLowerCase();
      return (!search || searchable.includes(search)) && (filter === "all" || (filter === "saved" ? isSavedConcept(item.id) : status === filter));
    }).sort((a, b) => {
      const ta = Date.parse(model.state.concepts[a.id]?.saved_at || "") || 0;
      const tb = Date.parse(model.state.concepts[b.id]?.saved_at || "") || 0;
      return tb - ta || concepts.indexOf(a) - concepts.indexOf(b);
    });
    if (!matches.length) { root.append(el("div", "empty-state", search ? "没有匹配的概念，试试名称、别名或更短的关键词。" : !concepts.length ? "概念资料尚未整理。可在 PDF 原文中选中术语，解释并收藏需要学习的概念。" : "这个分类里还没有概念。可以先阅读预制释义，再收藏需要学习的内容。")); return; }
    matches.forEach((concept) => {
      const status = conceptStatus(concept.id);
      const card = el("article", "concept-card"); card.id = `concept-${concept.id}`;
      const top = el("div", "concept-top"); top.append(el("h3", "", concept.term)); card.append(top);
      card.append(el("p", "concept-aliases", [concept.custom ? "个人新增概念" : model.content.study_review ? "AI 整理释义 · 请核对原文" : "预制释义", ...array(concept.aliases)].join(" · ")));
      card.append(el("p", "concept-plain", concept.plain || "这个术语已收藏。可在下方点击提问，生成有原文依据的解释。"));
      const details = el("details"); details.append(el("summary", "", "查看技术解释、本文语境与边界"));
      const body = el("div", "concept-detail");
      [["技术解释", concept.technical], ["本文中的含义", concept.paper_context], ["适用边界", concept.boundary]].forEach(([label, value]) => { if (value) body.append(el("h4", "", label), el("p", "", textValue(value))); });
      if (concept.zotero_source) { body.append(button(`查看附件来源 · ${sourceLabel(concept.zotero_source)} ↗`, "text-button", () => zoteroManager.openSource(concept.zotero_source))); }
      else if (array(concept.pages).length) { body.append(button(`回到原文 · 第 ${concept.pages.join("、")} 页 ↗`, "text-button", () => openPage(concept.pages[0]))); }
      const related = el("div", "concept-links");
      array(concept.related).forEach((reference) => {
        const id = typeof reference === "string" ? reference : reference.id || reference.concept_id;
        const target = concepts.find((item) => item.id === id);
        if (target) related.append(button(target.term, "", () => { $("concept-search").value = target.term; $("concept-filter").value = "all"; renderConcepts(); }));
      }); body.append(related); details.append(body); card.append(details);
      const actions = el("div", "concept-actions");
      actions.append(button("围绕这个概念提问 ↗", "text-button", () => askConcept(concept)));
      const select = el("select"); select.setAttribute("aria-label", `${concept.term} 的学习状态`);
      Object.entries(statusLabels).forEach(([value, label]) => { const option = el("option", "", label); option.value = value; select.append(option); }); select.value = status;
      select.addEventListener("change", () => setConceptStatus(concept.id, select.value)); actions.append(select); card.append(actions); root.append(card);
    });
  }

  async function setConceptStatus(id, status) {
    try { await updateState({ action: "concept", concept_id: id, status }); toast(status === "learning" ? "已加入学习清单。" : `已标记：${statusLabels[status]}。`); }
    catch (error) { toast(error.message, true); renderConcepts(); }
  }

  function askConcept(concept) {
    if ($("question-input").value.trim() || (usingPage() && $("quote-input").value.trim())) {
      if (model.conceptId !== concept.id) toast("提问框已有未提交的问题或引用。请先保存或清空，再切换到其他概念。");
      focusComposer();
      return;
    }
    model.conceptId = concept.id; model.parentId = "";
    clearPageReference();
    $("question-input").value = `请解释“${concept.term}”在本文中的含义。`;
    $("concept-term").value = string(concept.term).slice(0, 150);
    $("capture-concept").checked = false;
    updateComposerContext(); saveDraft(); focusComposer();
  }

  function openPage(page) { model.page = pageNumber(page); showView("reader"); $("main").scrollIntoView({ behavior: "smooth", block: "start" }); }

  async function loadPage(number) {
    if (!model.articleId) return;
    clearTimeout(model.resizeTimer);
    pdfReader.cancel();
    model.page = pageNumber(number);
    const serial = ++model.pageSerial;
    const id = model.articleId;
    $("page-number").value = model.page;
    $("page-prev").disabled = model.page <= 1;
    $("page-next").disabled = model.page >= pageCount();
    $("selection-action").hidden = true; model.lastSelection = "";
    if (!usingPage() && !model.zoteroSource && !$("quote-input").value.trim()) $("question-page").value = model.page;
    updateComposerContext(); saveDraft();
    model.pdfWidth = $("pdf-scroll").clientWidth;
    const annotationsRequest = loadAnnotations(id, model.page, serial);
    await pdfReader.render({ url: `/api/pdf?${query({ id })}`, pageNumber: model.page, zoom: $("pdf-zoom").value });
    await annotationsRequest;
  }

  function selectionText() {
    const selection = window.getSelection();
    if (!selection || !selection.rangeCount || selection.isCollapsed) return "";
    const range = selection.getRangeAt(0);
    const source = $("pdf-scroll").querySelector(".textLayer");
    if (!source?.contains(range.commonAncestorContainer)) return "";
    return selection.toString().trim();
  }

  async function copySelection() {
    const selected = selectionText() || model.lastSelection;
    if (!selected) { toast("请先在 PDF 原文中选中一个术语或句段。"); return; }
    try { await navigator.clipboard.writeText(selected); toast("选文已复制。"); }
    catch { toast("浏览器未允许自动复制。请保留选区，按 Ctrl+C 复制。"); }
  }

  async function loadAnnotations(id, page, serial) {
    model.annotations = [];
    $("page-annotations").hidden = true;
    try {
      const payload = await api(`/api/annotations?${query({ id, page })}`);
      if (id !== model.articleId || serial !== model.pageSerial) return;
      model.annotations = array(payload.annotations);
      pdfReader.setAnnotations(model.annotations);
      renderAnnotations();
    } catch (error) {
      if (id !== model.articleId || serial !== model.pageSerial) return;
      $("page-annotations").hidden = false;
      $("annotation-count").textContent = "读取未完成";
      $("annotation-list").replaceChildren(el("p", "annotation-empty", `旧有标注暂时无法读取：${error.message}`));
    }
  }

  function renderAnnotations() {
    $("page-annotations").hidden = !model.annotations.length;
    $("annotation-count").textContent = `${model.annotations.length} 条`;
    const root = $("annotation-list"); root.replaceChildren();
    model.annotations.forEach((annotation) => {
      const item = button("", "annotation-item", () => { pdfReader.selectAnnotation(annotation.id); openAnnotationReader(annotation); });
      item.append(el("strong", "", annotation.quote), el("span", "", annotation.note || "仅高亮 · 只读"));
      root.append(item);
    });
  }

  function openAnnotationReader(annotation) {
    $("annotation-dialog-title").textContent = annotation.archived ? "已取消的本地批注（只读）" : "旧有本地批注（只读）";
    $("annotation-quote").textContent = `阅读版第 ${annotation.page} 页 · ${annotation.quote || ""}`;
    $("annotation-note").textContent = annotation.note || "此记录只有高亮，没有批注文字。";
    $("selection-action").hidden = true;
    $("annotation-dialog").showModal();
  }

  async function loadLegacyRecords(id) {
    try {
      const records = await api(`/api/legacy-records?${query({ id })}`);
      if (id !== model.articleId) return;
      model.legacyRecords = records; model.legacyError = "";
    } catch (error) {
      if (id !== model.articleId) return;
      model.legacyError = error.message;
    }
    renderLegacyRecords();
  }

  function renderLegacyRecords() {
    const root = $("legacy-content"); root.replaceChildren();
    let draft = null;
    try { draft = readDraft("note-draft"); } catch { /* Stored records remain readable. */ }
    const notes = array(model.legacyRecords?.notes), annotations = array(model.legacyRecords?.annotations);
    $("legacy-records").hidden = !notes.length && !annotations.length && draft === null && !model.legacyError;
    const textRecord = (title, value) => {
      const entry = el("details", "legacy-entry"); entry.append(el("summary", "", title));
      entry.append(el("p", "legacy-text", value || "此版本为空。"));
      entry.append(button("复制文字", "text-button", async () => {
        try { await navigator.clipboard.writeText(string(value)); toast("文字已复制。"); }
        catch { toast("请选中文字，按 Ctrl+C 复制。"); }
      })); root.append(entry);
    };
    if (draft !== null) textRecord("浏览器保留的笔记草稿 · 未写入学习记录", draft);
    notes.forEach((note, index) => textRecord(`旧笔记 ${index + 1} · ${string(note.created_at).replace("T", " ")}`, note.body));
    if (annotations.length) {
      root.append(el("h3", "", "旧有本地标注（含已取消记录）"));
      annotations.forEach((annotation) => {
        const entry = el("details", "legacy-entry");
        entry.append(el("summary", "", `阅读版第 ${annotation.page} 页${annotation.archived ? " · 已取消" : " · 保留标注"}`), el("blockquote", "legacy-text", annotation.quote || ""), el("p", "legacy-text", annotation.note || "仅高亮"));
        if (Number.isInteger(annotation.page) && annotation.page >= 1 && annotation.page <= pageCount()) entry.append(button("查看阅读版来源页 ↗", "text-button", () => openPage(annotation.page)));
        root.append(entry);
      });
    }
    if (model.legacyError) root.append(el("p", "question-error", `旧记录读取未完成：${model.legacyError}`), button("重新读取旧记录", "text-button", () => loadLegacyRecords(model.articleId)));
  }

  function captureSelection(asConcept = false) {
    const selected = selectionText() || model.lastSelection;
    if (!selected) { toast("请先在 PDF 原文中选中一个术语或句段。"); return false; }
    if (asConcept && selected.length > 150) { toast("这段选文超过 150 字，请作为句段提问，或只选一个术语。", true); return false; }
    $("quote-input").value = selected;
    $("question-page").value = model.page;
    $("question-scope").value = "page";
    model.zoteroSource = null;
    $("include-image").checked = false;
    if (asConcept) {
      model.conceptId = "";
      $("capture-concept").checked = true;
      $("concept-term").value = selected;
      if (!$("question-input").value.trim()) $("question-input").value = `请解释“${selected}”在本文中的含义。`;
    } else {
      $("capture-concept").checked = false;
      model.conceptId = "";
    }
    updateComposerContext(); saveDraft(); $("selection-action").hidden = true;
    focusComposer();
    if (!asConcept) toast("选文已带入，原有问题文字已保留。写下问题后再保存或解释。");
    return true;
  }

  function setQuestionPanel(open, remember = false) {
    const panel = $("question-panel");
    const returnFocus = !open && panel.contains(document.activeElement);
    panel.hidden = !open;
    document.querySelector(".reading-layout").classList.toggle("question-panel-collapsed", !open);
    $("question-panel-toggle").setAttribute("aria-expanded", string(open));
    const label = `${open ? "收起" : "展开"}读懂这一处`;
    $("question-panel-toggle").setAttribute("aria-label", label);
    $("question-panel-toggle").title = label;
    $("question-panel-toggle-label").textContent = open ? "收起解释" : "展开解释";
    if (remember) {
      try { localStorage.setItem(questionPanelKey, string(open)); } catch { /* The panel still works without browser storage. */ }
    }
    if (returnFocus) $("question-panel-toggle").focus({ preventScroll: true });
  }

  function focusComposer() {
    setQuestionPanel(true);
    $("question-input").focus({ preventScroll: true });
    $("question-input").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function mergeQuestion(question) {
    if (!question?.id) return;
    model.state.questions = [question, ...currentQuestions().filter((item) => item.id !== question.id)];
  }

  async function submitQuestion(generate) {
    if (model.submitting || !model.articleId) return;
    if (generate && !model.library.ai_available) { toast("智能解释目前不可用，可以先保存问题；已有离线释义不受影响。", true); return; }
    const question = $("question-input").value.trim();
    const captureConcept = $("capture-concept").checked;
    const term = $("concept-term").value.trim();
    if (!question) { toast("先写下想了解的问题。"); $("question-input").focus(); return; }
    if (captureConcept && !term) { toast("请填写要收藏的概念名称。"); $("concept-term").focus(); return; }
    const articleId = model.articleId;
    const submittedDraft = JSON.stringify(draftSnapshot());
    model.submitting = true;
    $("save-question").disabled = true; $("explain-question").disabled = true; $("article-select").disabled = true;
    try {
      const payload = await api("/api/question", {
        article_id: articleId, question, source_scope: $("question-scope").value, reading_page: model.page,
        page: usingPage() ? (model.zoteroSource ? 1 : pageNumber($("question-page").value)) : model.page,
        quote: usingPage() ? $("quote-input").value.trim() : "",
        zotero_source: usingPage() && model.zoteroSource ? { attachment_key: model.zoteroSource.attachment_key, page: attachmentPage($("question-page").value) } : null,
        concept_id: model.conceptId, depth: $("question-depth").value, parent_id: model.parentId,
        generate, include_image: usingPage() && $("include-image").checked, term: captureConcept ? term : "", capture_concept: captureConcept,
      });
      if (articleId !== model.articleId) return;
      mergeQuestion(payload.question); model.activeQuestionId = payload.question.id;
      if (JSON.stringify(draftSnapshot()) === submittedDraft) {
        $("question-input").value = "";
        clearPageReference();
        $("concept-term").value = "";
        $("capture-concept").checked = false;
        model.conceptId = "";
        model.parentId = "";
        model.lastSelection = "";
        $("selection-action").hidden = true;
      }
      updateComposerContext(); saveDraft(); renderQuestions(); renderActiveAnswer();
      toast(generate ? "问题已保存，正在解释。你可以继续阅读。" : "问题已保存在这篇文章的阅读记录中。");
      if (captureConcept) await refreshArticleData();
      schedulePoll();
    } catch (error) { toast(error.message, true); }
    finally { model.submitting = false; $("save-question").disabled = false; $("explain-question").disabled = !model.library.ai_available; $("article-select").disabled = false; }
  }

  function schedulePoll() {
    clearTimeout(model.pollTimer);
    if (!currentQuestions().some((question) => question.status === "running")) return;
    const id = model.articleId;
    model.pollTimer = setTimeout(async () => {
      try {
        const previousRunning = new Set(currentQuestions().filter((item) => item.status === "running").map((item) => item.id));
        const payload = await api(`/api/questions?${query({ id })}`);
        if (id !== model.articleId) return;
        model.state.questions = array(payload.questions);
        const finished = currentQuestions().filter((item) => previousRunning.has(item.id) && item.status !== "running");
        renderQuestions(); renderActiveAnswer();
        if (finished.length) {
          await refreshArticleData();
          toast(finished.some((item) => item.status === "failed") ? "有一条解释未完成，问题已保留，可在阅读记录中重试。" : "解释已完成，已保存到阅读记录。", finished.some((item) => item.status === "failed"));
        }
      } catch (error) { if (id === model.articleId) toast(`暂时无法刷新解释状态：${error.message}`, true); }
      finally { if (id === model.articleId) schedulePoll(); }
    }, 3000);
  }

  async function retryQuestion(question) {
    if (!model.library.ai_available) { toast("智能解释目前不可用，问题记录会保留。", true); return; }
    try {
      const id = model.articleId;
      const payload = await api("/api/retry", { article_id: id, question_id: question.id });
      if (id !== model.articleId) return;
      mergeQuestion(payload.question); model.activeQuestionId = payload.question.id;
      renderQuestions(); renderActiveAnswer(); schedulePoll(); toast("正在继续解释这条已保存的问题。");
    } catch (error) { toast(error.message, true); }
  }

  function followUp(question) {
    model.parentId = question.id; model.conceptId = string(question.concept_id);
    clearPageReference();
    $("capture-concept").checked = false;
    model.activeQuestionId = question.id;
    updateComposerContext(); saveDraft(); renderActiveAnswer(); focusComposer();
    toast("已接续上次问答，本次默认从整篇文章找依据。");
  }

  function clearPageReference() {
    $("question-scope").value = "article";
    model.zoteroSource = null;
    $("quote-input").value = ""; $("question-page").value = model.page;
    $("include-image").checked = false;
  }

  function reuseFollowupSource() {
    const parent = currentQuestions().find((item) => item.id === model.parentId);
    if (!parent || parent.source_scope === "article") return;
    $("question-scope").value = "page";
    model.zoteroSource = cleanZoteroSource(parent.zotero_source);
    $("question-page").value = model.zoteroSource ? model.zoteroSource.page : pageNumber(parent.page);
    $("quote-input").value = string(parent.quote); $("include-image").checked = false;
    updateComposerContext(); saveDraft();
    $("question-scope").focus({ preventScroll: true });
  }

  function renderAnswer(target, answer) {
    [["解释", answer.explanation, ""], ["本文证据", answer.paper_evidence, ""], ["通用背景", answer.background, ""], ["边界与不确定性", answer.boundary, "boundary"], ["接着读时可以追问", answer.check_question, "check"]].forEach(([title, value, type]) => {
      if (!value) return;
      const section = el("section", `answer-section ${type}`); section.append(el("h4", "", title), el("p", "", textValue(value))); target.append(section);
    });
    const related = array(answer.related_concepts);
    if (related.length) {
      const links = el("div", "concept-links");
      related.forEach((value) => {
        const id = typeof value === "string" ? value : value.id || value.concept_id;
        const name = typeof value === "string" ? value : value.term || value.name || id;
        const concept = currentConcepts().find((item) => item.id === id || item.term === name);
        if (concept) links.append(button(concept.term, "", () => { $("concept-search").value = concept.term; $("concept-filter").value = "all"; showView("concepts"); renderConcepts(); }));
        else if (name) links.append(el("span", "pill", name));
      }); target.append(links);
    }
  }

  function questionActions(target, question) {
    const actions = el("div", "question-actions");
    if (question.status === "completed") actions.append(button("继续追问 ↗", "text-button", () => followUp(question)));
    if (["saved", "failed"].includes(question.status)) actions.append(button(question.status === "failed" ? "重新解释并保存" : "智能解释并保存", "button soft", () => retryQuestion(question)));
    if (question.source_scope !== "article") {
      if (question.zotero_source) actions.append(button(`查看附件来源 · ${sourceLabel(question.zotero_source)}`, "text-button", () => zoteroManager.openSource(question.zotero_source)));
      else actions.append(button(`${question.source_scope === "page" ? "查看引用页" : "回到原记录页"} · 阅读版第 ${pageNumber(question.page)} 页`, "text-button", () => openPage(question.page)));
    }
    if (question.reading_page && (question.source_scope === "article" || question.zotero_source || question.reading_page !== question.page)) {
      actions.append(button(`回到提问时的阅读位置 · 第 ${question.reading_page} 页`, "text-button", () => openPage(question.reading_page)));
    }
    target.append(actions);
  }

  function renderQuestionContent(target, question) {
    if (question.zotero_source) target.append(el("p", "zotero-answer-source", `来源：${sourceLabel(question.zotero_source)}（附件独立计页）`));
    if (question.status === "running") target.append(el("p", "question-message loading-pulse", "正在结合原文生成解释。问题已保存，可以继续阅读。"));
    if (question.status === "saved") target.append(el("p", "question-message", "这个问题已保存，尚未调用智能解释。"));
    if (question.status === "failed") target.append(el("p", "question-error", question.error || "解释没有完成，问题和选文仍然保留。"));
    if (question.answer) renderAnswer(target, question.answer);
    questionActions(target, question);
  }

  function renderQuestions() {
    const root = $("question-list"); root.replaceChildren();
    const questions = [...currentQuestions()].sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
    $("history-count").textContent = questions.length;
    const search = $("history-search").value.trim().toLocaleLowerCase();
    const matches = questions.filter((item) => !search || [item.question, item.quote, textValue(item.answer)].join(" ").toLocaleLowerCase().includes(search));
    $("question-summary").textContent = search ? `${matches.length} / ${questions.length} 条` : `${questions.length} 条记录`;
    if (!matches.length) { root.append(el("div", "empty-state", search ? "没有匹配的问题记录。" : "还没有学习问答。预制导读和概念不计作你的提问；保存第一个问题后，它会出现在这里。")); return; }
    matches.forEach((question) => {
      const card = el("article", "question-card");
      const meta = el("div", "question-meta");
      const date = new Date(question.created_at);
      const scope = question.source_scope === "article" ? "整篇文章" : referenceLabel(question);
      meta.append(el("span", `question-status ${question.status}`, questionLabels[question.status] || question.status), el("span", "", Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })), el("span", "", `${depthLabels[question.depth] || "标准"} · ${scope}`));
      card.append(meta, el("h3", "", question.question));
      if (question.parent_id) card.append(el("p", "muted", "这条记录延续了此前的问答。"));
      if (question.quote) card.append(el("blockquote", "", question.quote));
      renderQuestionContent(card, question); root.append(card);
    });
  }

  function renderActiveAnswer() {
    const root = $("active-answer"); root.replaceChildren();
    const question = currentQuestions().find((item) => item.id === model.activeQuestionId);
    $("coach-empty").hidden = Boolean(question);
    if (!question) return;
    root.append(el("div", "eyebrow", "当前解释"), el("h3", "", question.question));
    renderQuestionContent(root, question);
  }

  function bindEvents() {
    document.querySelectorAll(".tab").forEach((node) => node.addEventListener("click", () => showView(node.dataset.view)));
    $("article-select").addEventListener("change", () => switchArticle($("article-select").value));
    $("library-open").addEventListener("click", () => showView("network"));
    $("zotero-open").addEventListener("click", () => { showView("zotero"); zoteroManager.show(); });
    $("zotero-source-open").addEventListener("click", () => { if (model.zoteroSource) zoteroManager.openSource(model.zoteroSource); });
    $("zotero-source-clear").addEventListener("click", () => {
      clearPageReference();
      updateComposerContext(); saveDraft();
    });
    $("article-assets").addEventListener("click", () => zoteroManager.showArticle());
    $("page-prev").addEventListener("click", () => loadPage(model.page - 1));
    $("page-next").addEventListener("click", () => loadPage(model.page + 1));
    $("page-number").addEventListener("change", () => loadPage($("page-number").value));
    $("pdf-zoom").addEventListener("change", () => loadPage(model.page));
    $("pdf-retry").addEventListener("click", () => { pdfReader.resetDocument(); loadPage(model.page); });
    const resizeObserver = new ResizeObserver(() => {
      const width = $("pdf-scroll").clientWidth;
      if (model.view !== "reader" || $("pdf-zoom").value !== "fit" || !width || Math.abs(width - model.pdfWidth) < 2) return;
      clearTimeout(model.resizeTimer);
      model.resizeTimer = setTimeout(() => { if (model.view === "reader" && $("pdf-zoom").value === "fit") loadPage(model.page); }, 120);
    });
    resizeObserver.observe($("pdf-scroll"));
    $("use-selection").addEventListener("click", () => captureSelection());
    $("copy-selection").addEventListener("click", copySelection);
    $("copy-selection-popup").addEventListener("click", copySelection);
    $("annotation-close").addEventListener("click", () => $("annotation-dialog").close());
    $("pdf-scroll").addEventListener("click", (event) => {
      if (!window.getSelection()?.isCollapsed) return;
      const annotation = pdfReader.annotationAt(event.clientX, event.clientY);
      if (annotation) { pdfReader.selectAnnotation(annotation.id); openAnnotationReader(annotation); }
    });
    $("capture-selection").addEventListener("click", () => captureSelection());
    $("collect-selection").addEventListener("click", () => { if (captureSelection(true)) submitQuestion(true); });
    ["use-selection", "capture-selection", "collect-selection", "copy-selection", "copy-selection-popup"].forEach((id) => $(id).addEventListener("pointerdown", (event) => event.preventDefault()));
    document.addEventListener("selectionchange", () => {
      if (model.view !== "reader") return;
      const selected = selectionText();
      if (selected) model.lastSelection = selected;
      $("selection-action").hidden = !selected;
      $("collect-selection").disabled = selected.length > 150 || !model.library?.ai_available;
      $("collect-selection").title = selected.length > 150 ? "概念名称限 150 字；句段可带到提问框。" : "解释这个术语，并加入个人概念收藏。";
    });
    ["concept-search", "concept-filter"].forEach((id) => $(id).addEventListener(id === "concept-search" ? "input" : "change", renderConcepts));
    $("history-search").addEventListener("input", renderQuestions);
    $("question-form").addEventListener("input", () => { updateComposerContext(); saveDraft(); });
    $("question-form").addEventListener("change", () => { updateComposerContext(); saveDraft(); });
    $("question-form").addEventListener("submit", (event) => { event.preventDefault(); submitQuestion(event.submitter?.dataset.generate === "true"); });
    $("followup-cancel").addEventListener("click", () => { model.parentId = ""; updateComposerContext(); saveDraft(); });
    $("followup-source-reuse").addEventListener("click", reuseFollowupSource);
    $("concept-context-clear").addEventListener("click", () => { model.conceptId = ""; updateComposerContext(); saveDraft(); });
    window.addEventListener("beforeunload", saveDraft);
  }

  function renderArticleOptions() {
    const articles = topicsManager ? topicsManager.visibleArticles() : array(model.library.articles);
    $("article-select").replaceChildren();
    articles.forEach((article) => { const option = el("option", "", article.short_title || article.title); option.value = article.id; $("article-select").append(option); });
    if (!articles.length) { const option = el("option", "", "主题内尚无文章"); option.value = ""; $("article-select").append(option); }
    $("article-select").disabled = !articles.length;
    if (articles.some((article) => article.id === model.articleId)) $("article-select").value = model.articleId;
    $("library-count").textContent = `${articles.length} 篇文章 · 单篇学习记录复用`;
    return articles;
  }

  async function syncTopicView({ resetSelection = false, scopeOnly = false } = {}) {
    libraryManager?.syncScope(resetSelection);
    if (scopeOnly) return;
    // A topic change supersedes any article still loading for the previous scope.
    model.switchSerial += 1;
    const articles = renderArticleOptions();
    if (!articles.length) {
      $("load-error").hidden = true;
      showTopicOverview("这里还没有文章，可加入已有文章或从 Zotero 来源添加。");
    } else if (!articles.some((article) => article.id === model.articleId)) await switchArticle(articles[0].id);
    else syncReadingLocation();
  }

  function showTopicOverview(message) {
    saveDraft(); clearTimeout(model.pollTimer);
    model.articleId = ""; model.article = null; model.content = {}; model.state = cleanState({});
    $("article-tag").textContent = "尚未打开文章";
    document.querySelector(".workspace").classList.add("topic-empty");
    document.querySelector(".app-shell").classList.add("topic-empty");
    $("article-title").textContent = topicsManager.topic()?.name || "文献学习助手";
    $("article-subtitle").textContent = message;
    showView("network");
  }

  async function addImportedArticle(articleId, topicId) {
    await topicsManager.addArticle(articleId, topicId);
    await refreshLibrary();
    if (topicId) await topicsManager.select(topicId);
  }

  async function refreshStudyContent() {
    const id = model.articleId;
    if (id) {
      const payload = await api(`/api/article?${query({ id })}`);
      if (id === model.articleId) {
        model.content = payload.content || model.content;
        renderOverview(); renderSections(); renderProgress(); renderConcepts();
        studyManager.syncArticle(model.content);
      }
    }
    await libraryManager?.refreshGenerated();
  }

  async function refreshLibrary(initialTopic) {
    model.library = await api("/api/library");
    await topicsManager?.refresh(initialTopic);
    const articles = renderArticleOptions();
    if (!model.library.articles.length) $("import-box").open = true;
    syncModelAvailability();
    return articles;
  }

  function syncModelAvailability(status = model.library?.model_backend) {
    if (!model.library) return;
    if (status) {
      model.library.model_backend = status;
      model.library.ai_available = Boolean(status.available);
    }
    $("ai-status").classList.toggle("ready", Boolean(model.library.ai_available));
    $("ai-status").title = `${status?.label || "智能解释"}：${status?.message || (model.library.ai_available ? "只在明确点击后发送" : "暂不可用；可阅读离线资料并保存问题")}`;
    $("explain-question").disabled = model.submitting || !model.library.ai_available;
    $("collect-selection").disabled = model.lastSelection.length > 150 || !model.library.ai_available;
    modelSettingsManager?.sync(status);
  }

  async function start() {
    let panelOpen = false;
    try { panelOpen = localStorage.getItem(questionPanelKey) === "true"; } catch { /* Default to more reading space. */ }
    setQuestionPanel(panelOpen);
    $("question-panel-toggle").addEventListener("click", () => {
      const open = $("question-panel").hidden;
      setQuestionPanel(open, true);
      if (open) $("question-panel-close").focus({ preventScroll: true });
    });
    $("question-panel-close").addEventListener("click", () => setQuestionPanel(false, true));
    $("question-panel").addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); setQuestionPanel(false, true); }
    });
    topicsManager = createTopicsManager({ api, getLibrary: () => model.library, refreshLibrary, toast,
      afterImport: (options) => studyManager.afterImport(options),
      canChange: () => { if (!model.submitting) return true; toast("请等待当前问题保存完成，再切换主题。"); return false; },
      onChange: syncTopicView,
    });
    zoteroManager = createZoteroManager({
      api, getArticleId: () => model.articleId, refreshLibrary, openArticle: switchArticle, toast,
      loadAssets: (id) => libraryManager.showAssets(id),
      prepareImportTopics: (select) => topicsManager.importOptions(select), addToTopic: addImportedArticle,
      chooseArticleTopic: (id) => topicsManager.chooseArticleTopic(id),
      afterImport: (options) => studyManager.afterImport(options),
      activate: () => showView("zotero"),
      takeSource: async ({ articleId, source, quote, comment }) => {
        if (model.submitting) { toast("请等待当前问题保存完成。"); return false; }
        const incoming = cleanZoteroSource(source);
        if (!incoming) { toast("这条附件来源暂时不可用，请刷新 Zotero 资料。", true); return false; }
        const opened = model.articleId === articleId || await switchArticle(articleId);
        if (!opened || model.articleId !== articleId) return false;
        if (usingPage() && $("quote-input").value.trim() && $("quote-input").value !== quote) {
          toast("引用框已有内容，请先保存或清空引用，再带入这条附件来源。"); focusComposer(); return false;
        }
        model.zoteroSource = incoming; model.conceptId = "";
        $("question-scope").value = "page";
        $("quote-input").value = quote;
        $("question-page").value = model.zoteroSource.page;
        if (!$("question-input").value.trim() && comment) $("question-input").value = `关于这段原文，我的批注是：${comment}\n\n请结合原文帮助我理解。`;
        $("capture-concept").checked = false; $("include-image").checked = false;
        updateComposerContext(); saveDraft(); showView("zotero"); focusComposer();
        toast("附件来源已带入。请核对问题，再选择保存或智能解释。"); return true;
      },
    });
    libraryManager = createLibraryManager({
      api, getLibrary: () => model.library, getArticleId: () => model.articleId, getPage: () => model.page,
      getScope: () => topicsManager.scope(), getScopedArticles: (options) => topicsManager.visibleArticles(options),
      prepareImportTopics: (select) => topicsManager.importOptions(select), addToTopic: addImportedArticle,
      afterImport: (options) => studyManager.afterImport(options),
      openArticle: switchArticle, toast,
      openSource: async (id, page) => {
        const opened = model.articleId === id || await switchArticle(id);
        if (opened && model.articleId === id) { openPage(page); return true; }
        return false;
      },
    });
    studyManager = createStudyManager({ api, getArticleId: () => model.articleId, getLibrary: () => model.library,
      getScope: () => topicsManager.scope(), getComparisonSelection: () => libraryManager.selectedIds(),
      onPublished: refreshStudyContent, toast,
    });
    modelSettingsManager = createModelSettingsManager({ api, getLibrary: () => model.library, toast,
      onChange: (status) => {
        syncModelAvailability(status);
        libraryManager.syncLibrary();
        renderQuestions(); renderActiveAnswer();
        studyManager.refresh().catch((error) => toast(`学习内容整理状态暂时无法读取：${error.message}`, true));
        libraryManager.refreshGenerated().catch((error) => toast(`跨文记录暂时无法刷新：${error.message}`, true));
      },
    });
    bindEvents();
    try {
      const params = new URLSearchParams(location.search);
      const articles = await refreshLibrary(params.get("topic") || "");
      if (!articles.length) {
        await syncTopicView();
        if (!topicsManager.topic()) zoteroManager.show();
      } else {
        const requested = params.get("article");
        await switchArticle(articles.some((item) => item.id === requested) ? requested : articles[0].id);
      }
      studyManager.refresh().catch((error) => toast(`学习内容整理状态暂时无法读取：${error.message}`, true));
    } catch (error) { $("load-error").textContent = error.message; $("load-error").hidden = false; $("article-title").textContent = "文献学习助手"; toast(error.message, true); }
  }

  start();
})();

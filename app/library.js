import { renderImportMetadata } from "./import-preview.js";

export function createLibraryManager({ api, getLibrary, getArticleId, getPage, getScope, getScopedArticles, prepareImportTopics, addToTopic, afterImport, openArticle, openSource, toast }) {
  const $ = (id) => document.getElementById(id);
  const list = (value) => Array.isArray(value) ? value : [];
  const text = (value) => value == null ? "" : String(value);
  const state = {
    visible: false, selected: new Set(), relations: [], comparisons: [],
    preview: null, previewUrl: "", importBusy: false, compareSerial: 0, compareBusy: false,
    networkSerial: 0, networkKey: "", assetSerial: 0, pollTimer: null,
    comparisonSaving: false, relationSaving: false,
  };
  const relationTypes = { complement: "补充视角", method: "方法关联", definition: "定义差异", hypothesis: "待验证联想" };
  const comparisonStatuses = { saved: "已保存", running: "正在比较", completed: "已生成 · 需核对", failed: "生成未完成" };
  const comparisonFields = [
    ["question", "科学问题"], ["unit", "分析单位"], ["cohort", "研究数据与设计"],
    ["normal", "对照与比较基准"], ["definition", "定义与判定"], ["evidence", "主要发现与证据"], ["limits", "局限"],
  ];
  const articles = () => list(getLibrary()?.articles);
  const scopedArticles = () => getScopedArticles({ comparison: true });
  const currentTopicId = () => { const scope = getScope(); return scope.topic && !scope.cross ? scope.topic.id : ""; };
  const article = (id) => articles().find((item) => item.id === id);
  const articleName = (id) => article(id)?.short_title || article(id)?.title || id;
  const selectedIds = () => scopedArticles().filter((item) => state.selected.has(item.id)).map((item) => item.id);
  const aiAvailable = () => Boolean(getLibrary()?.ai_available);

  function node(tag, className, value) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value != null) result.textContent = text(value);
    return result;
  }

  function action(label, className, handler) {
    const result = node("button", className, label);
    result.type = "button";
    result.addEventListener("click", handler);
    return result;
  }

  function status(id, message, error = false) {
    const target = $(id);
    target.textContent = message;
    target.hidden = !message;
    target.classList.toggle("is-error", error);
  }

  function safeLink(url, label, localOnly = false) {
    if (!url) return null;
    let resolved;
    try { resolved = new URL(url, location.href); } catch { return null; }
    if (!["http:", "https:"].includes(resolved.protocol) || (localOnly && resolved.origin !== location.origin)) return null;
    const link = node("a", "text-button", label);
    link.href = resolved.href;
    link.target = "_blank";
    link.rel = "noopener";
    return link;
  }

  function sourceButton(articleId, page, label) {
    const number = Number(page);
    const source = article(articleId);
    if (!source || !Number.isInteger(number) || number < 1 || (source.pages && number > source.pages)) return null;
    const button = action(label || `${articleName(articleId)} · PDF 第 ${number} 页 ↗`, "text-button source-button", async () => {
      button.disabled = true;
      try { await openSource(articleId, number); }
      catch (error) { toast(error.message, true); }
      finally { button.disabled = false; }
    });
    return button;
  }

  function renderCitations(target, values) {
    const citations = list(values);
    if (!citations.length) return;
    const root = node("div", "network-citations");
    citations.forEach((citation) => {
      const entry = node("div", "network-citation");
      const button = sourceButton(citation.article_id, citation.page);
      if (button) entry.append(button);
      else entry.append(node("span", "field-help", "原文位置尚未提供"));
      if (citation.quote) entry.append(node("blockquote", "", citation.quote));
      root.append(entry);
    });
    target.append(root);
  }

  function networkQuery() {
    const scope = $("network-scope").value;
    const topic = getScope().topic;
    const ids = selectedIds();
    if ((scope === "topic" && !topic) || (scope === "selected" && (ids.length < 2 || ids.length > 5))) return null;
    return new URLSearchParams({ scope, ...(scope === "topic" ? { topic_id: topic.id } : {}), ...(scope === "selected" ? { ids: ids.join(",") } : {}), ...($("network-history").checked ? { history: "1" } : {}) }).toString();
  }

  function updateNetworkScope() {
    const topic = getScope().topic;
    const option = $("network-scope").querySelector('option[value="topic"]');
    option.disabled = !topic; option.textContent = topic ? `当前主题：${topic.name}` : "当前学习主题（未选择）";
    const params = networkQuery();
    const label = $("network-scope").value === "topic" ? topic?.name : $("network-scope").value === "selected" ? `已选 ${selectedIds().length} 篇文章` : "全部文章";
    $("network-scope-description").textContent = params ? `关系与问答的显示、导出范围：${label}；${$("network-history").checked ? "包含历史整理结果" : "仅当前整理结果"}。手动问题仍分别保留。` : "请先选择 2–5 篇文章，或改用当前主题/全部文章范围。";
    ["markdown", "json"].forEach((format) => {
      const link = $("network-export-" + format);
      if (params) link.href = `/api/network/export?${params}&format=${format}`; else link.removeAttribute("href");
      link.setAttribute("aria-disabled", String(!params));
    });
    return params;
  }

  function renderNetwork() {
    $("relation-summary-count").textContent = `· ${state.relations.length} 条`;
    $("relation-list").replaceChildren(); renderRelations($("relation-list"), state.relations);
    renderComparisonHistory();
  }

  function updateScope() {
    const count = selectedIds().length;
    $("comparison-selection-status").textContent = `已选择 ${count} 篇；逐项对照和跨文总结使用同一组文章。`;
    $("build-compare").disabled = state.compareBusy || count < 2;
    $("comparison-save").disabled = state.comparisonSaving || count < 2;
    $("comparison-generate").disabled = state.comparisonSaving || count < 2 || !aiAvailable();
    $("comparison-generate").title = aiAvailable() ? "仅在点击后发送所选文章的原文节选与导读" : "智能比较暂不可用；仍可逐项对照或保存比较问题";
  }

  function scopeChanged() {
    state.compareSerial += 1;
    $("comparison-table").replaceChildren();
    status("compare-status", "选择已更新，点击“查看逐项对照”读取这组文章的资料。");
    updateScope(); updateNetworkScope();
    if (state.visible && $("network-scope").value === "selected") loadNetwork();
  }

  function renderChoices() {
    const root = $("comparison-choices");
    root.replaceChildren();
    scopedArticles().forEach((item) => {
      const label = node("label", "comparison-choice");
      const check = node("input"); check.type = "checkbox"; check.value = item.id; check.checked = state.selected.has(item.id);
      check.addEventListener("change", () => {
        if (check.checked && state.selected.size >= 5) { check.checked = false; toast("一次最多对照 5 篇文章。"); return; }
        if (check.checked) state.selected.add(item.id); else state.selected.delete(item.id);
        scopeChanged();
      });
      const description = node("span");
      description.append(node("strong", "", item.short_title || item.title), node("small", "", item.title));
      label.append(check, description); root.append(label);
    });
    updateScope();
  }

  function updateRelationPages(side, reset = false) {
    const selected = $("relation-" + side).value;
    const input = $("relation-" + side + "-page");
    input.max = article(selected)?.pages || "";
    if (reset) input.value = selected === getArticleId() ? getPage() : "";
  }

  function syncLibrary() {
    const available = new Set(scopedArticles().map((item) => item.id));
    state.selected = new Set([...state.selected].filter((id) => available.has(id)));
    renderChoices(); updateNetworkScope();
    ["source", "target"].forEach((side) => {
      const select = $("relation-" + side), previous = select.value;
      select.replaceChildren();
      const placeholder = node("option", "", "选择文章"); placeholder.value = ""; select.append(placeholder);
      scopedArticles().forEach((item) => { const option = node("option", "", item.short_title || item.title); option.value = item.id; select.append(option); });
      select.value = available.has(previous) ? previous : side === "source" && available.has(getArticleId()) ? getArticleId() : "";
      updateRelationPages(side, select.value !== previous);
    });
  }

  async function showAssets(id) {
    const serial = ++state.assetSerial;
    $("assets-title").textContent = "本机附件与来源说明";
    $("assets-content").replaceChildren(node("p", "field-help", "正在读取本机资源…"));
    try {
      const payload = await api(`/api/assets?id=${encodeURIComponent(id)}`);
      if (serial !== state.assetSerial) return;
      const root = $("assets-content"); root.replaceChildren();
      const source = node("section", "asset-provenance");
      source.append(node("h3", "", "知识库与来源"));
      source.append(node("p", "", payload.kb_paper_id ? `知识库规范 ID：${payload.kb_paper_id}` : "尚未关联知识库规范文献。"));
      if (payload.kb_source) source.append(node("p", "field-help", `收录来源：${text(payload.kb_source)}`));
      if (payload.source_note) source.append(node("p", "field-help", payload.source_note));
      const verification = safeLink(payload.verification_url, "核对官方来源 ↗");
      if (verification) source.append(verification);
      root.append(source, node("h3", "asset-heading", "本机资源"));
      if (!list(payload.assets).length) root.append(node("p", "field-help", "没有登记可下载的附件。"));
      list(payload.assets).forEach((asset) => {
        const entry = node("div", "asset-item");
        entry.append(node("strong", "", asset.label || asset.filename || "附件"));
        entry.append(node("p", "field-help", [asset.filename, asset.pages ? `${asset.pages} 页` : ""].filter(Boolean).join(" · ")));
        const link = safeLink(asset.url, "打开或下载 ↗", true);
        entry.append(link || node("span", "field-help", "下载链接暂不可用")); root.append(entry);
      });
    } catch (error) { if (serial === state.assetSerial) $("assets-content").replaceChildren(node("p", "network-status is-error", error.message)); }
  }

  function setImportBusy(busy) {
    state.importBusy = busy;
    ["import-file", "import-preview-button", "import-title", "import-doi", "import-short-title", "import-topic", "import-study-after"].forEach((id) => { $(id).disabled = busy; });
    $("import-commit-button").disabled = busy || !state.preview;
  }

  async function previewImport(event) {
    event.preventDefault();
    if (state.importBusy) return;
    const file = $("import-file").files[0];
    if (!file) return;
    if (!getLibrary()?.token) { status("import-status", "资料库尚未载入，请稍后再预览文件。", true); return; }
    if (!file.size || file.size > 60 * 1024 * 1024) { status("import-status", "请选择大小不超过 60 MB 的非空 PDF 文件。", true); return; }
    if (file.type !== "application/pdf" && !/\.pdf$/i.test(file.name)) { status("import-status", "目前只支持 PDF 文件。", true); return; }
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = ""; state.preview = null; $("import-commit-form").hidden = true;
    setImportBusy(true); status("import-status", "正在本机检查 PDF 并读取预览…");
    try {
      const response = await fetch("/api/import/preview", {
        method: "POST", headers: { "Content-Type": "application/pdf", "X-Learning-Token": getLibrary().token, "X-Filename": encodeURIComponent(file.name) }, body: file,
      });
      let payload;
      try { payload = await response.json(); } catch { throw new Error("没有收到有效预览，请检查本地服务后重试。"); }
      if (!response.ok) throw new Error(payload.error || "PDF 预览未完成。");
      state.preview = payload;
      $("import-title").value = text(payload.title);
      $("import-doi").value = text(payload.doi);
      $("import-short-title").value = "";
      prepareImportTopics($("import-topic"));
      $("import-study-after").checked = false;
      $("import-preview-info").textContent = `${payload.pages} 页 PDF · 核对后加入`;
      state.previewUrl = URL.createObjectURL(file);
      renderImportMetadata($("import-metadata"), { ...payload, filename: file.name }, state.previewUrl);
      const duplicates = $("import-duplicates"); duplicates.replaceChildren(); duplicates.hidden = !list(payload.duplicates).length;
      if (!duplicates.hidden) {
        duplicates.append(node("strong", "", "发现可能重复的文章，请先核对"));
        list(payload.duplicates).forEach((item) => {
          const row = node("p", "", `${item.title || articleName(item.id)}${item.reason ? ` · ${item.reason}` : ""}`);
          if (item.id) row.append(document.createTextNode(" "), action("查看已有文章 ↗", "text-button", () => openArticle(item.id)));
          duplicates.append(row);
        });
      }
      $("import-commit-form").hidden = false;
      status("import-status", "预览完成，尚未加入资料库。请核对信息后确认导入。");
    } catch (error) { status("import-status", error.message, true); }
    finally { setImportBusy(false); }
  }

  async function commitImport(event) {
    event.preventDefault();
    if (state.importBusy || !state.preview || !$("import-commit-form").reportValidity()) return;
    const body = { preview_id: state.preview.preview_id, title: $("import-title").value.trim(), doi: $("import-doi").value.trim(), short_title: $("import-short-title").value.trim() };
    const topicId = $("import-topic").value;
    const studyAfter = $("import-study-after").checked;
    if (!body.title) { status("import-status", "请填写文章标题。", true); return; }
    setImportBusy(true); status("import-status", "正在加入本机资料库…");
    let imported;
    try {
      const payload = await api("/api/import/commit", body);
      imported = payload.article;
      URL.revokeObjectURL(state.previewUrl); state.previewUrl = "";
      state.preview = null; $("import-commit-form").hidden = true; $("import-file").value = "";
      await addToTopic(imported.id, topicId); syncLibrary();
      status("import-status", "文章已加入资料库。导读尚未整理，可先阅读原文。");
      await openArticle(imported.id);
      toast("文章已导入，学习记录将独立保存。");
      if (studyAfter) {
        const result = await afterImport({ articleIds: [imported.id], topicId });
        status("import-status", result.error || "文章已导入，并已开始整理学习内容；可查看整理进度。", Boolean(result.error));
      }
    } catch (error) {
      status("import-status", imported ? `文章已导入，但加入主题或打开未完成：${error.message}。可在“编辑主题”中加入已有文章，无需重复导入。` : error.message, true);
    } finally { setImportBusy(false); }
  }

  function requireSelection() {
    const ids = selectedIds();
    if (ids.length < 2 || ids.length > 5) { toast("请先选择 2–5 篇文章。"); return null; }
    return ids;
  }

  async function buildComparison() {
    const ids = requireSelection();
    if (!ids || state.compareBusy) return;
    const serial = ++state.compareSerial;
    state.compareBusy = true; updateScope(); status("compare-status", "正在读取这些文章的对照资料…");
    try {
      const payload = await api("/api/compare", { article_ids: ids });
      if (serial !== state.compareSerial) return;
      renderComparisonTable(list(payload.articles));
      status("compare-status", `已显示 ${list(payload.articles).length} 篇文章的整理资料；未调用 AI。`);
    } catch (error) { if (serial === state.compareSerial) status("compare-status", error.message, true); }
    finally { state.compareBusy = false; updateScope(); }
  }

  function renderComparisonTable(values) {
    const root = $("comparison-table"); root.replaceChildren();
    if (!values.length) { root.append(node("p", "empty-state", "这组文章暂时没有可显示的对照资料。")); return; }
    const scroll = node("div", "comparison-table-scroll"); scroll.tabIndex = 0; scroll.setAttribute("aria-label", "文章逐项对照表，可横向滚动");
    const table = node("table", "comparison-table");
    const caption = node("caption", "sr-only", `逐项比较：${comparisonFields.map(([, label]) => label).join("、")}`);
    const head = node("thead"), header = node("tr"); header.append(node("th", "", "比较维度"));
    values.forEach((value) => {
      const cell = node("th"); cell.scope = "col";
      cell.append(node("strong", "", value.short_title || value.title));
      const link = sourceButton(value.id, 1, "阅读原文 ↗"); if (link) cell.append(link);
      header.append(cell);
    });
    head.append(header); const body = node("tbody");
    comparisonFields.forEach(([key, label]) => {
      const row = node("tr"), heading = node("th", "", label); heading.scope = "row"; row.append(heading);
      values.forEach((value) => {
        const detail = value.comparison?.[key], cell = node("td");
        cell.append(node("p", "", typeof detail === "string" ? detail : detail?.text || "尚未整理"));
        const links = node("div", "comparison-page-links");
        [...new Set(list(detail?.pages))].forEach((page) => { const link = sourceButton(value.id, page, `PDF 第 ${page} 页 ↗`); if (link) links.append(link); });
        if (links.childNodes.length) cell.append(links); row.append(cell);
      });
      body.append(row);
    });
    table.append(caption, head, body); scroll.append(table); root.append(scroll);
  }

  function renderRelations(root, values) {
    const relations = list(values);
    if (!relations.length) { root.append(node("p", "empty-state", "还没有登记文章关系。可从上方记录一条待验证的个人联想。")); return; }
    relations.forEach((relation) => {
      const card = node("article", "relation-card");
      const type = node("div", "relation-meta");
      type.append(node("span", "pill", relationTypes[relation.type] || "文章关联"), node("span", "muted", relation.origin === "personal" ? "个人联想 · 待验证" : relation.origin === "generated" ? "AI 整理 · 请核对原文" : "预先整理 · 可回到原文核对"));
      if (relation.is_current === false) type.append(node("span", "pill historical", "历史整理结果"));
      const pair = node("p", "relation-pair", `${articleName(relation.source)} ↔ ${articleName(relation.target)}`);
      card.append(type, pair, node("h3", "", relation.title || "文章之间的联系"));
      if (relation.explanation) card.append(node("p", "relation-explanation", relation.explanation));
      if (relation.boundary || relation.origin === "personal") card.append(node("p", "relation-boundary", relation.boundary || "这是一条个人联想，尚未由原文验证。"));
      renderCitations(card, relation.evidence); root.append(card);
    });
  }

  function relationSnapshot() {
    return {
      source: $("relation-source").value, target: $("relation-target").value,
      title: $("relation-title").value.trim(), explanation: $("relation-explanation").value.trim(),
      source_page: Number($("relation-source-page").value), target_page: Number($("relation-target-page").value),
    };
  }

  async function saveRelation(event) {
    event.preventDefault();
    if (state.relationSaving || !$("relation-form").reportValidity()) return;
    const body = relationSnapshot();
    if (body.source === body.target) { status("relation-status", "请为关系选择两篇不同的文章。", true); return; }
    if (!body.title || !body.explanation || !Number.isInteger(body.source_page) || !Number.isInteger(body.target_page)) { status("relation-status", "请填写联想、说明和两端的整数页码。", true); return; }
    state.relationSaving = true; $("relation-save").disabled = true; status("relation-status", "正在保存个人联想…");
    try {
      await api("/api/relation", body);
      await loadNetwork(true);
      if (JSON.stringify(relationSnapshot()) === JSON.stringify(body)) { $("relation-title").value = ""; $("relation-explanation").value = ""; }
      status("relation-status", "已保存为个人待验证联想，未修改科研知识库。");
    } catch (error) { status("relation-status", error.message, true); }
    finally { state.relationSaving = false; $("relation-save").disabled = false; }
  }

  async function submitComparison(event) {
    event.preventDefault();
    if (state.comparisonSaving) return;
    const ids = requireSelection(), question = $("comparison-question").value.trim();
    if (!ids || !question) return;
    const generate = event.submitter?.dataset.generate === "true";
    if (generate && !aiAvailable()) { status("comparison-submit-status", "智能比较暂不可用，可先保存比较问题。", true); return; }
    state.comparisonSaving = true; updateScope();
    status("comparison-submit-status", generate ? "正在提交智能比较…" : "正在保存比较问题…");
    try {
      await api("/api/comparison", { article_ids: ids, question, generate, topic_id: currentTopicId() });
      await loadNetwork(true);
      if ($("comparison-question").value.trim() === question && JSON.stringify(selectedIds()) === JSON.stringify(ids)) $("comparison-question").value = "";
      status("comparison-submit-status", generate ? "比较请求已保存，请在下方查看状态与原文引用。" : "比较问题已保存，尚未调用 AI。");
    } catch (error) { status("comparison-submit-status", error.message, true); }
    finally { state.comparisonSaving = false; updateScope(); }
  }

  async function retryComparison(record, button) {
    button.disabled = true;
    try { await api("/api/comparison/retry", { comparison_id: record.id }); await loadNetwork(true); }
    catch (error) { toast(error.message, true); button.disabled = !aiAvailable(); }
  }

  function renderComparisonHistory() {
    const root = $("comparison-history"); root.replaceChildren();
    const records = state.comparisons;
    if (!records.length) { root.append(node("p", "empty-state", "当前范围还没有比较记录。先选择文章，再写下想比较的问题。")); return; }
    [...records].sort((a, b) => text(b.created_at).localeCompare(text(a.created_at))).forEach((record) => {
      const card = node("article", "comparison-record");
      const meta = node("div", "comparison-record-meta");
      meta.append(node("span", `comparison-record-status ${record.status || "saved"}`, comparisonStatuses[record.status] || "状态待核对"));
      if (record.created_at) meta.append(node("span", "muted", text(record.created_at).replace("T", " ").slice(0, 19)));
      if (record.topic_name) meta.append(node("span", "pill", `创建时主题：${record.topic_name}`));
      if (record.origin === "study") meta.append(node("span", "pill", "学习内容整理 · AI 总结"));
      else meta.append(node("span", "pill", "个人比较问题"));
      if (record.is_current === false) meta.append(node("span", "pill historical", "历史整理结果"));
      card.append(meta, node("h3", "", record.question));
      card.append(node("p", "comparison-record-articles", list(record.article_ids).map(articleName).join(" · ")));
      if (record.status === "running") card.append(node("p", "field-help", "正在根据所选文章整理比较，可继续阅读其他文章。"));
      if (record.error) card.append(node("p", "network-status is-error", record.error));
      if (record.status === "completed" && record.answer) {
        card.append(node("p", "ai-comparison-notice", "AI 总结需核对原文"));
        [["summary", "综合回答"], ["agreements", "一致之处"], ["differences", "差异"], ["boundaries", "解释边界"], ["hypotheses", "待验证的联想"]].forEach(([key, label]) => {
          if (!record.answer[key]) return;
          const section = node("section", "comparison-answer-section"); section.append(node("h4", "", label), node("p", "", record.answer[key])); card.append(section);
        });
        renderCitations(card, record.answer.citations);
      }
      if (record.origin !== "study" && ["saved", "failed"].includes(record.status)) {
        const retry = action(record.status === "failed" ? "重新智能比较" : "智能比较这条问题", "button soft", () => retryComparison(record, retry));
        retry.disabled = !aiAvailable(); card.append(retry);
      }
      root.append(card);
    });
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (state.visible && state.comparisons.some((record) => record.status === "running")) state.pollTimer = setTimeout(() => loadNetwork(true), 2200);
  }

  async function loadNetwork(quiet = false) {
    const serial = ++state.networkSerial;
    clearTimeout(state.pollTimer);
    const params = updateNetworkScope();
    if (params !== state.networkKey) {
      state.relations = []; state.comparisons = []; renderNetwork(); state.networkKey = params;
    }
    if (!params) { status("network-status", "请先选择有效的记录范围。"); return; }
    if (!quiet) status("network-status", "正在读取所选范围的关系与问答…");
    try {
      const payload = await api(`/api/network?${params}`);
      if (serial !== state.networkSerial || params !== networkQuery()) return;
      state.relations = list(payload.relations); state.comparisons = list(payload.comparisons);
      renderNetwork(); schedulePoll(); status("network-status", "");
    } catch (error) { if (serial === state.networkSerial) status("network-status", `关系与问答暂时无法读取：${error.message}。可点击刷新重试。`, true); }
  }

  ["network-scope", "network-history"].forEach((id) => $(id).addEventListener("change", () => loadNetwork()));
  $("import-preview-form").addEventListener("submit", previewImport);
  $("import-commit-form").addEventListener("submit", commitImport);
  $("import-file").addEventListener("change", () => { state.preview = null; $("import-commit-form").hidden = true; status("import-status", ""); });
  $("build-compare").addEventListener("click", buildComparison);
  $("relation-form").addEventListener("submit", saveRelation);
  ["source", "target"].forEach((side) => $("relation-" + side).addEventListener("change", () => updateRelationPages(side, true)));
  $("relations-refresh").addEventListener("click", () => loadNetwork());
  $("comparison-form").addEventListener("submit", submitComparison);
  $("comparisons-refresh").addEventListener("click", () => loadNetwork());

  return {
    show() { state.visible = true; syncLibrary(); loadNetwork(); },
    hide() { state.visible = false; clearTimeout(state.pollTimer); },
    syncLibrary,
    selectedIds,
    async refreshGenerated() { if (state.visible) { await loadNetwork(); if ($("comparison-table").childNodes.length && selectedIds().length >= 2) await buildComparison(); } },
    syncScope(resetSelection = false) {
      if (resetSelection) { state.selected.clear(); $("network-scope").value = currentTopicId() ? "topic" : "all"; scopeChanged(); }
      syncLibrary();
      if (resetSelection) { state.selected.clear(); renderChoices(); }
      if (state.visible) loadNetwork();
    },
    showAssets,
  };
}

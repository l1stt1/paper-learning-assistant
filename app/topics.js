export function createTopicsManager({ api, getLibrary, canChange, onChange, refreshLibrary, afterImport, toast }) {
  const $ = (id) => document.getElementById(id);
  const list = (value) => Array.isArray(value) ? value : [];
  const node = (tag, className, value) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value != null) item.textContent = String(value);
    return item;
  };
  const button = (label, className, handler) => {
    const item = node("button", className, label); item.type = "button";
    item.addEventListener("click", handler); return item;
  };
  const state = { topics: [], currentId: "", cross: false, editor: null, saving: false, articleToAdd: "", addingArticle: false, sourceBusy: false, sourceSerial: 0, tagSerial: 0, preview: null, job: null, pollTimer: null, studyCrossKeys: new Set(), studyRequested: null, studyHandledId: "", studyOutcome: null };
  const topic = () => state.topics.find((item) => item.id === state.currentId) || null;
  const articles = () => list(getLibrary()?.articles);
  const articleName = (id) => { const item = articles().find((entry) => entry.id === id); return item?.short_title || item?.title || id; };
  const status = (id, message, error = false) => { $(id).textContent = message; $(id).hidden = !message; $(id).classList.toggle("is-error", error); };

  function importOptions(select, preferred = state.currentId) {
    select.replaceChildren();
    const none = node("option", "", "暂不分组"); none.value = ""; select.append(none);
    state.topics.forEach((entry) => { const option = node("option", "", entry.name); option.value = entry.id; select.append(option); });
    select.value = state.topics.some((entry) => entry.id === preferred) ? preferred : "";
  }

  function render() {
    const select = $("topic-select"), current = topic();
    const all = node("option", "", "全部文章"); all.value = ""; select.replaceChildren(all);
    state.topics.forEach((entry) => { const option = node("option", "", `${entry.name} · ${entry.article_ids.length} 篇`); option.value = entry.id; select.append(option); });
    select.value = state.currentId;
    $("topic-edit").disabled = !current;
    $("topic-study").disabled = !current || !current.article_ids.length;
    const scopeName = current?.name || "全部文章";
    $("brand-scope").textContent = scopeName;
    $("topic-context-title").textContent = scopeName;
    $("topic-context-notes").textContent = current?.notes || (current ? "按主题顺序阅读；可在侧栏“管理学习主题”中记录学习问题和调整顺序。" : "选择学习主题，把同一个问题下的文章放在一起阅读。");
    $("topic-source-label").textContent = current?.source ? `来源：${current.source.label || "Zotero"}` : current ? "本地自建主题" : "文章和学习记录按单篇复用";
    $("topic-source-check").hidden = !current?.source;
    $("topic-cross-toggle").hidden = !current;
    $("topic-cross-toggle").textContent = state.cross ? "返回主题内比较" : "跨主题选择文章";
    $("topic-cross-toggle").setAttribute("aria-pressed", String(state.cross));
    $("topic-scope-label").textContent = current && !state.cross ? `可选文章范围：${current.name}` : "可选文章范围：全部文章";
    ["import-topic", "zotero-import-topic"].forEach((id) => importOptions($(id)));
  }

  async function refresh(initialId) {
    const payload = await api("/api/topics");
    state.topics = list(payload.topics);
    if (initialId !== undefined) state.currentId = initialId;
    if (!state.topics.some((item) => item.id === state.currentId)) state.currentId = "";
    render(); return state.topics;
  }

  async function select(id, { force = false } = {}) {
    const target = state.topics.find((item) => item.id === id);
    id = target?.id || "";
    if (!force && id === state.currentId) return true;
    if (canChange && !canChange()) { $("topic-select").value = state.currentId; return false; }
    state.currentId = id; state.cross = false; render();
    await onChange({ resetSelection: true });
    return true;
  }

  function visibleArticles({ comparison = false } = {}) {
    const current = topic();
    if (!current || (comparison && state.cross)) return articles();
    const byId = new Map(articles().map((item) => [item.id, item]));
    return current.article_ids.map((id) => byId.get(id)).filter(Boolean);
  }

  function renderEditor() {
    const editor = state.editor;
    const root = $("topic-members"); root.replaceChildren();
    if (!editor.article_ids.length) root.append(node("p", "field-help", "主题里还没有文章，可从下方加入。"));
    editor.article_ids.forEach((id, index) => {
      const row = node("li", "topic-member");
      row.append(node("span", "topic-member-title", `${index + 1}. ${articleName(id)}`));
      const actions = node("div", "topic-member-actions");
      const move = (offset) => { const next = index + offset; [editor.article_ids[index], editor.article_ids[next]] = [editor.article_ids[next], editor.article_ids[index]]; renderEditor(); };
      const up = button("↑", "icon-button", () => move(-1)); up.disabled = index === 0 || state.saving; up.setAttribute("aria-label", `上移 ${articleName(id)}`);
      const down = button("↓", "icon-button", () => move(1)); down.disabled = index === editor.article_ids.length - 1 || state.saving; down.setAttribute("aria-label", `下移 ${articleName(id)}`);
      const remove = button("移出主题", "text-button", () => { editor.article_ids = editor.article_ids.filter((value) => value !== id); renderEditor(); }); remove.disabled = state.saving;
      actions.append(up, down, remove); row.append(actions); root.append(row);
    });
    const available = $("topic-available"); available.replaceChildren();
    const search = $("topic-article-search").value.trim().toLocaleLowerCase();
    const choices = articles().filter((item) => !editor.article_ids.includes(item.id) && (!search || `${item.title} ${item.short_title || ""}`.toLocaleLowerCase().includes(search)));
    choices.forEach((item) => {
      const row = node("div", "topic-add-row"); row.append(node("span", "", item.short_title || item.title));
      const add = button("加入", "button subtle", () => { editor.article_ids.push(item.id); renderEditor(); }); add.disabled = state.saving;
      row.append(add); available.append(row);
    });
    if (!choices.length) available.append(node("p", "field-help", search ? "没有匹配的其他文章。" : "其他文章均已加入；也可以从 Zotero 来源添加文章。"));
  }

  function openEditor(isNew = false) {
    const current = isNew ? null : topic();
    if (!isNew && !current) return;
    state.editor = current ? { id: current.id, expected_updated_at: current.updated_at, article_ids: [...current.article_ids] } : { article_ids: [] };
    $("topic-editor-title").textContent = current ? "编辑学习主题" : "新建学习主题";
    $("topic-name").value = current?.name || ""; $("topic-notes").value = current?.notes || ""; $("topic-article-search").value = "";
    status("topic-editor-status", ""); renderEditor(); $("topic-editor-dialog").showModal();
  }

  async function saveEditor(event) {
    event.preventDefault();
    if (state.saving || !state.editor || !$("topic-editor-form").reportValidity()) return;
    state.saving = true; $("topic-save").disabled = true; $("topic-editor-close").disabled = true; renderEditor();
    ["topic-name", "topic-notes", "topic-article-search"].forEach((id) => { $(id).disabled = true; });
    status("topic-editor-status", "正在保存主题…");
    try {
      const payload = await api("/api/topic", { ...state.editor, name: $("topic-name").value.trim(), notes: $("topic-notes").value.trim() });
      await refresh(); $("topic-editor-dialog").close();
      await select(payload.topic.id, { force: true }); toast("主题已保存；文章和单篇学习记录继续复用。");
    } catch (error) { status("topic-editor-status", error.message, true); }
    finally { state.saving = false; $("topic-save").disabled = false; $("topic-editor-close").disabled = false; ["topic-name", "topic-notes", "topic-article-search"].forEach((id) => { $(id).disabled = false; }); if ($("topic-editor-dialog").open) renderEditor(); }
  }

  async function addArticle(articleId, topicId) {
    if (!topicId) return;
    await api("/api/topic/add", { topic_id: topicId, article_ids: [articleId] });
    await refresh();
  }

  function chooseArticleTopic(articleId) {
    state.articleToAdd = articleId;
    importOptions($("topic-article-target"));
    $("topic-article-target").options[0].textContent = "选择一个学习主题";
    $("topic-article-name").textContent = articleName(articleId);
    status("topic-article-status", state.topics.length ? "" : "请先通过侧栏新建学习主题。");
    $("topic-article-save").disabled = !state.topics.length;
    $("topic-article-dialog").showModal();
  }

  async function saveArticleTopic() {
    const topicId = $("topic-article-target").value;
    if (!topicId || state.addingArticle) { if (!topicId) status("topic-article-status", "请先选择一个学习主题。", true); return; }
    state.addingArticle = true; $("topic-article-save").disabled = true; $("topic-article-close").disabled = true;
    try {
      await addArticle(state.articleToAdd, topicId); await onChange({});
      $("topic-article-dialog").close(); toast("文章已加入主题，原有学习记录继续保留。");
    } catch (error) { status("topic-article-status", error.message, true); }
    finally { state.addingArticle = false; $("topic-article-save").disabled = false; $("topic-article-close").disabled = false; }
  }

  function sourceBusy(busy) {
    state.sourceBusy = busy;
    $("topic-source-form").querySelectorAll("input, select, button").forEach((control) => { control.disabled = busy; });
    $("topic-source-confirm").disabled = busy || !state.preview;
    $("topic-source-close").disabled = busy && !state.job;
    ["topic-preview-all", "topic-preview-none", "topic-study-after"].forEach((id) => { $(id).disabled = busy; });
    $("topic-study-cross").disabled = busy || !$("topic-study-after").checked;
  }

  function invalidatePreview() {
    if (state.job?.status !== "running") { state.job = null; $("topic-source-job").hidden = true; }
    state.preview = null; $("topic-source-preview").hidden = true; $("topic-source-confirm").disabled = true;
    status("topic-source-status", "来源设置已改变，请重新预览。");
  }

  function sourceFields() {
    const tagged = $("topic-source-kind").value === "tag";
    $("topic-source-tag-field").hidden = !tagged;
    $("topic-source-collection").required = !tagged;
    $("topic-source-tag").required = tagged;
    $("topic-source-children-field").hidden = tagged;
    $("topic-source-name-field").hidden = Boolean($("topic-source-target").value);
    $("topic-source-name").required = false;
  }

  async function loadTags(preferred = "") {
    const serial = ++state.tagSerial;
    const select = $("topic-source-tag"); select.replaceChildren(node("option", "", "正在读取标签…")); select.disabled = true;
    try {
      const payload = await api(`/api/zotero/tags?collection=${encodeURIComponent($("topic-source-collection").value)}`);
      if (serial !== state.tagSerial) return;
      const blank = node("option", "", "选择标签"); blank.value = ""; select.replaceChildren(blank);
      list(payload.tags).forEach((tag) => { const option = node("option", "", tag); option.value = tag; select.append(option); });
      if (preferred && list(payload.tags).includes(preferred)) select.value = preferred;
      if (preferred && !list(payload.tags).includes(preferred)) status("topic-source-status", "原绑定标签目前不在可用来源中，请核对 Zotero 后重新选择。", true);
    } catch (error) { if (serial === state.tagSerial) status("topic-source-status", error.message, true); }
    finally { if (serial === state.tagSerial) select.disabled = state.sourceBusy; }
  }

  async function openSource({ bound = false, collectionKey = "" } = {}) {
    if (state.job?.status === "running") { $("topic-source-dialog").showModal(); return; }
    const serial = ++state.sourceSerial;
    const current = topic(); const source = bound ? current?.source : null;
    state.preview = null; state.job = null;
    state.studyRequested = null; state.studyHandledId = ""; state.studyCrossKeys.clear();
    state.studyOutcome = null;
    $("topic-study-after").checked = false; $("topic-study-cross").checked = false; $("topic-study-cross").disabled = true;
    $("topic-study-cross-choices").hidden = true;
    clearTimeout(state.pollTimer);
    $("topic-source-preview").hidden = true; $("topic-source-job").hidden = true;
    importOptions($("topic-source-target")); $("topic-source-target").options[0].textContent = "建立新主题";
    $("topic-source-kind").value = source?.kind || "collection";
    $("topic-source-name").value = ""; $("topic-source-children").checked = Boolean(source?.include_children);
    const placeholder = node("option", "", "正在读取分类…"); placeholder.value = "";
    $("topic-source-collection").replaceChildren(placeholder);
    sourceFields(); sourceBusy(true); status("topic-source-status", "正在连接 Zotero 并读取来源…");
    $("topic-source-dialog").showModal();
    try {
      const payload = await api("/api/zotero/collections");
      if (serial !== state.sourceSerial) return;
      const all = node("option", "", "全部文献（按标签筛选时可选）"); all.value = "";
      $("topic-source-collection").replaceChildren(all);
      list(payload.collections).forEach((entry) => { const option = node("option", "", entry.path || entry.name); option.value = entry.key; $("topic-source-collection").append(option); });
      $("topic-source-collection").value = source?.collection_key || collectionKey || "";
      if ((source?.collection_key || collectionKey) && !$("topic-source-collection").value) throw new Error("所选 Zotero 分类当前不可用，请核对分类后重新选择。已加入的文章继续保留。");
      if ($("topic-source-kind").value === "tag") await loadTags(source?.tag || "");
      status("topic-source-status", "选择来源后预览。这里只读取书目与附件信息，不生成导读或调用 AI。");
    } catch (error) { if (serial === state.sourceSerial) status("topic-source-status", `${error.message} 已加入主题的文章仍可离线阅读。`, true); }
    finally { if (serial === state.sourceSerial) { sourceBusy(false); sourceFields(); } }
  }

  function selectedRows() {
    if (!state.preview) return [];
    return [...$("topic-preview-items").querySelectorAll("[data-topic-item]")].filter((row) => row.querySelector("input[type=checkbox]").checked).map((row) => ({ item_key: row.dataset.topicItem, ...(row.querySelector("select")?.value ? { attachment_key: row.querySelector("select").value } : {}) }));
  }

  function updatePreviewCount() {
    const count = selectedRows().length;
    $("topic-preview-count").textContent = `已选择 ${count} / ${list(state.preview?.items).length} 篇`;
    $("topic-source-confirm").disabled = state.sourceBusy || !state.preview;
    $("topic-source-confirm").textContent = count ? `确认来源并加入 ${count} 篇` : "确认绑定来源（不加入文章）";
    updateBatchStudyChoices();
  }

  function updateBatchStudyChoices() {
    const enabled = $("topic-study-after").checked;
    $("topic-study-cross").disabled = !enabled || state.sourceBusy;
    if (!enabled) { $("topic-study-cross").checked = false; state.studyCrossKeys.clear(); }
    const root = $("topic-study-cross-choices"); root.hidden = !$("topic-study-cross").checked; root.replaceChildren();
    const selected = selectedRows(), valid = new Set(selected.map((item) => item.item_key));
    state.studyCrossKeys = new Set([...state.studyCrossKeys].filter((key) => valid.has(key)));
    if (root.hidden) return;
    root.append(node("p", "field-help", `已选跨文范围 ${state.studyCrossKeys.size} 篇；仅从本次勾选导入的文章中选择 2–5 篇。`));
    selected.forEach((item) => {
      const entry = state.preview.items.find((row) => row.item_key === item.item_key);
      const row = node("label", "study-cross-choice"), check = node("input"); check.type = "checkbox"; check.checked = state.studyCrossKeys.has(item.item_key);
      check.addEventListener("change", () => {
        if (check.checked && state.studyCrossKeys.size >= 5) { check.checked = false; toast("跨文整理一次最多选择 5 篇文章。"); return; }
        if (check.checked) state.studyCrossKeys.add(item.item_key); else state.studyCrossKeys.delete(item.item_key); updateBatchStudyChoices();
      });
      row.append(check, node("span", "", entry?.title || item.item_key)); root.append(row);
    });
  }

  function renderPreview() {
    const preview = state.preview; const root = $("topic-preview-items"); root.replaceChildren();
    const labels = { existing: "复用已有文章", ready: "可导入", choose_pdf: "请选择正文 PDF", missing_pdf: "缺少可用 PDF", conflict: "需要单独核对" };
    list(preview.items).forEach((entry) => {
      const row = node("div", "topic-preview-item"); row.dataset.topicItem = entry.item_key;
      const choice = node("label", "topic-preview-choice"); const check = node("input"); check.type = "checkbox";
      check.disabled = !["existing", "ready", "choose_pdf"].includes(entry.status);
      check.checked = ["existing", "ready"].includes(entry.status);
      const alreadyMember = state.topics.find((item) => item.id === preview.topic_id)?.article_ids.includes(entry.article_id);
      const title = node("span"); title.append(node("strong", "", entry.title || "未命名文献"), node("small", "", [alreadyMember ? "已在主题中 · 复用已有记录" : labels[entry.status] || entry.status, entry.date, entry.doi ? `DOI ${entry.doi}` : ""].filter(Boolean).join(" · ")));
      choice.append(check, title); row.append(choice);
      if (entry.message) row.append(node("p", "field-help", entry.message));
      if (entry.status === "choose_pdf" || (entry.status === "ready" && list(entry.pdfs).length)) {
        const select = node("select"); select.setAttribute("aria-label", `为 ${entry.title} 选择正文 PDF`);
        const empty = node("option", "", "请选择用于阅读的正文 PDF"); empty.value = ""; select.append(empty);
        list(entry.pdfs).forEach((pdf) => { const option = node("option", "", [pdf.title, pdf.filename].filter(Boolean).join(" · ") || pdf.key); option.value = pdf.key; select.append(option); });
        select.value = entry.selected_attachment_key || (entry.status === "ready" ? entry.pdfs[0]?.key || "" : "");
        select.addEventListener("change", () => { check.checked = Boolean(select.value); updatePreviewCount(); }); row.append(select);
      }
      check.addEventListener("change", updatePreviewCount); root.append(row);
    });
    if (!list(preview.items).length) root.append(node("p", "field-help", "此来源当前没有文章。可以保留空主题，之后再查看来源变化。"));
    const changes = $("topic-preview-changes"); changes.replaceChildren();
    list(preview.warnings).forEach((warning) => changes.append(node("p", "network-status", warning)));
    if (list(preview.removed).length) {
      changes.append(node("h4", "", "已不在 Zotero 来源中 · 主题内继续保留"));
      list(preview.removed).forEach((entry) => changes.append(node("p", "field-help", entry.title || articleName(entry.article_id))));
    }
    if (list(preview.changed).length) {
      changes.append(node("h4", "", "来源信息变化 · 请核对"));
      list(preview.changed).forEach((entry) => changes.append(node("p", "field-help", `${entry.title || articleName(entry.article_id)}：${entry.message || "来源信息已变化，已有阅读版与记录继续保留。"}`)));
    }
    $("topic-source-preview").hidden = false; updatePreviewCount();
  }

  async function previewSource(event) {
    event.preventDefault();
    if (state.sourceBusy) return;
    state.job = null; $("topic-source-job").hidden = true;
    state.studyRequested = null; state.studyHandledId = ""; state.studyOutcome = null; state.studyCrossKeys.clear();
    $("topic-study-after").checked = false; $("topic-study-cross").checked = false;
    const kind = $("topic-source-kind").value, collection = $("topic-source-collection").value;
    if (kind === "collection" && !collection) { status("topic-source-status", "请选择一个 Zotero 分类。", true); return; }
    if (kind === "tag" && !$("topic-source-tag").value) { status("topic-source-status", "请选择一个 Zotero 标签。", true); return; }
    sourceBusy(true); status("topic-source-status", "正在核对来源中的文章与本机资料库…");
    try {
      state.preview = await api("/api/zotero/topic-preview", { ...($("topic-source-target").value ? { topic_id: $("topic-source-target").value } : {}), source: { kind, ...(collection ? { collection_key: collection } : {}), ...(kind === "tag" ? { tag: $("topic-source-tag").value } : { include_children: $("topic-source-children").checked }) } });
      if (!$("topic-source-name").value.trim()) $("topic-source-name").value = state.preview.name_suggestion || "";
      renderPreview(); status("topic-source-status", "预览完成。确认后只处理勾选文章；已有文章复用，缺件继续保留提示。");
    } catch (error) { state.preview = null; $("topic-source-preview").hidden = true; status("topic-source-status", error.message, true); }
    finally { sourceBusy(false); if (state.preview) updatePreviewCount(); }
  }

  function renderJob() {
    const job = state.job; if (!job) return;
    const root = $("topic-source-job"); root.hidden = false; root.replaceChildren();
    root.append(node("h3", "", job.status === "running" ? "正在加入主题" : job.status === "completed" ? "主题更新完成" : "主题更新未完成"));
    root.append(node("p", "", `已处理 ${job.processed || 0} / ${job.total || 0} 篇 · 新导入 ${job.added_count || 0} 篇 · 复用 ${job.reused_count || 0} 篇`));
    if (job.error) root.append(node("p", "network-status is-error", job.error));
    list(job.failed).forEach((entry) => root.append(node("p", "network-status is-error", `${entry.title || entry.item_key}：${entry.error}`)));
    if (job.status !== "running") root.append(node("p", "field-help", list(job.failed).length ? "已成功加入的文章保留。处理缺件或冲突后，可重新查看来源并选择未成功的文章。" : "文章和原有单篇学习记录已复用；学习内容整理的状态单独显示。"));
  }

  async function finishJob() {
    renderJob();
    if (state.job?.status === "running") { state.pollTimer = setTimeout(pollJob, 1800); return; }
    await refreshLibrary();
    if (state.job?.topic_id) await select(state.job.topic_id, { force: true });
    sourceBusy(false); $("topic-source-confirm").disabled = true;
    status("topic-source-status", state.studyOutcome?.message || (state.studyRequested?.enabled ? "导入处理已结束，正在启动所选学习内容整理…" : "处理已结束。请查看逐篇结果；导入未生成导读或调用 AI。"), state.studyOutcome?.error || state.job?.status === "failed");
    if (state.studyRequested?.enabled && state.studyHandledId !== state.job.id) {
      state.studyHandledId = state.job.id;
      const results = list(state.job.results);
      const ids = [...new Set(results.map((entry) => entry.article_id).filter(Boolean))];
      let crossIds = [...new Set(results.filter((entry) => state.studyRequested.crossKeys.includes(entry.item_key)).map((entry) => entry.article_id))];
      const crossSkipped = state.studyRequested.crossKeys.length && crossIds.length < 2;
      if (crossSkipped) crossIds = [];
      if (ids.length) {
        const result = await afterImport({ articleIds: ids, topicId: state.job.topic_id || "", crossArticleIds: crossIds });
        state.studyOutcome = { message: result.error || `成功文章已加入，并已开始整理学习内容。${crossSkipped ? "跨文范围成功加入不足 2 篇，本次仅整理单篇材料。" : "可查看学习内容整理进度。"}`, error: Boolean(result.error) };
      } else state.studyOutcome = { message: "没有成功加入的文章，学习内容整理未启动。请先处理导入问题。", error: true };
      status("topic-source-status", state.studyOutcome.message, state.studyOutcome.error);
    }
  }

  async function pollJob() {
    clearTimeout(state.pollTimer);
    if (!state.job) return;
    try { const payload = await api(`/api/zotero/topic-job?id=${encodeURIComponent(state.job.id)}`); state.job = payload.job; await finishJob(); }
    catch (error) {
      status("topic-source-status", `暂时无法读取进度：${error.message}。可点击“刷新导入进度”，不要重复提交。`, true);
      $("topic-job-refresh").hidden = false;
    }
  }

  async function confirmSource() {
    if (state.sourceBusy || !state.preview || state.job) return;
    const selections = selectedRows();
    const missingChoice = selections.some((choice) => state.preview.items.find((entry) => entry.item_key === choice.item_key)?.status === "choose_pdf" && !choice.attachment_key);
    if (missingChoice) { status("topic-source-status", "请为已勾选且有多个附件的文章选择正文 PDF。", true); return; }
    const name = $("topic-source-name").value.trim();
    if (!state.preview.topic_id && !name) { status("topic-source-status", "请填写新主题名称。", true); $("topic-source-name").focus(); return; }
    if ($("topic-study-cross").checked && (state.studyCrossKeys.size < 2 || state.studyCrossKeys.size > 5)) { status("topic-source-status", "请为跨文整理选择 2–5 篇文章，或关闭跨文整理。", true); return; }
    state.studyRequested = { enabled: $("topic-study-after").checked, crossKeys: $("topic-study-cross").checked ? [...state.studyCrossKeys] : [] };
    sourceBusy(true); status("topic-source-status", "正在提交所选文章…"); $("topic-job-refresh").hidden = true;
    try {
      const payload = await api("/api/zotero/topic-import", { preview_id: state.preview.preview_id, ...(name ? { name } : {}), selections });
      state.job = payload.job; $("topic-source-close").disabled = false;
      $("topic-source-preview").querySelectorAll("input, select, button").forEach((control) => { control.disabled = true; });
      await finishJob();
    } catch (error) {
      if (state.job) {
        $("topic-job-refresh").hidden = false;
        status("topic-source-status", `任务已提交，界面暂未更新：${error.message}。请刷新导入进度。`, true);
      } else { sourceBusy(false); status("topic-source-status", error.message, true); }
    }
  }

  $("topic-select").addEventListener("change", () => select($("topic-select").value).catch((error) => toast(error.message, true)));
  $("topic-new").addEventListener("click", () => openEditor(true));
  $("topic-edit").addEventListener("click", () => openEditor());
  $("topic-editor-form").addEventListener("submit", saveEditor);
  $("topic-editor-close").addEventListener("click", () => $("topic-editor-dialog").close());
  $("topic-editor-dialog").addEventListener("cancel", (event) => { if (state.saving) event.preventDefault(); });
  $("topic-article-close").addEventListener("click", () => $("topic-article-dialog").close());
  $("topic-article-dialog").addEventListener("cancel", (event) => { if (state.addingArticle) event.preventDefault(); });
  $("topic-article-save").addEventListener("click", saveArticleTopic);
  $("topic-article-search").addEventListener("input", renderEditor);
  $("topic-cross-toggle").addEventListener("click", async () => { state.cross = !state.cross; render(); try { await onChange({ resetSelection: true, scopeOnly: true }); } catch (error) { toast(error.message, true); } });
  $("topic-source-check").addEventListener("click", () => openSource({ bound: true }));
  $("zotero-topic-open").addEventListener("click", () => openSource({ collectionKey: $("zotero-collection").value }));
  $("topic-source-form").addEventListener("submit", previewSource);
  $("topic-source-confirm").addEventListener("click", confirmSource);
  $("topic-source-close").addEventListener("click", () => $("topic-source-dialog").close());
  $("topic-source-dialog").addEventListener("cancel", (event) => { if (state.sourceBusy && !state.job) event.preventDefault(); });
  ["topic-source-target", "topic-source-kind", "topic-source-collection", "topic-source-tag", "topic-source-children"].forEach((id) => $(id).addEventListener("change", async () => {
    invalidatePreview(); sourceFields();
    if (["topic-source-kind", "topic-source-collection"].includes(id) && $("topic-source-kind").value === "tag") await loadTags();
  }));
  $("topic-preview-all").addEventListener("click", () => { $("topic-preview-items").querySelectorAll("input[type=checkbox]:not(:disabled)").forEach((check) => { check.checked = true; }); updatePreviewCount(); });
  $("topic-preview-none").addEventListener("click", () => { $("topic-preview-items").querySelectorAll("input[type=checkbox]").forEach((check) => { check.checked = false; }); updatePreviewCount(); });
  $("topic-study-after").addEventListener("change", updateBatchStudyChoices);
  $("topic-study-cross").addEventListener("change", () => { const selected = selectedRows(); if ($("topic-study-cross").checked && selected.length >= 2 && selected.length <= 5) state.studyCrossKeys = new Set(selected.map((item) => item.item_key)); updateBatchStudyChoices(); });
  $("topic-job-refresh").addEventListener("click", () => { $("topic-job-refresh").hidden = true; pollJob(); });

  return { refresh, select, topic, render, visibleArticles, addArticle, importOptions, openSource, chooseArticleTopic,
    scope: () => ({ topic: topic(), cross: state.cross }),
    async ensureArticleVisible(id) { if (topic() && !topic().article_ids.includes(id)) { await select(""); toast("已切换到全部文章，打开这篇跨主题文献。"); } },
  };
}

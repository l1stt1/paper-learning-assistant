export function createStudyManager({ api, getArticleId, getLibrary, getScope, getComparisonSelection, onPublished, toast }) {
  const $ = (id) => document.getElementById(id);
  const list = (value) => Array.isArray(value) ? value : [];
  const text = (value) => value == null ? "" : String(value);
  const node = (tag, className, value) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value != null) item.textContent = text(value);
    return item;
  };
  const action = (label, className, handler) => { const item = node("button", className, label); item.type = "button"; item.addEventListener("click", handler); return item; };
  const moduleNames = { sources: "原文与图表", guide: "导读", concepts: "概念", comparison: "逐项对照", relations: "文章关系", summary: "跨文总结" };
  const articleModules = ["sources", "guide", "concepts", "comparison"];
  const moduleLabels = { pending: "待整理", running: "整理中", completed: "已整理", failed: "未完成", existing: "已有内容" };
  const jobLabels = { queued: "等待开始", running: "正在整理", completed: "已完成整理", partial: "部分完成", failed: "整理未完成", stopped: "已停止后续处理", interrupted: "已中断，可继续" };
  const state = { articles: [], jobs: [], aiAvailable: false, preview: null, topicId: "", selected: new Set(), cross: new Set(), materialChoices: new Map(), busy: false, actionJob: "", serial: 0, refreshSerial: 0, pollTimer: null, loaded: false, published: "", content: null };
  const articleName = (id) => list(getLibrary()?.articles).find((item) => item.id === id)?.short_title || list(getLibrary()?.articles).find((item) => item.id === id)?.title || id;
  const runningJobs = () => state.jobs.filter((job) => ["queued", "running"].includes(job.status));
  const completedModule = (value) => ["completed", "existing"].includes(value?.status);
  const hasMissing = (entry) => articleModules.some((key) => !completedModule(entry.modules?.[key]));
  const status = (message, error = false) => { $("study-status").textContent = message; $("study-status").hidden = !message; $("study-status").classList.toggle("is-error", error); };

  function moduleRow(modules, keys = articleModules) {
    const root = node("div", "study-modules");
    keys.forEach((key) => {
      const value = modules?.[key] || { status: "pending" };
      const item = node("div", `study-module ${value.status || "pending"}`);
      item.append(node("strong", "", moduleNames[key]), node("span", "", moduleLabels[value.status] || "待整理"));
      if (value.message) item.append(node("small", "", value.message));
      root.append(item);
    });
    return root;
  }

  function renderArticleStatus() {
    const root = $("study-article-status"); root.replaceChildren();
    const entry = state.articles.find((item) => item.article_id === getArticleId());
    const content = state.content || {};
    if (!entry && !content.study_review) { root.hidden = true; return; }
    root.hidden = false;
    if (entry) root.append(moduleRow(entry.modules));
    const reviewMessage = content.study_review?.message || content.study_review?.label;
    if (reviewMessage) root.append(node("p", "study-review-note", reviewMessage));
    const coverage = content.source_coverage;
    const coverageFields = [["main_text", "正文阅读"], ["figures", "图表阅读"], ["supplementary", "补充材料"], ["missing", "尚未覆盖"], ["notes", "覆盖说明"]];
    if (coverage && (list(coverage.materials).length || list(coverage.unread_materials).length || coverage.note || coverageFields.some(([key]) => list(coverage[key]).length))) {
      const details = node("details", "study-coverage"); details.append(node("summary", "", "实际覆盖材料与核查说明"));
      coverageFields.forEach(([key, label]) => list(coverage[key]).forEach((description) => { if (typeof description === "string") details.append(node("p", "field-help", `${label}：${description}`)); }));
      list(coverage.materials).forEach((material) => {
        const read = new Set(list(material.read_pages)).size, images = new Set(list(material.image_pages)).size;
        const counts = [`已读取 ${read}${material.pages ? ` / ${material.pages}` : ""} 页`, `已查看 ${images} 页图像`];
        details.append(node("p", "field-help", `${material.label || "阅读材料"} · ${counts.join(" · ")}`));
      });
      list(coverage.unread_materials).forEach((material) => details.append(node("p", "field-help", `未覆盖：${material.label || "附件"}${material.reason ? ` · ${material.reason}` : ""}`)));
      if (coverage.note) details.append(node("p", "field-help", coverage.note));
      if (content.study_review) details.append(node("p", "field-help", "页码与摘录位置检查不等同于人工科学审阅；请结合原文核对解释。"));
      root.append(details);
    }
  }

  function renderBanner() {
    const active = runningJobs()[0], latest = state.jobs[0];
    const job = active || latest;
    $("study-progress").hidden = !job;
    if (!job) return;
    const ids = list(job.article_ids);
    const scope = job.topic_name || (ids.length === 1 ? articleName(ids[0]) : "所选文章");
    const count = ids.length > 1 ? `（${ids.length} 篇）` : "";
    const current = [job.current_article_id ? articleName(job.current_article_id) : "", moduleNames[job.current_module]].filter(Boolean).join(" · ");
    $("study-progress-text").textContent = [active ? "正在处理" : "最近一次整理", scope + count, jobLabels[job.status], active && current, job.stop_requested && "已请求停止后续"].filter(Boolean).join(" · ");
    $("study-progress-open").textContent = active ? "查看整理进度" : "查看整理记录";
  }

  function updateStart() {
    const count = state.selected.size;
    const crossEnabled = $("study-cross-enable").checked;
    const crossValid = !crossEnabled || state.cross.size >= 2 && state.cross.size <= 5;
    $("study-selection-count").textContent = `整理单篇材料：${count} 篇${crossEnabled ? ` · 跨文范围：${state.cross.size} 篇` : ""}`;
    $("study-cross-choices").hidden = !crossEnabled;
    $("study-start").disabled = state.busy || !state.preview || !state.aiAvailable || runningJobs().length > 0 || !(count || crossEnabled && state.cross.size) || !crossValid;
    $("study-start").textContent = $("study-regenerate").checked ? "重新整理所选学习内容" : "开始整理缺失内容";
    $("study-start-note").textContent = runningJobs().length ? "已有整理任务正在运行；可先查看进度，完成或停止后再开始新的整理。" : !state.aiAvailable ? "当前智能整理不可用；仍可查看已有学习材料。" : crossEnabled && !crossValid ? "请为文章关系与跨文总结明确选择 2–5 篇文章。" : $("study-regenerate").checked ? "将重整所选文章的已有学习内容；原章节/概念身份及个人学习记录保留。" : "成功模块保留，已有完整内容自动复用；新内容作为 AI 整理稿加载。";
  }

  function renderPreview() {
    const root = $("study-articles"); root.replaceChildren();
    list(state.preview?.articles).forEach((entry) => {
      const card = node("section", "study-article-choice");
      const label = node("label", "study-choice-title"), check = node("input"); check.type = "checkbox"; check.checked = state.selected.has(entry.article_id);
      const title = node("span"); title.append(node("strong", "", entry.title || articleName(entry.article_id)), node("small", "", hasMissing(entry) ? "有待整理模块" : "已有完整学习内容，默认复用"));
      label.append(check, title); card.append(label, moduleRow(entry.modules));
      check.addEventListener("change", () => { if (check.checked) state.selected.add(entry.article_id); else state.selected.delete(entry.article_id); renderPreview(); });
      const details = node("details", "study-materials"); details.open = state.selected.has(entry.article_id);
      details.append(node("summary", "", "模型读取材料（默认仅主阅读 PDF）"));
      list(entry.materials).forEach((material) => {
        const row = node("label", "study-material-choice"), choice = node("input"); choice.type = "checkbox";
        choice.checked = state.materialChoices.get(entry.article_id)?.has(material.id) || false;
        choice.disabled = material.id === "reading" || material.kind !== "pdf" || !material.available || !state.selected.has(entry.article_id);
        const description = node("span");
        description.append(node("strong", "", material.label || "阅读材料"), node("small", "", [material.id === "reading" ? "主阅读 PDF · 必选" : material.kind !== "pdf" ? "非 PDF，当前不解析" : "可选 PDF · 请自行确认正文或补充材料身份", material.pages ? `${material.pages} 页` : "", !material.available ? "当前不可用" : ""].filter(Boolean).join(" · ")));
        choice.addEventListener("change", () => { const selected = state.materialChoices.get(entry.article_id); if (choice.checked) selected.add(material.id); else selected.delete(material.id); });
        row.append(choice, description); details.append(row);
      });
      list(entry.warnings).forEach((warning) => details.append(node("p", "field-help", warning)));
      card.append(details); root.append(card);
    });
    const crossRoot = $("study-cross-choices"); crossRoot.replaceChildren();
    list(state.preview?.articles).forEach((entry) => {
      const row = node("label", "study-cross-choice"), check = node("input"); check.type = "checkbox"; check.checked = state.cross.has(entry.article_id);
      check.addEventListener("change", () => {
        if (check.checked && state.cross.size >= 5) { check.checked = false; toast("跨文整理一次最多选择 5 篇文章。"); return; }
        if (check.checked) state.cross.add(entry.article_id); else state.cross.delete(entry.article_id); updateStart();
      });
      row.append(check, node("span", "", entry.title || articleName(entry.article_id))); crossRoot.append(row);
    });
    $("study-cross-section").hidden = list(state.preview?.articles).length < 2;
    updateStart();
  }

  async function open({ articleIds, topicId = "", theme = false } = {}) {
    if (state.busy) { toast("正在提交整理请求，请稍后查看整理进度。"); return; }
    const serial = ++state.serial;
    const ids = [...new Set(list(articleIds).filter(Boolean))];
    if (!ids.length) { toast("当前范围尚无文章，请先加入文章。"); return; }
    state.preview = null; state.topicId = topicId; state.selected.clear(); state.cross.clear(); state.materialChoices.clear();
    $("study-dialog-title").textContent = theme ? "整理主题学习内容" : "整理这篇文章的学习内容";
    $("study-setup").hidden = false; $("study-regenerate").checked = false; $("study-cross-enable").checked = false;
    $("study-articles").replaceChildren(node("p", "field-help", "正在核对学习模块和可用材料…"));
    $("study-cross-section").hidden = true; $("study-start").disabled = true; status("");
    if (!$("study-dialog").open) $("study-dialog").showModal();
    try {
      const payload = await api("/api/study/preview", { article_ids: ids, ...(topicId ? { topic_id: topicId } : {}) });
      if (serial !== state.serial) return;
      state.preview = payload; state.aiAvailable = Boolean(payload.ai_available);
      const currentSelection = list(getComparisonSelection()).filter((id) => ids.includes(id));
      if (currentSelection.length >= 2 && currentSelection.length <= 5) state.cross = new Set(currentSelection);
      list(payload.articles).forEach((entry) => {
        if (!theme || hasMissing(entry)) state.selected.add(entry.article_id);
        state.materialChoices.set(entry.article_id, new Set(list(entry.materials).filter((material) => material.id === "reading").map((material) => material.id)));
      });
      renderPreview(); await refresh();
    } catch (error) { if (serial === state.serial) status(error.message, true); }
  }

  function renderJobs() {
    const root = $("study-jobs"); root.replaceChildren();
    if (!state.jobs.length) { root.append(node("p", "field-help", "还没有学习内容整理记录。")); return; }
    state.jobs.forEach((job) => {
      const card = node("section", `study-job ${job.status || ""}`);
      const heading = node("div", "network-subheading"); heading.append(node("h3", "", jobLabels[job.status] || "学习内容整理"));
      if (job.created_at) heading.append(node("span", "field-help", new Date(job.created_at).toLocaleString("zh-CN", { hour12: false })));
      card.append(heading);
      if (job.message) card.append(node("p", "field-help", job.message));
      if (job.stop_requested) card.append(node("p", "network-status", "已请求停止后续处理。当前模型请求完成后会保留结果，不会立即撤销。"));
      if (job.error) card.append(node("p", "network-status is-error", job.error));
      list(job.articles).forEach((entry) => {
        const section = node("div", "study-job-article"); section.append(node("h4", "", entry.title || articleName(entry.article_id)), moduleRow(entry.modules));
        if (entry.error) section.append(node("p", "network-status is-error", entry.error)); card.append(section);
      });
      if (list(job.cross_article_ids).length) {
        card.append(node("h4", "", `跨文范围：${job.cross_article_ids.map(articleName).join(" · ")}`), moduleRow(job.cross, ["relations", "summary"]));
      }
      const controls = node("div", "study-job-actions");
      if (["queued", "running"].includes(job.status)) {
        const stop = action(job.stop_requested ? "已请求停止后续" : "停止后续处理", "button subtle", () => controlJob(job.id, "stop"));
        stop.disabled = Boolean(job.stop_requested || state.actionJob); controls.append(stop);
      } else if (["partial", "failed", "stopped", "interrupted"].includes(job.status)) {
        const resume = action("继续未完成模块", "button soft", () => controlJob(job.id, "resume"));
        resume.disabled = Boolean(state.actionJob || runningJobs().length || !state.aiAvailable); controls.append(resume);
      }
      card.append(controls); root.append(card);
    });
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (runningJobs().length) state.pollTimer = setTimeout(() => refresh().catch((error) => { status(`整理进度暂时无法刷新：${error.message}。可点击“刷新整理状态”。`, true); }), 2400);
  }

  async function refresh() {
    const serial = ++state.refreshSerial;
    const payload = await api("/api/study/status");
    if (serial !== state.refreshSerial) return;
    state.articles = list(payload.articles); state.jobs = list(payload.jobs).sort((a, b) => text(b.created_at).localeCompare(text(a.created_at))); state.aiAvailable = Boolean(payload.ai_available);
    const published = JSON.stringify(state.articles.map((entry) => [entry.article_id, ...articleModules.map((key) => completedModule(entry.modules?.[key]) ? entry.modules[key].status : "")]).concat(state.jobs.map((job) => [job.id, job.cross?.relations?.status, job.cross?.summary?.status, ["completed", "partial", "failed", "stopped", "interrupted"].includes(job.status) ? job.updated_at : ""])));
    const changed = state.loaded && state.published !== published;
    state.loaded = true; state.published = published;
    renderArticleStatus(); renderBanner(); renderJobs(); updateStart(); schedulePoll();
    if (changed) await onPublished();
  }

  async function acceptJob(job) {
    state.jobs = [job, ...state.jobs.filter((entry) => entry.id !== job.id)];
    renderBanner(); renderJobs(); updateStart(); schedulePoll();
    await refresh();
  }

  async function controlJob(jobId, command) {
    if (state.actionJob) return;
    state.actionJob = jobId; renderJobs(); status(command === "stop" ? "正在请求停止后续处理…" : "正在继续未完成模块…");
    try {
      const payload = await api(`/api/study/${command}`, { job_id: jobId }); await acceptJob(payload.job);
      status(command === "stop" ? "已请求停止后续处理；当前模型请求结束后保留成功结果。" : "已继续未完成模块，成功内容继续复用。");
    } catch (error) { status(error.message, true); }
    finally { state.actionJob = ""; renderJobs(); }
  }

  async function start() {
    if (state.busy || !state.preview) return;
    const articleIds = [...state.selected];
    const crossIds = $("study-cross-enable").checked ? [...state.cross] : [];
    if (!articleIds.length && !crossIds.length || crossIds.length && (crossIds.length < 2 || crossIds.length > 5)) { status("请选择需要整理的文章；跨文整理须明确选择 2–5 篇。", true); return; }
    const materials = Object.fromEntries(articleIds.map((id) => [id, [...(state.materialChoices.get(id) || new Set(["reading"]))]]));
    state.busy = true; updateStart(); status("正在提交学习内容整理…");
    let submitted = false;
    try {
      const payload = await api("/api/study/start", { article_ids: articleIds, materials, ...(state.topicId ? { topic_id: state.topicId } : {}), cross_article_ids: crossIds, regenerate: $("study-regenerate").checked });
      submitted = true; $("study-setup").hidden = true; await acceptJob(payload.job);
      status("整理已开始。可关闭此窗口继续阅读；关闭页面也不会取消后台处理。");
    } catch (error) { status(submitted ? `整理已提交，但进度刷新未完成：${error.message}。请刷新整理状态，勿重复提交。` : error.message, true); }
    finally { state.busy = false; updateStart(); }
  }

  async function afterImport({ articleIds, topicId = "", crossArticleIds = [] }) {
    const ids = [...new Set(list(articleIds).filter(Boolean))];
    if (!ids.length) return { started: false, error: "没有成功加入的文章可供整理。" };
    let submitted = false;
    try {
      const payload = await api("/api/study/start", { article_ids: ids, materials: Object.fromEntries(ids.map((id) => [id, ["reading"]])), ...(topicId ? { topic_id: topicId } : {}), cross_article_ids: crossArticleIds, regenerate: false });
      submitted = true; await acceptJob(payload.job); toast("文章已导入，并已开始整理学习内容；可查看整理进度。");
      return { started: true, job: payload.job };
    } catch (error) {
      const message = submitted ? `文章已导入，整理请求已提交，但状态刷新失败：${error.message}。请刷新整理状态。` : `文章已导入，但学习内容整理尚未启动：${error.message}。可从文章页“整理学习内容”重试，无需重新导入。`;
      toast(message, true); return { started: submitted, error: message };
    }
  }

  function showHistory() {
    $("study-dialog-title").textContent = "学习内容整理进度"; $("study-setup").hidden = true; status("");
    if (!$("study-dialog").open) $("study-dialog").showModal();
    refresh().catch((error) => status(error.message, true));
  }

  $("article-study").addEventListener("click", () => open({ articleIds: [getArticleId()] }));
  $("topic-study").addEventListener("click", () => { const scope = getScope(); if (!scope.topic) { toast("请先选择一个学习主题。"); return; } open({ articleIds: scope.topic.article_ids, topicId: scope.topic.id, theme: true }); });
  ["study-progress-open", "study-history-open"].forEach((id) => $(id).addEventListener("click", showHistory));
  $("study-close").addEventListener("click", () => $("study-dialog").close());
  $("study-refresh").addEventListener("click", () => refresh().then(() => status("整理状态已刷新。")).catch((error) => status(error.message, true)));
  $("study-start").addEventListener("click", start);
  ["study-regenerate", "study-cross-enable"].forEach((id) => $(id).addEventListener("change", updateStart));
  $("study-select-missing").addEventListener("click", () => { state.selected = new Set(list(state.preview?.articles).filter(hasMissing).map((entry) => entry.article_id)); renderPreview(); });
  $("study-select-all").addEventListener("click", () => { state.selected = new Set(list(state.preview?.articles).map((entry) => entry.article_id)); renderPreview(); });
  $("study-select-none").addEventListener("click", () => { state.selected.clear(); renderPreview(); });
  return { refresh, afterImport, showHistory, open, syncArticle(content) { state.content = content; renderArticleStatus(); } };
}

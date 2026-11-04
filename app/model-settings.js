export function createModelSettingsManager({ api, getLibrary, onChange, toast }) {
  const $ = (id) => document.getElementById(id);
  const state = { pending: false, loaded: false, busy: false };
  const fields = {
    backend: "model-backend", base_url: "model-api-url", model: "model-api-name",
    key_env: "model-key-env", reasoning_effort: "model-reasoning", max_output_tokens: "model-output-limit",
  };
  const defaults = { backend: "codex", base_url: "https://api.openai.com/v1", model: "", key_env: "OPENAI_API_KEY", reasoning_effort: "", max_output_tokens: 16384 };
  const count = (value) => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString("zh-CN") : "未记录";

  function message(value, error = false) {
    $("model-settings-message").textContent = value;
    $("model-settings-message").hidden = !value;
    $("model-settings-message").classList.toggle("is-error", error);
  }

  function updateControls() {
    const apiSelected = $("model-backend").value === "openai";
    $("model-api-fields").hidden = !apiSelected;
    $("model-api-fields").disabled = !apiSelected;
    $("model-cli-note").hidden = apiSelected;
    $("model-settings-fields").disabled = state.pending || !state.loaded;
    $("model-settings-save").disabled = state.pending || !state.loaded || state.busy || !getLibrary()?.token;
    $("model-settings-refresh").disabled = state.pending;
    $("model-settings-busy").hidden = !state.busy;
  }

  function sync(status = getLibrary()?.model_backend) {
    const label = status?.label || "当前选择的模型服务";
    document.querySelectorAll(".model-service-label").forEach((item) => { item.textContent = label; });
    if (!status) return;
    state.busy = Boolean(status.busy);
    $("model-settings-current").textContent = `当前使用：${label}`;
    $("model-settings-availability").textContent = status.message || (status.available ? "本机配置可用。" : "尚未配置可用的模型服务。");
    $("model-settings-availability").classList.toggle("is-error", !status.available);
    const usage = status.usage || {};
    $("model-usage-calls").textContent = count(usage.calls);
    $("model-usage-api-calls").textContent = count(usage.api_calls);
    ["input", "output", "total"].forEach((key) => {
      $("model-usage-" + key).textContent = usage.token_usage_calls > 0 ? count(usage[key + "_tokens"]) : "未提供";
    });
    $("model-usage-warning").textContent = usage.warning || "";
    $("model-usage-warning").hidden = !usage.warning;
    updateControls();
  }

  function fill(settings) {
    Object.entries(fields).forEach(([key, id]) => { $(id).value = settings?.[key] ?? defaults[key]; });
  }

  async function refresh() {
    if (state.pending) return;
    state.pending = true; updateControls(); message("正在读取本机模型设置…");
    try {
      const status = await api("/api/model-settings");
      state.loaded = true;
      fill(status.settings); sync(status); onChange(status);
      message("已读取当前设置；没有发送模型请求。");
    } catch (error) { message(error.message, true); }
    finally { state.pending = false; updateControls(); }
  }

  async function save(event) {
    event.preventDefault();
    if (state.pending || state.busy || !state.loaded) return;
    const settings = { backend: $("model-backend").value };
    if (settings.backend === "openai") {
      Object.entries(fields).forEach(([key, id]) => { settings[key] = $(id).value.trim(); });
      settings.max_output_tokens = Number(settings.max_output_tokens);
    }
    state.pending = true; updateControls(); message("正在保存模型设置…");
    try {
      const status = await api("/api/model-settings", settings);
      fill(status.settings); sync(status); onChange(status);
      message("设置已保存，下次主动生成时生效；没有发送模型请求。");
      toast("模型设置已保存，现有学习记录与问题草稿保留。");
    } catch (error) { message(error.message, true); }
    finally { state.pending = false; updateControls(); }
  }

  $("model-settings-open").addEventListener("click", () => {
    $("model-settings-dialog").showModal();
    refresh();
  });
  $("model-settings-close").addEventListener("click", () => $("model-settings-dialog").close());
  $("model-backend").addEventListener("change", updateControls);
  $("model-settings-refresh").addEventListener("click", refresh);
  $("model-settings-form").addEventListener("submit", save);
  fill(defaults); updateControls();
  return { sync };
}

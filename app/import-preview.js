// Shared article identity preview for local files and Zotero attachments.
export function renderImportMetadata(root, preview, pdfUrl = "") {
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = String(value);
    return element;
  };
  const metadata = preview.metadata || {};
  const identity = preview.pdf_identity || {};
  const fromZotero = metadata.source === "Zotero 书目";
  root.replaceChildren();

  const heading = node("div", "import-metadata-heading");
  heading.append(node("h4", "", "文章信息"), node("span", "field-help", metadata.source || "PDF 内嵌信息"));
  root.append(heading);
  const fields = node("dl", "import-metadata-fields");
  for (const [label, value] of [["期刊", metadata.journal], ["发表日期", metadata.date]]) {
    const field = node("div");
    field.append(node("dt", "", label), node("dd", "", value || (fromZotero ? "Zotero 中未提供" : "未识别")));
    fields.append(field);
  }
  root.append(fields);
  if (metadata.abstract) {
    const abstract = node("details", "import-abstract");
    abstract.append(node("summary", "", "摘要"), node("p", "", metadata.abstract));
    root.append(abstract);
  } else {
    root.append(node("p", "field-help", `摘要：${fromZotero ? "Zotero 中未提供" : "未识别"}`));
  }

  const file = node("div", "import-file-check");
  const fileHeading = node("div", "import-metadata-heading");
  fileHeading.append(node("h4", "", "所选 PDF"));
  if (pdfUrl) {
    const link = node("a", "text-button", "打开 PDF 核对 ↗");
    link.href = pdfUrl; link.target = "_blank"; link.rel = "noopener";
    fileHeading.append(link);
  }
  file.append(fileHeading, node("p", "import-filename", `${preview.filename || "PDF 附件"} · ${preview.pages} 页`));
  if (fromZotero && identity.title && identity.title_source !== "文件名") {
    file.append(node("p", "field-help", `PDF 内嵌标题：${identity.title}`));
  }
  const pdfDoi = identity.doi || "";
  if (pdfDoi) {
    file.append(node("p", "field-help", `PDF 中识别的 DOI：${pdfDoi}`));
    if (fromZotero && preview.doi) {
      const matches = pdfDoi.toLowerCase() === preview.doi.toLowerCase();
      file.append(node("p", matches ? "import-identity-match" : "import-identity-warning",
        matches ? "与 Zotero 书目 DOI 一致。" : "与 Zotero 书目 DOI 不同，请打开 PDF 确认是否选中了对应正文或补充材料。"));
    }
  } else {
    file.append(node("p", "field-help", "开头页未识别到 DOI，可打开 PDF 按标题核对。"));
  }
  if (preview.text_available === false) {
    file.append(node("p", "field-help", "此 PDF 开头页暂无可读取文字；仍可阅读 PDF，基于文字的智能解释可能受限。"));
  }
  root.append(file);
}

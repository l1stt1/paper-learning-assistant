const PDF_ROOT = "/vendor/pdfjs/";
const PDF_TO_CSS = 96 / 72;
let libraryPromise;

function loadLibrary() {
  if (!libraryPromise) {
    libraryPromise = import(`${PDF_ROOT}build/pdf.mjs`).then((library) => {
      library.GlobalWorkerOptions.workerSrc = `${PDF_ROOT}build/pdf.worker.mjs`;
      return library;
    }).catch((error) => {
      libraryPromise = null;
      throw error;
    });
  }
  return libraryPromise;
}

// Each request owns its canvas and text layer. A cancelled page cannot replace
// the current page when document loading or rendering finishes out of order.
export class PdfReader {
  constructor(host, onState) {
    this.host = host;
    this.onState = onState;
    this.serial = 0;
    this.documentEntry = null;
    this.renderTask = null;
    this.textLayer = null;
    this.annotations = [];
  }

  cancel() {
    this.serial += 1;
    this.renderTask?.cancel();
    this.textLayer?.cancel();
    this.renderTask = null;
    this.textLayer = null;
    this.annotations = [];
    this.host.replaceChildren();
    this.host.removeAttribute("data-page");
    return this.serial;
  }

  resetDocument() {
    this.cancel();
    const previous = this.documentEntry;
    this.documentEntry = null;
    if (previous) previous.task.destroy().catch(() => {});
  }

  selectionRects() {
    const selection = window.getSelection();
    const page = this.host.querySelector(".pdf-page");
    const textLayer = page?.querySelector(".textLayer");
    if (!selection || selection.isCollapsed || !selection.rangeCount || !textLayer?.contains(selection.getRangeAt(0).commonAncestorContainer)) return [];
    const bounds = page.getBoundingClientRect();
    const rects = [];
    for (const rect of selection.getRangeAt(0).getClientRects()) {
      const left = Math.max(bounds.left, rect.left), top = Math.max(bounds.top, rect.top);
      const right = Math.min(bounds.right, rect.right), bottom = Math.min(bounds.bottom, rect.bottom);
      if (right - left < 0.5 || bottom - top < 0.5) continue;
      const previous = rects.find((item) => Math.abs(item.top - top) < 2 && Math.abs(item.bottom - bottom) < 2 && left <= item.right + 1 && right >= item.left - 1);
      if (previous) { previous.left = Math.min(previous.left, left); previous.right = Math.max(previous.right, right); previous.top = Math.min(previous.top, top); previous.bottom = Math.max(previous.bottom, bottom); }
      else rects.push({ left, top, right, bottom });
    }
    return rects.map((rect) => ({
      x: (rect.left - bounds.left) / bounds.width,
      y: (rect.top - bounds.top) / bounds.height,
      width: (rect.right - rect.left) / bounds.width,
      height: (rect.bottom - rect.top) / bounds.height,
    }));
  }

  setAnnotations(annotations) {
    this.annotations = annotations;
    this.drawAnnotations();
  }

  drawAnnotations() {
    const layer = this.host.querySelector(".user-annotation-layer");
    if (!layer) return;
    layer.replaceChildren();
    for (const annotation of this.annotations) {
      for (const rect of annotation.rects) {
        const highlight = document.createElement("div");
        highlight.className = "user-highlight";
        highlight.dataset.annotationId = annotation.id;
        highlight.style.left = `${rect.x * 100}%`;
        highlight.style.top = `${rect.y * 100}%`;
        highlight.style.width = `${rect.width * 100}%`;
        highlight.style.height = `${rect.height * 100}%`;
        layer.append(highlight);
      }
    }
  }

  annotationAt(clientX, clientY) {
    const page = this.host.querySelector(".pdf-page");
    if (!page || !this.host.dataset.page) return null;
    const bounds = page.getBoundingClientRect();
    const x = (clientX - bounds.left) / bounds.width, y = (clientY - bounds.top) / bounds.height;
    return this.annotations.find((annotation) => annotation.rects.some((rect) => x >= rect.x && x <= rect.x + rect.width && y >= rect.y && y <= rect.y + rect.height));
  }

  selectAnnotation(id) {
    let first;
    this.host.querySelectorAll(".user-highlight").forEach((highlight) => {
      const selected = highlight.dataset.annotationId === id;
      highlight.classList.toggle("selected", selected);
      if (selected && !first) first = highlight;
    });
    first?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  async render({ url, pageNumber, zoom = "fit", pageLabel = "原始 PDF" }) {
    const serial = this.cancel();
    const current = () => serial === this.serial;
    this.onState({ status: "loading", pageNumber });
    try {
      const library = await loadLibrary();
      if (!current()) return;
      if (this.documentEntry?.url !== url) {
        const previous = this.documentEntry;
        this.documentEntry = null;
        if (previous) previous.task.destroy().catch(() => {});
        const task = library.getDocument({
          url,
          cMapUrl: `${PDF_ROOT}cmaps/`,
          cMapPacked: true,
          standardFontDataUrl: `${PDF_ROOT}standard_fonts/`,
          wasmUrl: `${PDF_ROOT}wasm/`,
          iccUrl: `${PDF_ROOT}iccs/`,
          isEvalSupported: false,
        });
        this.documentEntry = { url, task };
      }
      const entry = this.documentEntry;
      let pdf;
      try {
        pdf = await entry.task.promise;
      } catch (error) {
        if (this.documentEntry === entry) this.documentEntry = null;
        entry.task.destroy().catch(() => {});
        throw error;
      }
      if (!current()) return;
      const page = await pdf.getPage(pageNumber);
      if (!current()) return;
      const baseViewport = page.getViewport({ scale: PDF_TO_CSS });
      const hostStyle = getComputedStyle(this.host);
      const availableWidth = Math.max(100, Math.floor(this.host.getBoundingClientRect().width - parseFloat(hostStyle.paddingLeft) - parseFloat(hostStyle.paddingRight)));
      const zoomFactor = zoom === "fit" ? availableWidth / baseViewport.width : Number(zoom);
      const viewport = page.getViewport({ scale: PDF_TO_CSS * zoomFactor });
      const pageElement = document.createElement("div");
      pageElement.className = "pdf-page is-rendering";
      pageElement.setAttribute("aria-label", `${pageLabel} 第 ${pageNumber} 页`);
      pageElement.style.width = `${viewport.width}px`;
      pageElement.style.height = `${viewport.height}px`;
      pageElement.style.setProperty("--total-scale-factor", viewport.scale * viewport.userUnit);
      const canvas = document.createElement("canvas");
      canvas.setAttribute("aria-hidden", "true");
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.ceil(viewport.width * pixelRatio);
      canvas.height = Math.ceil(viewport.height * pixelRatio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      const textElement = document.createElement("div");
      textElement.className = "textLayer";
      const annotationElement = document.createElement("div");
      annotationElement.className = "user-annotation-layer";
      annotationElement.setAttribute("aria-hidden", "true");
      pageElement.append(canvas, annotationElement, textElement);
      this.host.replaceChildren(pageElement);
      this.host.scrollLeft = 0;
      this.renderTask = page.render({
        canvas,
        viewport,
        transform: pixelRatio === 1 ? null : [pixelRatio, 0, 0, pixelRatio, 0, 0],
      });
      const textLayer = new library.TextLayer({
        textContentSource: page.streamTextContent(),
        container: textElement,
        viewport,
      });
      this.textLayer = textLayer;
      await Promise.all([this.renderTask.promise, textLayer.render()]);
      if (!current()) return;
      this.renderTask = null;
      pageElement.classList.remove("is-rendering");
      this.host.dataset.page = String(pageNumber);
      this.drawAnnotations();
      this.onState({
        status: "ready",
        pageNumber,
        totalPages: pdf.numPages,
        hasText: textLayer.textContentItemsStr.some((text) => text.trim()),
      });
    } catch (error) {
      if (!current()) return;
      this.cancel();
      this.onState({ status: "error", pageNumber, message: error.message || "PDF 页面无法显示。" });
    }
  }
}

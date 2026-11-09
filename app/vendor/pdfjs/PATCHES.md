# PDF.js local patch

Base: `pdfjs-dist` 5.6.205, build `ada343803`. The upstream version and worker API remain unchanged.

`build/pdf.mjs` includes the filename-scanning fix from [Mozilla PDF.js PR #21685](https://github.com/mozilla/pdf.js/pull/21685), merged as `89852881b8e6c597270f8ef77815ae7675318416`. In `getPdfFilenameFromUrl`, locate the last `.pdf` extension and then walk backwards to the preceding separator. This avoids quadratic backtracking when a URL fragment has no PDF filename.

The bundled worker is unmodified. In particular, `FileSpec.filename` deliberately normalizes escaped separators in the upstream order; see the [upstream explanation](https://github.com/mozilla/pdf.js/blob/v5.6.205/src/core/file_spec.js#L58-L66). Do not reorder these replacements in response to a generic double-unescaping warning.

Regression checks: `node --test tests/test_pdfjs_filename.mjs` from the repository root. Remove this backport only when updating the full PDF.js distribution to a version containing the upstream fix.

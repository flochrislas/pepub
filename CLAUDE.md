# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Converter

**CLI — single EPUB:**
```bash
python pepub.py path/to/book.epub
python pepub.py path/to/book.epub --overwrite   # re-convert even if output folder exists
```

**CLI — batch (folder of EPUBs):**
```bash
python pepub.py path/to/folder/
```

**GUI:**
```bash
pythonw pepub-gui.pyw    # Windows, no console window
# or double-click pepub-gui.bat
```

## External Requirement

`pandoc` must be installed and available in `PATH`. The script checks for it at startup and exits with a clear error if missing.

## Dependencies

- `ebooklib` — EPUB parsing
- `beautifulsoup4` + `lxml` — HTML parsing
- `pypandoc` — HTML→Markdown (wraps pandoc binary)
- `pyyaml` — YAML frontmatter
- `customtkinter` — GUI only

## Architecture

The project is two files:

- **`pepub.py`** — all conversion logic; no side-effects on import, safe to `import convert_epub` from elsewhere
- **`pepub-gui.pyw`** — `customtkinter` wrapper; imports and calls `convert_epub()` in a background thread, redirecting stdout/stderr into a queue that feeds the log textbox

### Conversion pipeline (`convert_epub`)

1. **Pre-scan phase** — three book-wide passes before any chapter is written:
   - `extract_images` — copies all EPUB images into `assets/`, deduplicating names
   - `build_footnote_map` — scans all documents for `<a id="_ftnN">` anchors and stores the following sibling HTML as footnote bodies
   - `build_toc_map` — walks `book.toc` to build `{file_name: title}` for fallback title resolution
   - `build_css_heading_classes` — parses EPUB CSS to detect class names that look like block headings (large font-size or centered + large top-margin)

2. **Two processing paths** (chosen based on whether `_build_flat_toc` returns entries):

   **Primary — TOC-driven** (`_process_toc_section`): one markdown file per TOC entry (chapters, sections, sub-sections). `_extract_section_html` slices the raw HTML from `start_anchor_id` up to the next TOC anchor in the same file, so sections within a shared HTML file each get their own output file. Title comes directly from the TOC entry; no title extraction needed.

   **Fallback — spine-based** (`process_chapter`): used when the EPUB has no TOC. Iterates `book.spine`, skips `linear='no'` and non-`EpubHtml` items. Title extraction priority: toc_map lookup → `h1–h6` in body → CSS class matching `_TITLE_CLASS_RE` → `<title>` tag → `"Section N"`. Skip gate: `item.is_chapter()`, not a nav document, stem not in `SKIP_FILENAME_PATTERNS`.

   **Shared HTML pre-processing** (order matters in both paths):
   1. `extract_footnote_refs` — replaces `<a href="#_ftnN">` with `FNREF_N` placeholder
   2. `fix_image_refs` — rewrites `<img src>` to `assets/<local_name>`
   3. `promote_title_elements` — upgrades `<div>`/`<p>` etc. with title-like CSS classes to `<h>` tags
   4. `clean_html_attrs` — removes empty anchors, unwraps block containers, strips non-essential attributes

   After conversion: `html_to_markdown` calls pandoc then `_postprocess_markdown` (strips residual HTML, fenced divs, pandoc span annotations, normalises whitespace). Appends pandoc-style footnote definitions `[^N]: …` for any refs found.

3. **TOC file** (`generate_toc_file`) — `00 - BookTitle.md` with YAML frontmatter (`title`, `author`, `publisher`, `year`, `read`, `rating`, `tags`) and `[[wiki-link]]` list

### Key design decisions

- `_read_epub_tolerant` monkey-patches `zipfile.ZipFile.read` to return empty bytes for manifest items missing from the ZIP archive (a common EPUB defect), restoring the original method in a `finally` block
- In TOC-driven mode, `_extract_section_html` uses raw string search (not a second parse) to find anchor positions, then wraps the slice in `<body>…</body>` for a second parse — faster than re-parsing the full document per section
- `index` (chapter counter) increments only for sections/chapters that produce non-empty output; `total` is the number of TOC entries (or candidate spine items) to fix zero-padding width
- Image collision: second occurrence of the same basename gets `stem_1.ext`, third gets `stem_2.ext`, etc.
- `promote_title_elements` must run before `clean_html_attrs` (which strips `class` attributes)
- `extract_footnote_refs` must run before `clean_html_attrs` (which removes `<a>` tags without hrefs)

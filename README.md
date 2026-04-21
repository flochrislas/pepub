# pepub

Convert EPUB books into Obsidian-compatible markdown files.

Each book is extracted into a dedicated folder containing one markdown file per chapter (or TOC section), a table-of-contents index file with YAML frontmatter, and an `assets/` subfolder for images.

```
My Book/
├── 00 - My Book.md          ← index with YAML frontmatter + [[wiki-links]]
├── 01 - Introduction.md
├── 02 - Chapter One.md
├── ...
└── assets/
    └── cover.jpg
```

The index file includes editable metadata fields ready for use in Obsidian:

```yaml
---
title: My Book
author: Author Name
publisher: Publisher
year: 2021
read: false
rating: null
tags:
  - book
---
```

## Requirements

**Pandoc** must be installed and available in `PATH`:

```bash
# Windows
winget install --id JohnMacFarlane.Pandoc

# macOS
brew install pandoc
```

Or download from [pandoc.org](https://pandoc.org/installing.html).

**Python packages:**

```bash
pip install ebooklib beautifulsoup4 lxml pyyaml pypandoc customtkinter
```

## Usage

### Command line

```bash
# Single file
python pepub.py path/to/book.epub

# Entire folder
python pepub.py path/to/folder/

# Re-convert books that were already converted
python pepub.py path/to/book.epub --overwrite

# Write output to a specific folder (default: next to each EPUB)
python pepub.py path/to/folder/ --output-dir path/to/vault/
```

Output is written to a folder named after the EPUB **filename** (not the book's metadata title). This means you can rename an `.epub` before conversion to control the output folder name — and rerunning will skip any `.epub` whose folder already exists, so partial batches can be resumed safely.

### GUI

```bash
pythonw pepub-gui.pyw
```

Or double-click `pepub-gui.bat` on Windows.

The GUI has two path fields — **Input** (a single `.epub` or a folder of them) and **Output** (optional; leave empty to write alongside each source file) — plus an **Overwrite** checkbox and a **Convert** button.

When you select an input, the main area previews the EPUB files that will actually be converted, filtered against what already exists in the output folder and the Overwrite setting. Once you click Convert, the same area streams the conversion log and a batch summary at the end.

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
```

Output is written to a folder named after the book title, in the same directory as the source EPUB.

### GUI

```bash
pythonw pepub-gui.pyw
```

Or double-click `pepub-gui.bat` on Windows. The GUI lets you pick a file or folder, toggle the overwrite option, and view conversion progress in a log window.

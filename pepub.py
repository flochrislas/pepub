import argparse, io, os, posixpath, re, sys, unicodedata, warnings, zipfile
from pathlib import Path
import ebooklib, yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from ebooklib import epub
import pypandoc

warnings.filterwarnings('ignore', category=XMLParsedAsHTMLWarning)

SKIP_FILENAME_PATTERNS = frozenset([
    'cover', 'copyright', 'toc', 'nav', 'navigation',
    'title-page', 'titlepage', 'title_page', 'halftitle',
    'half-title', 'half_title', 'dedication', 'colophon',
    'back-cover', 'backmatter',
])

# CSS class patterns that indicate heading elements in EPUB HTML
# Matches: chap-tit, chapitre-titre2, part-tit, appcrit-tit, pretit, sous-titre…
_TITLE_CLASS_RE = re.compile(
    r'(?:^|[-_\s])tit(?:re|le)?(?:[-_\s]|$)|tit(?:re|le)?$', re.IGNORECASE
)
# Matches: niv1-int, niv2-int, section-inter, intniv, intniv2…
_INTERTITLE_CLASS_RE = re.compile(
    r'(?:^|[-_\s])inter?(?:[-_\s]|$)|(?:^|[-_\s])intniv', re.IGNORECASE
)


def sanitize_filename(name):
    name = unicodedata.normalize('NFC', name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = name[:200]
    name = name.rstrip('. ')  # Windows forbids trailing dots/spaces in folder names
    return name or 'Untitled'


def extract_metadata(book):
    def get_field(field):
        values = book.get_metadata('DC', field)
        return values[0][0] if values else None

    title = get_field('title') or 'Unknown Title'
    # Fix erroneous space after apostrophe in contractions (common EPUB metadata defect)
    title = re.sub(r"([a-zA-Z\u00C0-\u024F])([\u2018\u2019'])\s+(?=[a-zA-Z\u00C0-\u024F])", r'\1\2', title)
    author = get_field('creator')
    publisher = get_field('publisher')
    raw_date = get_field('date')
    year = None
    if raw_date:
        m = re.search(r'\b(1[0-9]{3}|20[0-9]{2})\b', raw_date)
        year = int(m.group(1)) if m else None

    return {
        'title': title,
        'author': author,
        'publisher': publisher,
        'year': year,
    }


def extract_images(book, assets_dir):
    image_map = {}
    used_names = {}

    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        basename = os.path.basename(item.file_name)
        stem, ext = os.path.splitext(basename)

        if basename in used_names:
            count = used_names[basename]
            used_names[basename] = count + 1
            local_name = f'{stem}_{count}{ext}'
        else:
            used_names[basename] = 1
            local_name = basename

        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / local_name).write_bytes(item.get_content())
        image_map[item.file_name] = local_name

    return image_map


def resolve_image_href(raw_src, chapter_href, image_map):
    # Strip query and fragment
    raw_src = raw_src.split('?')[0].split('#')[0]

    if raw_src in image_map:
        return image_map[raw_src]

    chapter_dir = posixpath.dirname(chapter_href)
    resolved = posixpath.normpath(posixpath.join(chapter_dir, raw_src))
    if resolved in image_map:
        return image_map[resolved]

    return None


def fix_image_refs(soup, image_map, chapter_href):
    for img in soup.find_all('img'):
        raw_src = img.get('src', '')
        if not raw_src:
            continue
        local_name = resolve_image_href(raw_src, chapter_href, image_map)
        if local_name:
            img['src'] = f'assets/{local_name}'
        else:
            print(f'Warning: image not found: {raw_src}', file=sys.stderr)


def build_footnote_map(book):
    """Pre-scan all EPUB documents and return {ftn_id: footnote_html_fragment}.
    e.g. {'_ftn2': 'C\'est le titre que La Boétie...'}
    """
    fmap = {}
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), 'lxml')
        for anchor in soup.find_all('a', id=re.compile(r'^_ftn\d+$')):
            ftn_id = anchor['id']
            # Collect HTML of siblings that follow the anchor (the footnote text)
            parts = [str(s) for s in anchor.next_siblings]
            fn_html = ''.join(parts).strip()
            if fn_html:
                fmap[ftn_id] = fn_html
    return fmap


def build_toc_map(book):
    """Return {file_name: title} from the EPUB's NCX/TOC.

    Chapter-level links (no '#' fragment) take priority over in-page section
    links that share the same base href. Without this, the last sub-section
    entry would overwrite the chapter title for a given file.
    """
    toc_map = {}
    chapter_hrefs = set()  # hrefs established by fragment-free (chapter-level) links

    def _set(href, basename, title, has_fragment):
        if not has_fragment:
            toc_map[href] = title
            toc_map[basename] = title
            chapter_hrefs.add(href)
            chapter_hrefs.add(basename)
        else:
            # Only fill in if no chapter-level entry already owns this href
            if href not in chapter_hrefs:
                toc_map[href] = title
            if basename not in chapter_hrefs:
                toc_map[basename] = title

    def _walk(items):
        for item in items:
            if isinstance(item, epub.Link):
                href = item.href.split('#')[0]
                if item.title and href:
                    _set(href, href.split('/')[-1], item.title, '#' in item.href)
            elif isinstance(item, tuple) and len(item) == 2:
                section, children = item
                if hasattr(section, 'href') and section.href:
                    href = section.href.split('#')[0]
                    if hasattr(section, 'title') and section.title:
                        _set(href, href.split('/')[-1], section.title, '#' in section.href)
                _walk(children)

    _walk(book.toc)
    return toc_map


def _build_flat_toc(book):
    """Return a flat ordered list of all TOC entries: [(title, file_href, anchor_id_or_None)].

    Walks the EPUB TOC recursively (parent before children) so that every entry at
    every nesting level — chapters, sections, sub-sections — is included in order.
    """
    entries = []

    def _walk(items):
        for item in items:
            if isinstance(item, epub.Link):
                href_parts = item.href.split('#', 1)
                file_href = href_parts[0]
                anchor = href_parts[1] if len(href_parts) > 1 else None
                if item.title and file_href:
                    entries.append((item.title, file_href, anchor))
            elif isinstance(item, tuple) and len(item) == 2:
                section, children = item
                if hasattr(section, 'href') and section.href:
                    href_parts = section.href.split('#', 1)
                    file_href = href_parts[0]
                    anchor = href_parts[1] if len(href_parts) > 1 else None
                    if hasattr(section, 'title') and section.title:
                        entries.append((section.title, file_href, anchor))
                _walk(children)

    _walk(book.toc)
    return entries


def _extract_section_html(html_content, start_anchor_id, end_anchor_id=None,
                          start_title=None, end_title=None):
    """Extract a body section from start_anchor_id up to (but not including) end_anchor_id.

    Returns a BeautifulSoup <body> element containing only the extracted fragment.
    If start_anchor_id is None the full body is returned.

    When an anchor ID is not found in the HTML, falls back to locating the boundary
    by matching start_title / end_title against block-level element text content.
    Falls back to the full body only when both anchor and title lookups fail.
    """
    soup = BeautifulSoup(html_content, 'lxml')
    body = soup.find('body')
    if not body:
        return None
    if not start_anchor_id and not end_anchor_id:
        return body  # no bounds: return full body

    html_str = str(body)

    def _find_tag_start(s, anchor_id):
        """Return the position of the opening '<' of the tag bearing id=anchor_id."""
        for quote in ('"', "'"):
            marker = f'id={quote}{anchor_id}{quote}'
            idx = s.find(marker)
            if idx >= 0:
                return s.rfind('<', 0, idx)
        return -1

    def _norm(s):
        s = re.sub(r'<[^>]+>', '', s)
        s = s.replace('\u00a0', ' ')
        s = re.sub(r'&[a-z]+;|&#\d+;', ' ', s)
        s = unicodedata.normalize('NFC', s)
        return re.sub(r'\s+', ' ', s).strip().lower()

    def _find_title_pos(s, title):
        """Return the position of a block element whose plain text matches title."""
        title_norm = _norm(title)
        if not title_norm:
            return -1
        for m in re.finditer(r'<(p|h[1-6])\b[^>]*>(.*?)</\1>', s, re.DOTALL | re.IGNORECASE):
            if _norm(m.group(2)) == title_norm:
                return m.start()
        return -1

    body_open_end = html_str.index('>') + 1
    body_close = html_str.rfind('</body>')
    if body_close < 0:
        body_close = len(html_str)

    if start_anchor_id:
        start = _find_tag_start(html_str, start_anchor_id)
        if start < 0 and start_title:
            start = _find_title_pos(html_str, start_title)
        if start < 0:
            start = body_open_end
    else:
        start = body_open_end  # no start anchor: extract from beginning of body

    if end_anchor_id:
        end = _find_tag_start(html_str, end_anchor_id)
        if end < 0 and end_title:
            end = _find_title_pos(html_str, end_title)
        if end < 0 or end <= start:
            end = body_close
    else:
        end = body_close

    fragment = f'<body>{html_str[start:end]}</body>'
    new_soup = BeautifulSoup(fragment, 'lxml')
    return new_soup.find('body')


def _css_length_is_large(val):
    """Return True if a CSS length value is large enough to suggest heading spacing."""
    m = re.match(r'([\d.]+)(em|rem|pt|px)', val.strip())
    if m:
        n, u = float(m.group(1)), m.group(2)
        return (u in ('em', 'rem') and n >= 1.5) or (u == 'pt' and n >= 20) or (u == 'px' and n >= 27)
    return False


def build_css_heading_classes(book):
    """Parse EPUB CSS and return class names that look like block headings.

    Heuristics (block-level signals only, to avoid catching inline drop-caps):
    - font-size: large / x-large / > 1.1em / > 13pt
    - text-align: center  AND  text-indent: 0  AND  top-margin >= 1.5em
    """
    heading_classes = set()
    for item in book.get_items_of_type(ebooklib.ITEM_STYLE):
        try:
            css = item.get_content().decode('utf-8', errors='replace')
        except Exception:
            continue
        for m in re.finditer(r'([^{}]+)\{([^}]*)\}', css, re.DOTALL):
            selector_raw, props = m.group(1).strip(), m.group(2)
            # Only process simple single-class selectors (.classname or
            # tag.classname). Skip compound/descendant selectors like
            # "#id .classname" — those override styles in a specific context
            # and should not be treated as global heading styles.
            classnames_to_check = []
            for sel in selector_raw.split(','):
                sel = sel.strip()
                cls_m = re.match(r'^[\w]*\.([\w-]+)$', sel)
                if cls_m:
                    classnames_to_check.append(cls_m.group(1))
            if not classnames_to_check:
                continue

            # Large font-size (keyword or em/pt value)
            fs = re.search(r'font-size\s*:\s*([^;]+)', props)
            if fs:
                v = fs.group(1).strip().lower()
                if v in ('large', 'x-large', 'xx-large', 'larger'):
                    heading_classes.update(classnames_to_check)
                    continue
                fm = re.match(r'([\d.]+)(em|rem|pt|px)', v)
                if fm:
                    n, u = float(fm.group(1)), fm.group(2)
                    if (u in ('em', 'rem') and n > 1.1) or (u == 'pt' and n > 13) or (u == 'px' and n > 17):
                        heading_classes.update(classnames_to_check)
                        continue

            # Centered block with no text-indent and a significant top margin
            align = re.search(r'text-align\s*:\s*(\w+)', props)
            indent = re.search(r'text-indent\s*:\s*([^;]+)', props)
            if align and align.group(1) == 'center' and indent and re.match(r'\s*0', indent.group(1)):
                mt = re.search(r'margin-top\s*:\s*([^;]+)', props)
                mg = re.search(r'(?:^|[\s;])margin\s*:\s*([^;]+)', props)
                top_val = None
                if mt:
                    top_val = mt.group(1).strip()
                elif mg:
                    parts = mg.group(1).strip().split()
                    if parts:
                        top_val = parts[0]
                if top_val and _css_length_is_large(top_val):
                    heading_classes.update(classnames_to_check)

    return heading_classes


def extract_footnote_refs(soup):
    """Replace footnote reference anchors with FNREF_N placeholders.
    Must be called BEFORE clean_html_attrs.
    Returns sorted list of referenced footnote numbers (strings).
    """
    refs = []
    for anchor in soup.find_all('a', href=True):
        href = anchor['href']
        m = re.search(r'#_ftn(\d+)\b', href)
        if m and not href.startswith(('http://', 'https://')):
            num = m.group(1)
            refs.append(num)
            anchor.replace_with(f'FNREF_{num}')
    return refs


_PROMOTE_INLINE = frozenset({
    'span', 'em', 'strong', 'b', 'i', 'a', 'u', 's', 'code',
    'sub', 'sup', 'small', 'cite', 'abbr', 'dfn', 'var', 'mark', 'time',
})


_BOLD_TAGS = frozenset(['b', 'strong'])
_BOLD_CLASSES = frozenset(['bold', 'gras'])


def _is_bold_heading(el):
    """Return True if el looks like an unlabelled bold heading.

    Criteria (all must hold):
    - Short text (≤ 80 chars) with at least 2 non-separator characters.
    - Does not read like a sentence (no ". " mid-text, does not end with '.').
    - Every non-whitespace text node is inside a <b>, <strong>, or
      <span class="bold|gras"> ancestor — i.e. the whole content is bold.

    This catches Calibre-generated EPUBs where sub-headings are encoded as
    plain <p> elements with bold spans rather than proper <h> tags or
    named heading CSS classes.
    """
    from bs4 import NavigableString
    text = el.get_text(strip=True)
    if not text or len(text) > 80:
        return False
    if len(re.sub(r'[\s*·•\-–—_=~]+', '', text)) < 2:
        return False  # separator line, not a heading
    if re.search(r'\.\s', text) or text.endswith('.'):
        return False  # reads like a sentence
    for node in el.descendants:
        if not isinstance(node, NavigableString) or not str(node).strip():
            continue
        parent = node.parent
        in_bold = False
        while parent and parent is not el:
            if parent.name in _BOLD_TAGS:
                in_bold = True
                break
            if parent.name == 'span' and _BOLD_CLASSES.intersection(parent.get('class', [])):
                in_bold = True
                break
            parent = parent.parent
        if not in_bold:
            return False
    return True


def promote_title_elements(soup, css_heading_classes=None, seen_title=False):
    """Convert non-heading elements with title-like CSS classes to <h> tags.

    Detection order (all must be block-level elements):
    1. _TITLE_CLASS_RE matches  → first seen: h1, rest: h2
    2. _INTERTITLE_CLASS_RE     → h{niv+1} (default h2)
    3. css_heading_classes      → always h2 (section headings in generic EPUBs)
    4. _is_bold_heading         → always h2 (Calibre-style bold-only headings)

    Pass seen_title=True when the chapter heading has already been established
    (e.g. the original h1 was a bare number and was removed) so that interior
    title-class elements are not incorrectly promoted to h1.

    Must be called BEFORE clean_html_attrs strips class attributes.
    """
    for el in soup.find_all(True):
        if el.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            if el.name == 'h1':
                seen_title = True
            continue
        if el.name in _PROMOTE_INLINE:
            continue  # never promote inline elements (catches drop-caps etc.)
        cls_list = el.get('class', [])
        cls = ' '.join(cls_list)

        is_main_title = bool(_TITLE_CLASS_RE.search(cls))
        is_intertitle = bool(_INTERTITLE_CLASS_RE.search(cls))
        is_css_heading = bool(css_heading_classes and css_heading_classes.intersection(cls_list))

        if is_main_title:
            if not seen_title:
                el.name = 'h1'
                seen_title = True
            else:
                el.name = 'h2'
        elif is_intertitle:
            # niv1-int → h2, niv2-int → h3, niv3-int → h4, etc.
            m = re.search(r'niv(\d+)', cls, re.IGNORECASE)
            level = min(int(m.group(1)) + 1, 4) if m else 2
            el.name = f'h{level}'
        elif is_css_heading or _is_bold_heading(el):
            # Skip separator elements (e.g. a centered paragraph containing only '*' or '***')
            text = el.get_text(strip=True)
            if text and len(re.sub(r'[\s*·•\-–—_=~]+', '', text)) >= 2:
                el.name = 'h2'


def clean_html_attrs(soup):
    """Remove noise before pandoc conversion:
    - Empty anchors (<a id="x"/>) → become []{#x} in pandoc output
    - All non-essential attributes on all tags
    - <br/> inside inline elements → become \\ line-break artifacts in pandoc output
    - Block containers → unwrapped so pandoc doesn't emit fenced divs (::: {})
    """
    for tag in soup.find_all('a'):
        href = tag.get('href', '')
        if not href and not tag.get_text(strip=True):
            tag.decompose()
        elif href and not href.startswith(('http://', 'https://')):
            tag.unwrap()
    inline_tags = {'b', 'strong', 'i', 'em', 'span', 'a', 'u', 's', 'small', 'sup', 'sub'}
    for br in soup.find_all('br'):
        if br.parent and br.parent.name in inline_tags:
            br.decompose()
        else:
            br.replace_with(' ')
    block_containers = {'div', 'section', 'article', 'aside', 'main', 'header', 'footer'}
    for tag in soup.find_all(block_containers):
        tag.unwrap()

    # Unwrap semantic inline elements that pandoc converts to [text]{.tag} annotations
    for tag in soup.find_all(['small', 'cite', 'abbr', 'dfn', 'var', 'mark', 'time']):
        tag.unwrap()
    keep_attrs = {
        'a':   {'href', 'title'},
        'img': {'src', 'alt', 'width', 'height'},
    }
    for tag in soup.find_all(True):
        allowed = keep_attrs.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag.attrs[attr]


def _postprocess_markdown(md):
    # Strip self-closing HTML tags
    md = re.sub(r'<(br|hr|img|input|meta|link)\s*/>', '', md, flags=re.IGNORECASE)
    md = re.sub(r'<(br|hr)\s*>', '', md, flags=re.IGNORECASE)

    # Strip block/inline tag wrappers preserving content
    block_tags = r'div|span|section|aside|figure|figcaption|article|main|header|footer|nav|p|blockquote'
    md = re.sub(rf'</?({block_tags})[^>]*>', '', md, flags=re.IGNORECASE)

    # Catch-all: strip remaining HTML-like tags
    md = re.sub(r'<[^>]+>', '', md)

    # Strip pandoc fenced divs: opening ":::  {..}" and closing ":::" lines
    md = re.sub(r'^:{3,}\s*\{[^}]*\}\s*$', '', md, flags=re.MULTILINE)
    md = re.sub(r'^:{3,}\s*$', '', md, flags=re.MULTILINE)

    # Convert footnote ref placeholders to pandoc footnote syntax
    md = re.sub(r'FNREF_(\d+)', r'[^\1]', md)

    # Strip trailing backslashes left by <br/> inside inline elements
    md = re.sub(r'\\\s*$', '', md, flags=re.MULTILINE)

    # Strip any remaining pandoc span annotations: [text]{.class} or [text]{#id} → text
    md = re.sub(r'\[([^\]\n]*)\]\{[.#][^}]*\}', r'\1', md)

    # Flatten pandoc superscript ^text^ and subscript ~text~ (not rendered by Obsidian)
    md = re.sub(r'\^([^^]+)\^', r'\1', md)
    md = re.sub(r'(?<!~)~([^~]+)~(?!~)', r'\1', md)

    # Unescape apostrophes (pandoc escapes them unnecessarily)
    md = md.replace("\\'", "'")

    # Collapse nested blockquotes (> > > >) to a single level (>)
    md = re.sub(r'^(>\s*)+', '> ', md, flags=re.MULTILINE)

    # Collapse 3+ blank lines to 2
    md = re.sub(r'\n{3,}', '\n\n', md)

    # Strip trailing whitespace per line
    md = '\n'.join(line.rstrip() for line in md.splitlines())

    return md


def html_to_markdown(html):
    md = pypandoc.convert_text(
        html, to='markdown', format='html',
        extra_args=['--wrap=none', '--markdown-headings=atx', '--strip-comments']
    )
    return _postprocess_markdown(md)


def _process_toc_section(toc_title, body, file_href, output_dir, image_map, index, total, footnote_map, css_heading_classes):
    """Process a BeautifulSoup body fragment (from a TOC entry) and write as a markdown file.

    Unlike process_chapter, the title comes directly from the TOC so no title
    extraction from HTML is needed. This is the primary processing path when the
    EPUB has a usable TOC (see convert_epub).
    """
    chapter_title = toc_title

    refs = extract_footnote_refs(body) if footnote_map is not None else []
    fix_image_refs(body, image_map, file_href)
    # seen_title=True: we already have the title; avoid promoting body elements to h1.
    promote_title_elements(body, css_heading_classes=css_heading_classes, seen_title=True)
    clean_html_attrs(body)
    md = html_to_markdown(str(body))

    # Remove headings that duplicate the TOC title — with or without its "N." prefix.
    title_norm = re.sub(r'\W+', ' ', chapter_title).strip().lower()
    title_norm_noprefix = re.sub(r'\W+', ' ', re.sub(r'^\d+[\.\-–—]\s*', '', chapter_title)).strip().lower()

    lines = md.split('\n')
    filtered = []
    for line in lines:
        if line.startswith('>'):
            line_text = re.sub(r'\W+', ' ', re.sub(r'^[>\s]+', '', line)).strip().lower()
            if line_text in (title_norm, title_norm_noprefix):
                continue
        heading_m = re.match(r'^#{1,6}\s+(.+)', line)
        if heading_m:
            text = heading_m.group(1)
            text_norm = re.sub(r'\W+', ' ', text).strip().lower()
            if text_norm in (title_norm, title_norm_noprefix):
                continue
            if re.match(r'^\d+\.?\s*$', text.strip()):
                continue  # bare chapter-number heading (e.g. "# 2") — artifact
        filtered.append(line)
    md = re.sub(r'\n{3,}', '\n\n', '\n'.join(filtered))

    # Demote any remaining h1 to h2, then prepend the TOC title as the sole h1.
    md = re.sub(r'^# (.+)', r'## \1', md, flags=re.MULTILINE)
    md = f'# {chapter_title}\n\n{md.lstrip()}'

    if refs and footnote_map:
        defs = []
        for num in sorted(set(refs), key=int):
            ftn_id = f'_ftn{num}'
            if ftn_id in footnote_map:
                fn_html = f'<body>{footnote_map[ftn_id]}</body>'
                fn_md = html_to_markdown(fn_html).strip()
                defs.append(f'[^{num}]: {fn_md}')
        if defs:
            md += '\n\n' + '\n'.join(defs) + '\n'

    pad = len(str(total))
    safe_title = sanitize_filename(chapter_title)
    filename = f'{str(index).zfill(pad)} - {safe_title}.md'
    if len(filename) > 120:
        filename = filename[:117].rstrip(' -') + '.md'

    (output_dir / filename).write_text(md, encoding='utf-8')
    return {'title': chapter_title, 'filename': filename}


def process_chapter(item, book, output_dir, image_map, index, total, footnote_map=None, toc_map=None, css_heading_classes=None):
    # Skip non-chapter items
    if not item.is_chapter():
        return None
    if 'nav' in (item.properties or []):
        return None

    # Check filename against skip patterns
    stem = Path(item.file_name).stem.lower()
    if stem in SKIP_FILENAME_PATTERNS:
        return None

    content = item.get_content()
    soup = BeautifulSoup(content, 'lxml')

    # Skip empty chapters
    body = soup.find('body')
    if not body:
        return None
    if not body.get_text(strip=True) and not body.find('img'):
        return None

    # Extract title: TOC map > h1-h6 > class-based > <title> tag > fallback
    # TOC is checked first because it reliably maps each file to its human-readable
    # title even in EPUBs where the body h1 is just a bare chapter number.
    chapter_title = None
    if toc_map:
        fn = item.file_name
        raw = toc_map.get(fn) or toc_map.get(fn.split('/')[-1])
        if raw:
            # Strip a leading "N. " / "N – " chapter-number prefix if present
            chapter_title = re.sub(r'^\d+[\.\-–—]\s*', '', raw).strip() or raw
    if not chapter_title:
        for tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            heading = body.find(tag)
            if heading and heading.get_text(strip=True):
                chapter_title = heading.get_text(strip=True)
                break
    if not chapter_title:
        # Look for elements whose CSS class suggests a title (e.g. "chap-tit", "titre")
        for el in body.find_all(True):
            cls = ' '.join(el.get('class', []))
            if _TITLE_CLASS_RE.search(cls):
                text = el.get_text(strip=True)
                if text and len(text) < 200:
                    chapter_title = text
                    break
    if not chapter_title:
        title_tag = soup.find('title')
        if title_tag and title_tag.get_text(strip=True):
            chapter_title = title_tag.get_text(strip=True)
    if not chapter_title:
        chapter_title = f'Section {index}'

    # Extract footnote refs before cleaning (replaces <a href="#_ftnN"> with FNREF_N)
    refs = extract_footnote_refs(soup) if footnote_map is not None else []

    fix_image_refs(soup, image_map, item.file_name)
    promote_title_elements(soup, css_heading_classes=css_heading_classes)
    clean_html_attrs(soup)
    md = html_to_markdown(str(body))

    # Remove chapter title duplicated as nested blockquotes or headings.
    # Done unconditionally so it runs even when CSS-detected h2 headings already exist.
    title_norm = re.sub(r'\W+', ' ', chapter_title).strip().lower()
    lines = md.split('\n')
    filtered = []
    for line in lines:
        if line.startswith('>') and re.sub(r'\W+', ' ', re.sub(r'^[>\s]+', '', line)).strip().lower() == title_norm:
            continue  # blockquote duplicate (Calibre-converted EPUBs)
        heading_m = re.match(r'^#{1,6}\s+(.+)', line)
        if heading_m:
            text = heading_m.group(1)
            if re.sub(r'\W+', ' ', text).strip().lower() == title_norm:
                continue  # heading duplicate at any level
            if re.match(r'^\d+\.?\s*$', text.strip()):
                continue  # bare chapter-number heading (e.g. "# 2") — artifact
        filtered.append(line)
    md = re.sub(r'\n{3,}', '\n\n', '\n'.join(filtered))

    # Demote any remaining h1 headings to h2 — they are sub-sections within the chapter
    # (e.g. EPUBs that use h1 for both chapter numbers and section headings).
    # Then set chapter_title as the sole h1 for the file.
    md = re.sub(r'^# (.+)', r'## \1', md, flags=re.MULTILINE)
    md = f'# {chapter_title}\n\n{md.lstrip()}'

    # Append footnote definitions for this chapter
    if refs and footnote_map:
        defs = []
        for num in sorted(set(refs), key=int):
            ftn_id = f'_ftn{num}'
            if ftn_id in footnote_map:
                fn_html = f'<body>{footnote_map[ftn_id]}</body>'
                fn_md = html_to_markdown(fn_html).strip()
                defs.append(f'[^{num}]: {fn_md}')
        if defs:
            md += '\n\n' + '\n'.join(defs) + '\n'

    pad = len(str(total))
    safe_title = sanitize_filename(chapter_title)
    filename = f'{str(index).zfill(pad)} - {safe_title}.md'
    if len(filename) > 120:
        filename = filename[:117].rstrip(' -') + '.md'

    (output_dir / filename).write_text(md, encoding='utf-8')
    return {'title': chapter_title, 'filename': filename}


def generate_toc_file(metadata, chapters, output_dir):
    frontmatter = {
        'title': metadata['title'],
        'author': metadata['author'],
        'publisher': metadata['publisher'],
        'year': metadata['year'],
        'read': False,
        'rating': None,
        'tags': ['book'],
    }

    yaml_str = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True,
                         default_flow_style=False)

    toc_lines = ['## Table of Contents', '']
    for ch in chapters:
        stem = Path(ch['filename']).stem
        toc_lines.append(f'- [[{stem}]]')

    body = '\n'.join(toc_lines)
    content = f'---\n{yaml_str}---\n\n{body}\n'

    safe_title = sanitize_filename(metadata['title'])
    toc_name = f'00 - {safe_title}.md'
    if len(toc_name) > 120:
        toc_name = toc_name[:117].rstrip(' -') + '.md'
    toc_path = output_dir / toc_name
    toc_path.write_text(content, encoding='utf-8')


def _read_epub_tolerant(path):
    """Read an EPUB, returning empty bytes for items missing from the ZIP archive.

    Some EPUBs list files in their manifest that were never included in the ZIP.
    Ebooklib raises KeyError in that case; this wrapper silently skips them.
    """
    original_read = zipfile.ZipFile.read

    def _tolerant_read(self, name, pwd=None):
        try:
            return original_read(self, name, pwd)
        except KeyError:
            print(f'Warning: missing item in EPUB archive: {name}', file=sys.stderr)
            return b''

    zipfile.ZipFile.read = _tolerant_read
    try:
        return epub.read_epub(str(path))
    finally:
        zipfile.ZipFile.read = original_read


def convert_epub(epub_path, overwrite=False):
    path = Path(epub_path)
    if not path.exists():
        print(f'Error: file not found: {epub_path}', file=sys.stderr)
        sys.exit(1)
    if path.suffix.lower() != '.epub':
        print(f'Error: not an EPUB file: {epub_path}', file=sys.stderr)
        sys.exit(1)

    book = _read_epub_tolerant(path)
    metadata = extract_metadata(book)

    safe_title = sanitize_filename(metadata['title'])
    output_dir = path.parent / safe_title

    if output_dir.exists() and not overwrite:
        print(f'  Skipping (already converted): {output_dir.name}')
        return 'skipped'

    output_dir.mkdir(exist_ok=True)

    assets_dir = output_dir / 'assets'
    image_map = extract_images(book, assets_dir)

    # Pre-scan footnote definitions across all EPUB documents
    footnote_map = build_footnote_map(book)

    css_heading_classes = build_css_heading_classes(book)

    flat_toc = _build_flat_toc(book)
    chapters = []

    if flat_toc:
        # TOC-driven: one markdown file per TOC entry (chapters, sections, sub-sections).
        # For each entry, find the next entry in the same file to bound HTML extraction.
        toc_sections = []
        for i, (title, file_href, anchor_id) in enumerate(flat_toc):
            next_anchor = None
            next_title = None
            for j in range(i + 1, len(flat_toc)):
                if flat_toc[j][1] == file_href:
                    next_anchor = flat_toc[j][2]
                    next_title = flat_toc[j][0]
                    break
                else:
                    break
            toc_sections.append((title, file_href, anchor_id, next_anchor, next_title))

        item_cache = {}

        def _get_item(href):
            if href not in item_cache:
                item = book.get_item_with_href(href)
                if item is None:
                    basename = href.split('/')[-1]
                    for candidate in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                        if candidate.file_name == href or candidate.file_name.endswith('/' + basename):
                            item = candidate
                            break
                item_cache[href] = item
            return item_cache[href]

        total = len(toc_sections)
        index = 1

        for toc_title, file_href, anchor_id, next_anchor, next_title in toc_sections:
            try:
                item = _get_item(file_href)
                if item is None:
                    print(f'Warning: EPUB item not found for: {file_href}', file=sys.stderr)
                    continue
                if hasattr(item, 'properties') and 'nav' in (item.properties or []):
                    continue

                body = _extract_section_html(item.get_content(), anchor_id, next_anchor,
                                             start_title=toc_title, end_title=next_title)
                if body is None or (not body.get_text(strip=True) and not body.find('img')):
                    continue

                result = _process_toc_section(
                    toc_title, body, file_href, output_dir, image_map,
                    index, total, footnote_map, css_heading_classes,
                )
                if result:
                    chapters.append(result)
                    index += 1
            except Exception as exc:
                print(f'Warning: skipping "{toc_title}": {exc}', file=sys.stderr)
    else:
        # Fallback: spine-based iteration for EPUBs with no TOC.
        toc_map = build_toc_map(book)
        candidate_items = []
        for item_id, linear in book.spine:
            if linear == 'no':
                continue
            item = book.get_item_with_id(item_id)
            if isinstance(item, epub.EpubHtml):
                candidate_items.append(item)

        total = len(candidate_items)
        index = 1
        for item in candidate_items:
            result = process_chapter(
                item, book, output_dir, image_map, index, total,
                footnote_map=footnote_map, toc_map=toc_map,
                css_heading_classes=css_heading_classes,
            )
            if result:
                chapters.append(result)
                index += 1

    generate_toc_file(metadata, chapters, output_dir)
    print(f'Done. Output: {output_dir}')


def _print_batch_report(results):
    """Print a summary table after batch conversion.

    results: list of (epub_name, status, warning_lines, error_msg)
      status: 'ok' | 'skipped' | 'error'
    """
    use_color = sys.stdout.isatty()
    RED    = '\033[31m' if use_color else ''
    YELLOW = '\033[33m' if use_color else ''
    GREEN  = '\033[32m' if use_color else ''
    BOLD   = '\033[1m'  if use_color else ''
    RESET  = '\033[0m'  if use_color else ''

    n_total   = len(results)
    n_ok      = sum(1 for _, s, w, _ in results if s == 'ok' and not w)
    n_warned  = sum(1 for _, s, w, _ in results if s == 'ok' and w)
    n_skipped = sum(1 for _, s, _, _ in results if s == 'skipped')
    n_errors  = sum(1 for _, s, _, _ in results if s == 'error')

    bar = '-' * 42
    print(f'\n{BOLD}{bar}{RESET}')
    print(f'{BOLD} Batch report — {n_total} file{"s" if n_total != 1 else ""}{RESET}')
    print(f'{BOLD}{bar}{RESET}')
    print(f' {GREEN}✓{RESET}  {n_ok + n_warned:<4} converted')
    if n_warned:
        print(f' {YELLOW}⚠{RESET}  {n_warned:<4} of those had warnings')
    print(f'    {n_skipped:<4} skipped (already converted)')
    if n_errors:
        print(f' {RED}✗{RESET}  {n_errors:<4} errors')
    print(f'{BOLD}{bar}{RESET}')

    warned_results = [(n, w) for n, s, w, _ in results if s == 'ok' and w]
    if warned_results:
        print(f'\n{YELLOW}Warnings:{RESET}')
        for name, warning_lines in warned_results:
            print(f'  {name}  ({len(warning_lines)})')

    error_results = [(n, e) for n, s, _, e in results if s == 'error']
    if error_results:
        print(f'\n{RED}Errors:{RESET}')
        for name, error_msg in error_results:
            print(f'  {RED}{name}{RESET} — {error_msg}')


def main():
    # Ensure stdout/stderr use UTF-8 on Windows consoles so accented titles
    # and Unicode report symbols are not mangled.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, 'reconfigure'):
            _stream.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(
        description='Convert an EPUB (or a folder of EPUBs) to Obsidian-compatible markdown files.'
    )
    parser.add_argument('path', help='Path to an EPUB file or a folder containing EPUB files')
    parser.add_argument('-o', '--overwrite', action='store_true',
                        help='Overwrite already-converted books (default: skip them)')
    args = parser.parse_args()

    try:
        pypandoc.get_pandoc_version()
    except OSError:
        print(
            'Error: pandoc is not installed or not found in PATH.\n'
            'Install it from https://pandoc.org/installing.html',
            file=sys.stderr
        )
        sys.exit(1)

    target = Path(args.path)

    if target.is_file():
        convert_epub(target, overwrite=args.overwrite)
    elif target.is_dir():
        epubs = sorted(target.glob('*.epub'))
        if not epubs:
            print(f'No EPUB files found in: {target}', file=sys.stderr)
            sys.exit(1)
        total = len(epubs)
        results = []  # (name, status, warning_lines, error_msg)

        class _Tee:
            """Write to both the real stderr and a capture buffer."""
            def __init__(self, real):
                self.real = real
                self.buf = io.StringIO()
            def write(self, text):
                self.real.write(text)
                self.buf.write(text)
            def flush(self):
                self.real.flush()

        for i, epub_path in enumerate(epubs, 1):
            print(f'[{i}/{total}] {epub_path.name}', flush=True)
            tee = _Tee(sys.stderr)
            sys.stderr = tee
            status = 'ok'
            error_msg = ''
            try:
                outcome = convert_epub(epub_path, overwrite=args.overwrite)
                if outcome == 'skipped':
                    status = 'skipped'
            except Exception as e:
                status = 'error'
                error_msg = str(e)
                print(f'  Error: {e}', file=tee.real)
            finally:
                sys.stderr = tee.real
            captured = tee.buf.getvalue()
            warning_lines = [l for l in captured.splitlines() if l.startswith('Warning:')]
            results.append((epub_path.name, status, warning_lines, error_msg))

        _print_batch_report(results)
    else:
        print(f'Error: path not found: {target}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

"""Materials: what the model is required to answer from.

An exam asks about the course, not about the subject in general. A model that answers
correctly but not with what was said in the lecture misses just as badly as one that
answers wrongly: the teacher hears someone else's words and someone else's order of
presentation. So a lecture, notes, or a study guide go into the system prompt in full,
and the prompt tells the model to stick to them.

**Why in full and not a search over chunks.** It is simpler and it works better. A
fifty-page lecture is about thirty thousand tokens, and the model's window is a million.
Cutting it into pieces and guessing which one to fetch for a question would add a
separate source of errors to save something nobody needs saved. The cache makes those
thirty thousand nearly free: they are written once and read at a tenth of the price.
Chunks will be needed when the materials grow to a whole course, and they will be
needed then.

**Why a folder and not a settings field.** A ten-megabyte PDF in the settings file
turns it into a ten-megabyte settings file, which is read at every start and shipped to
the browser in full every time the admin page opens. Files stay files, and the settings
only store what to do with them.

**Text or image.** What splits them is not the file format but the presence of a text
layer. Text is four times cheaper and more accurate: a page as an image costs about
fifteen hundred tokens against three to four hundred for the same page as letters. But
a scan or a photo has no text layer at all, and that does not make the lecture any less
a lecture. Such pages go to the model as images, and it reads them with its own eyes.

Recognizing them with our own OCR, which the core has, would be cheaper and worse. A
lecture page is not only letters: a diagram, a formula, a drawing, a derivation in the
margin. A recognizer returns lines and silently loses everything else, and the model
answers from half a page without knowing it saw only half.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from argus.core import friendly

log = logging.getLogger(__name__)

# A folder next to `argus.toml`. The person does not choose it: a "where the materials
# live" setting answers a question nobody asked, and it costs one more place where the
# path can drift from where the person put the file. Only a chat sets its own materials
# folder (`argus/chats/`), and it is the application, not the person, that does so:
# `channel.assist.materials.dir` in the chat settings, see `directory()`.
DIRNAME = "materials"

# What we can read as text. PDF because lectures are handed out in it; docx and pptx
# because that is what they are written in; plain text and markdown because that is what
# people keep notes in.
TEXT_SUFFIXES = (".pdf", ".docx", ".pptx", ".txt", ".md")

# What goes to the model as an image. A PDF without a text layer ends up here too.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")

SUFFIXES = TEXT_SUFFIXES + IMAGE_SUFFIXES

# Long side of a page sent as an image. The model would shrink anything larger by itself
# anyway, and we would be the ones paying to ship the extra megabytes.
MAX_SIDE = 1568

# How many times to enlarge a PDF page when rendering. An A4 page at 72 points per inch
# is 595 by 842; a 2x scale gives about 150 dpi, at which both the body text and the
# captions of diagrams are readable.
PDF_RENDER_SCALE = 2.0

JPEG_QUALITY = 82

# Where to put rendered pages. Inside the materials folder itself, not in a shared
# temporary directory: the pages belong to these files, and carrying them along with the
# files means copying one folder.
CACHE_DIRNAME = ".pages"

# Parsed results: (path, which kind) -> (file fingerprint, value). The fingerprint is the
# modification time and size: if the file is overwritten under the same name, parsing runs
# again. The key includes the kind of parse because one PDF yields both text and rendered
# pages, and they cost different amounts.
_cache: dict[tuple[Path, str], tuple[tuple[int, int], object]] = {}

# Parsing runs both in threads (`ui/admin.py`, `asyncio.to_thread`) and on the event loop
# (`install`), and cleaning the page cache of one parse would erase the pages another one
# has just written. So `load()` runs entirely under the lock; re-entering from the same
# thread (`install` -> `load`) does not block.
_LOCK = threading.RLock()

# Pages as images: no more than the setting itself allows (`Materials.max_pages`,
# `le=400`). Even when the limit is "not set" (0), rendering does not run off after
# hundreds of pages of a scanned book.
ABSOLUTE_MAX_PAGES = 400

# A PDF text layer poorer than this (characters per page) is a scan with a watermark or
# page numbers, not a lecture in letters: the pages go as images as well. The threshold is
# low on purpose: slides with a couple of lines per page also have a thin layer, but they
# are text, and forty pages as images instead of three thousand tokens of text would cost
# many times more on every question.
THIN_TEXT_CHARS_PER_PAGE = 40

# What share of replacement characters (U+FFFD) in the text read still counts as "a couple
# of broken letters" and not as noise instead of a file in another encoding.
MAX_BAD_SHARE = 0.02

# The share of bytes above 0x7F among letters at which single-byte text is read as
# Cyrillic: in Russian text that is almost all the letters, in Polish or French a few
# percent. Notes made of code and Russian words also stay Cyrillic, since there are plenty
# of words to spare.
CYRILLIC_MIN_SHARE = 0.25

# Reserve for the system prompt without the materials (profile, tone, rules:
# `assist/prompt.py`), when the model answers through the account server and its limit on
# the system part is shared.
RELAY_SYSTEM_RESERVE = 50_000

# Rendered pages on disk:
# `<file fingerprint>-l<how many were requested>-n<how many the file has>-<number>.jpg`.
# "How many were requested" is in the name because a cache rendered up to a limit of forty
# pages must not be handed out once the limit has been raised: it holds a stump.
_PAGE_NAME = re.compile(r"([0-9a-f]{16})-l(\d+)-n(\d+)-(\d{3})\.jpg")


@dataclass(slots=True)
class Item:
    """One materials file and what came of it."""

    name: str
    bytes: int
    chars: int = 0

    # How many pages went out as images. Not the same as the pages in the file: this is how
    # many fit under the limit.
    pages: int = 0
    error: str = ""


@dataclass(slots=True)
class Materials:
    """Everything that goes into the prompt, and what was paid for it."""

    text: str = ""

    # Pages as images, in JPEG. They go not into the system part, since an image cannot be put
    # there, but as the first blocks of the message, before the cache point.
    images: list[bytes] = field(default_factory=list)

    items: list[Item] = field(default_factory=list)

    # How many characters did not fit under the limit. If not zero, the model will not see
    # part of the lecture, and the person must know that before the exam, not from a strange
    # answer during it.
    dropped: int = 0

    # The same for pages: how many did not fit in `max_pages`.
    dropped_pages: int = 0

    @property
    def chars(self) -> int:
        return len(self.text)


def directory(cfg) -> Path:
    """The materials folder right now: the open chat has its own, otherwise the shared one.

    Read straight from the free-form `channel` section, not through `settings.read`: that
    parses the whole model and silently falls back to defaults on an invalid value, while
    here exactly one key is needed, and missing the folder that holds a person's files is
    worse than not noticing someone's typo in a neighboring field.
    """
    own = ((cfg.channel.get("assist") or {}).get("materials") or {}).get("dir")
    return cfg.resolve(Path(own if isinstance(own, str) and own else DIRNAME))


def load(cfg, max_chars: int = 0, max_pages: int = 0) -> Materials:
    """Collect the materials from the folder. The order is by file name.

    By name and not by modification time: lectures are numbered, and "01-intro" must come
    first today and tomorrow too. An order that depends on which file was touched last would
    change by itself, and the whole prompt cache would change with it.

    Text and images take two different paths, and what splits them is not the file format
    but the presence of a text layer. Text is four times cheaper than an image and more
    accurate, but a scan or a photo has none at all, and that does not make the lecture any
    less a lecture.

    Done under a lock: parsing is called from threads and from the event loop, and the page
    cache cleanup of two simultaneous parses of the same folder would erase each other's
    fresh pages.
    """
    with _LOCK:
        return _load(cfg, max_chars, max_pages)


def _relay_caps(cfg) -> tuple[int, int, int] | None:
    """Limits of the account server, when the model answers through it
    (`brain.provider = "relay"`).

    The server accepts no more than this (`server/relay.py`, `server/limits.py`): a question
    with anything extra would be refused there although the person broke no rule. We cut
    here, when reading the folder, so that the materials list in the settings (`dropped`,
    `dropped_pages`) and the prompt say the same thing. Returns (characters of text, pages,
    characters of base64 for the pages) or `None`.
    """
    if getattr(getattr(cfg, "brain", None), "provider", "") != "relay":
        return None
    from argus.brain import relay_api

    return (
        relay_api.MAX_SYSTEM_CHARS - RELAY_SYSTEM_RESERVE,
        relay_api.MAX_IMAGES - 1,                              # one slot for a screen frame or a photo
        relay_api.MAX_IMAGES_B64 - relay_api.MAX_IMAGE_B64,    # and a size reserve for that same slot
    )


def _load(cfg, max_chars: int, max_pages: int) -> Materials:
    out = Materials()
    folder = directory(cfg)
    if not folder.is_dir():
        return out

    budget = 0                      # characters of base64 for the pages; 0 means no limit
    caps = _relay_caps(cfg)
    if caps is not None:
        text_cap, page_cap, budget = caps
        max_chars = min(max_chars, text_cap) if max_chars else text_cap
        max_pages = min(max_pages, page_cap) if max_pages else page_cap

    files = [
        p for p in sorted(folder.iterdir(), key=lambda p: p.name.lower())
        if p.is_file() and p.suffix.lower() in SUFFIXES
    ]
    _sweep_cache(folder, {_stamp(p) for p in files})
    _forget_missing(folder, files)

    parts: list[str] = []
    spent = 0
    for path in files:
        item = Item(name=path.name, bytes=path.stat().st_size)
        out.items.append(item)
        # How many more pages will fit. There is no point rendering more: a six-hundred-page scan
        # would otherwise be rendered whole for the sake of forty, and a photo past the limit
        # would be decoded for nothing.
        room = max_pages - len(out.images) if max_pages else ABSOLUTE_MAX_PAGES
        if budget and spent >= budget:
            room = 0
        try:
            text, pages, total = _content_of(path, max(room, 0))
        except Exception as e:  # noqa: BLE001 — a broken file must not crash startup
            item.error = friendly.plain_or(e, "could not read the file")
            log.warning("material '%s' was not read: %s: %s", path.name, type(e).__name__, e)
            continue

        # We strip here and not in the readers: an empty file and a file of only spaces are the
        # same case, and it must be checked in one place, or the next new format will quietly
        # smuggle emptiness into the prompt.
        text = text.strip()
        if text:
            item.chars = len(text)
            parts.append(f"## {path.stem}\n{text}")
        taken: list[bytes] = []
        for page in pages:
            size = (len(page) + 2) // 3 * 4          # what the page turns into as base64
            if budget and spent + size > budget:
                break
            taken.append(page)
            spent += size
        if taken:
            item.pages = len(taken)
            out.images.extend(taken)
        out.dropped_pages += max(total - len(taken), 0)
        if not text and not taken:
            # Neither text nor pages: an empty file, or a format that produced nothing. Skipping it
            # silently would mean the person uploaded a lecture, saw it in the list, and answered the
            # whole exam without it.
            item.error = (
                "didn't fit — there are more pages than fit" if total
                else "empty — no text and no pages"
            )

    out.text = "\n\n".join(parts)
    if max_chars and len(out.text) > max_chars:
        out.dropped = len(out.text) - max_chars
        out.text = out.text[:max_chars]
    return out


def describe(m: Materials) -> str:
    """In one line: what was read, what was not, what did not fit.

    It lives here, not in the admin page and not in the prompt, because both need it: the
    log at startup and the block on the page. A second, similar enumerator would drift from
    the first on the first edit, showing one thing while the model received another.
    """
    parts = []
    for i in m.items:
        if i.error:
            parts.append(f"{i.name} ({i.error})")
        elif i.pages and i.chars:
            # The thin text layer of a scan: both it and the pages go out.
            parts.append(f"{i.name} — {i.chars} chars and {i.pages} pages as images")
        elif i.pages:
            parts.append(f"{i.name} — {i.pages} pages as images")
        else:
            parts.append(f"{i.name} — {i.chars} chars")
    if m.dropped:
        parts.append(f"{m.dropped} chars cut off")
    if m.dropped_pages:
        parts.append(f"{m.dropped_pages} pages didn't fit")
    return "; ".join(parts) if parts else "empty"


def _content_of(path: Path, room: int) -> tuple[str, list[bytes], int]:
    """What could be extracted from the file: text, pages as images, and how many pages in total
    would go as images. No more than `room` pages are returned; the rest are only counted."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        pages, total = _pages_cached(path, room, _photo_pages)
        return "", pages, total
    if suffix == ".pdf":
        text, count = _cached(path, "text", _read_pdf)
        if len(text) >= THIN_TEXT_CHARS_PER_PAGE * max(count, 1):
            return text, [], 0
        # There is no text layer or it is thin, so this is a scan or a lecture typeset as images.
        # We render the pages and give them to the model to see: that costs four times more, but
        # the choice here is not between expensive and cheap, it is between expensive and nothing.
        # The layer, if there is one, goes along too: a watermark and page numbers do no harm,
        # and real text, however short, is the only thing the model can read without the images.
        pages, total = _pages_cached(path, room, _render_pdf)
        return text, pages, total
    read = (_read_docx if suffix == ".docx" else _read_pptx if suffix == ".pptx" else _read_text)
    return _cached(path, "text", read), [], 0


def _cached(path: Path, kind: str, read):
    """Parse a file once, until it is touched.

    `install()` is called on every settings save, and parsing a fifty-page lecture takes a
    second, while rendering scans takes far more. A second inside a request handler that
    nobody paid for.
    """
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    hit = _cache.get((path, kind))
    if hit is not None and hit[0] == stamp:
        return hit[1]

    value = read(path)
    _cache[(path, kind)] = (stamp, value)
    return value


def _stamp(path: Path) -> str:
    """File fingerprint for the cache name: name, modification time, size."""
    stat = path.stat()
    raw = f"{path.name}|{stat.st_mtime_ns}|{stat.st_size}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def _pages_cached(path: Path, room: int, render) -> tuple[list[bytes], int]:
    """Pages of the file as images (the first `room` of them) and how many pages the file has
    in total.

    `render(path, limit)` returns (pages, how many the file has) and draws at most `limit`.
    Memory, then disk, then rendering; a cache made for a lower limit is no good for a
    higher one.
    """
    if room <= 0:
        return [], render(path, 0)[1]            # count only: no rendering, no cache
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    hit = _cache.get((path, "pages"))
    if hit is not None and hit[0] == stamp:
        rendered, total, pages = hit[1]
        if rendered >= room or len(pages) >= total:
            return pages[:room], total
    rendered, total, pages = _pages_from_disk(path, room, render)
    _cache[(path, "pages")] = (stamp, (rendered, total, pages))
    return pages[:room], total


def _pages_from_disk(path: Path, room: int, render) -> tuple[int, int, list[bytes]]:
    """Rendered pages come from disk, not afresh on every start.

    Memory does not help here: it lives until the program exits, and a forty-page scan takes
    seven seconds to render, which would be paid on **every** start. On disk the pages
    survive a restart, and an overwritten file gets a new fingerprint and is rendered again.
    Returns (up to how many pages it was rendered, how many pages the file has, the pages
    themselves).
    """
    folder = path.parent / CACHE_DIRNAME
    key = _stamp(path)
    groups: dict[tuple[int, int], list[Path]] = {}
    if folder.is_dir():
        for page in sorted(folder.glob(f"{key}-l*.jpg")):
            found = _PAGE_NAME.fullmatch(page.name)
            if found:
                groups.setdefault((int(found[2]), int(found[3])), []).append(page)
    for (rendered, total), files in groups.items():
        # A group is usable if it was rendered for at least the needed amount (or the file has no
        # more) and it is written to the end: a truncated record must not pass for a whole lecture.
        if (rendered >= room or total <= rendered) and len(files) == min(rendered, total):
            return rendered, total, [p.read_bytes() for p in files]

    pages, total = render(path, room)
    try:
        folder.mkdir(exist_ok=True)
        for stale in (p for files in groups.values() for p in files):
            stale.unlink(missing_ok=True)        # a stump for a lower limit, or an unfinished write
        for i, page in enumerate(pages):
            (folder / f"{key}-l{room}-n{total}-{i:03d}.jpg").write_bytes(page)
    except OSError as e:
        log.warning("pages of '%s' were not saved to the cache: %s", path.name, e)
    return room, total, pages


def _sweep_cache(folder: Path, keys: set[str]) -> None:
    """Remove the pages of files that no longer exist or were overwritten.

    Without cleanup the folder grows with every edit of a lecture: the fingerprint changes,
    the old pages stay behind, and a month later there is a gigabyte of images nobody reads.
    Files of a foreign shape (the earlier name format) go the same way.
    """
    cache = folder / CACHE_DIRNAME
    if not cache.is_dir():
        return
    for page in cache.glob("*.jpg"):
        found = _PAGE_NAME.fullmatch(page.name)
        if found is None or found[1] not in keys:
            try:
                page.unlink()
            except OSError:
                pass


def _forget_missing(folder: Path, files: list[Path]) -> None:
    """Forget what was parsed for files that are no longer in the folder: the memory lives as long as
    the program does, and a deleted lecture would sit in it until exit."""
    alive = set(files)
    for key in [k for k in _cache if k[0].parent == folder and k[0] not in alive]:
        del _cache[key]


def _decode(raw: bytes) -> str:
    """Text of a file: UTF-16 with a byte order mark, UTF-8 (with or without a mark), otherwise
    Windows-1251 (and single-byte Latin with diacritics is this computer's "ANSI",
    `_latin_codepage`).

    Russian notes from old Notepad are stored in "ANSI", which for us means Windows-1251,
    and its "Unicode" is UTF-16 with a mark. Everything used to be read as UTF-8 with
    replacement, and such a file turned into noise without a single error: the list showed
    "N chars", while the model received garbage.
    """
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    # Not clean UTF-8. If it is the same with a couple of broken bytes, we take it, as before:
    # half of the notes is better than nothing. If there are many "broken" ones, this is not
    # UTF-8 but a single-byte encoding, and it must not be read as UTF-8.
    lenient = raw.decode("utf-8-sig", errors="replace")
    if lenient.count("�") <= MAX_BAD_SHARE * len(lenient):
        return lenient
    cyrillic = raw.decode("cp1251", errors="replace")
    high = sum(b > 0x7F for b in raw)
    letters = sum(c.isalpha() for c in cyrillic)
    if letters and high < CYRILLIC_MIN_SHARE * letters and not re.search("[А-яЁё]{3}", cyrillic):
        # Latin with a few diacritics (Polish, French, Turkish), not Russian text: a Cyrillic code
        # page would turn "é" into the letter "й", and the list would show "N chars"
        # without a shadow of doubt. A Russian word (three letters in a row) among the Latin is a
        # sign of the opposite: English notes with Russian comments.
        return raw.decode(_latin_codepage(), errors="replace")
    return cyrillic


def _latin_codepage() -> str:
    """Code page for single-byte Latin with diacritics.

    This computer's "ANSI" (1250 on a Polish Windows, 1254 on a Turkish one), and where it
    says nothing (Python's UTF-8 mode, Mac, a Russian Windows), Western European 1252.
    """
    import locale

    try:
        name = codecs.lookup(locale.getpreferredencoding(False)).name
    except (LookupError, ValueError):
        return "cp1252"
    return "cp1252" if name in {"utf-8", "cp1251", "ascii"} else name


def _read_text(path: Path) -> str:
    text = _decode(path.read_bytes())
    # Noise instead of text is an error in the list, not "N chars": the person must
    # find out about it before the exam, not from a strange answer during it.
    if "\x00" in text or text.count("�") > MAX_BAD_SHARE * len(text):
        raise ValueError("could not detect the encoding — save the file as UTF-8")
    return text


def _read_pdf(path: Path) -> tuple[str, int]:
    """Text layer and page count: from them you can see whether the layer is sparse."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:  # noqa: BLE001 — one broken page does not cancel the lecture
            log.warning("page %d, in '%s': could not be parsed: %s", i, path.name, e)
    return "\n\n".join(p.strip() for p in pages if p.strip()), len(pages)


def _read_docx(path: Path) -> str:
    """Paragraphs and tables, in the same order as they stand in the document.

    We do not keep the markup: the model does not need it, and it costs tokens. Tables go
    as rows joined with " | ": in lectures on databases and networks half of the content is
    in them, and losing them would mean losing half. The order is the author's: a table
    comes after the paragraph that introduces it, not at the end of the file after all the
    paragraphs.
    """
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    parts: list[str] = []
    for child in doc.element.body.iterchildren():
        kind = child.tag.rsplit("}", 1)[-1]
        if kind == "p":
            text = Paragraph(child, doc).text.strip()
            if text:
                parts.append(text)
        elif kind == "tbl":
            for row in Table(child, doc).rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_pptx(path: Path) -> str:
    """Slides one by one, with the speaker notes.

    We take the notes because in lecture presentations the text is there: the slide has three
    words and a picture, and what is said about them is in the notes.
    """
    from pptx import Presentation

    out = []
    for n, slide in enumerate(Presentation(str(path)).slides, 1):
        lines = [
            shape.text_frame.text.strip()
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip()
        ]
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        if notes:
            lines.append(f"[notes] {notes}")
        if lines:
            out.append(f"### Slide {n}\n" + "\n".join(lines))
    return "\n\n".join(out)


def _render_pdf(path: Path, limit: int) -> tuple[list[bytes], int]:
    """Render the first `limit` pages of the PDF to JPEG (for scans without a text layer) and report
    how many pages the file has. `limit == 0` means only count."""
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(path))
    try:
        total = len(doc)
        return [
            _jpeg(doc[i].render(scale=PDF_RENDER_SCALE).to_pil())
            for i in range(min(total, limit))
        ], total
    finally:
        doc.close()


def _photo_pages(path: Path, limit: int) -> tuple[list[bytes], int]:
    """A photo is one "page"; it is not decoded until the cap leaves room for it."""
    if limit <= 0:
        return [], 1
    return [_jpeg(_open_image(path))], 1


def _open_image(path: Path):
    """Open a photo, rotating it according to the camera tag.

    Without the rotation, a lecture shot sideways on a phone would reach the model sideways:
    in the file it lies as the sensor captured it, and the "up" direction is recorded in a
    separate tag that only a viewer looks at.
    """
    from PIL import Image, ImageOps

    with Image.open(path) as img:
        return ImageOps.exif_transpose(img).convert("RGB")


def _jpeg(img) -> bytes:
    """Shrink to a reasonable size and compress. The model would shrink it further on its own anyway."""
    import io

    from PIL import Image

    if max(img.size) > MAX_SIDE:
        scale = MAX_SIDE / max(img.size)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()

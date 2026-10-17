"""
Adventure Time: Card Wars - print PNG images to A4 PDF

Install in the VS Code terminal:
    python -m pip install reportlab pillow

Run:
    python card_wars_pdf.py
"""

from collections import Counter
import html
from io import BytesIO
import math
import os
from pathlib import Path
import re
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from tkinter import filedialog, messagebox, ttk

from PIL import Image
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


# Dimensions are in millimeters. These are defaults, not verified official
# Card Wars dimensions. Measure your own cards and adjust them as needed.
PRESETS = {
    "Standard card": (63.0, 88.0),
    "Hero card": (76.0, 101.0),
    "Field card": (105.0, 130.0),
    "Custom size": (63.0, 88.0),
}

MM_TO_PT = 72.0 / 25.4

# Card Dweeb asks visitors not to scrape its hobby server once per card
# (https://www.carddweeb.com/Resources/Developers). Set names and IDs come
# from one Card Database request; full-size images come from the public
# GitHub image repository: github.com/itderrickh/CardWarsData
CARDDWEEB = "https://www.carddweeb.com"
DATABASE_URL = f"{CARDDWEEB}/CardDatabase"
DECKS_URL = f"{CARDDWEEB}/Decks"
OFFICIAL_DECKS_URL = f"{CARDDWEEB}/Decks/User?userId=0"
GITHUB_IMAGES = (
    "https://raw.githubusercontent.com/itderrickh/CardWarsData/master/images"
)
HTTP_HEADERS = {
    "User-Agent": "CardWarsPDF/1.0 (personal print helper)"
}

# Order matches the Card Database Set filter (values 0, 1, 2, ...).
SETS = [
    "Finn Vs Jake",
    "BMO Vs Lady Rainicorn",
    "Princess Bubblegum Vs Lumpy Space Princess",
    "Ice King Vs Marceline",
    "Lemon Grab Vs Gunter",
    "Fionna Vs Cake",
    "Doubles Tournament",
    "Hero Pack",
    "For The Glory",
    "Promo",
    "Kickstarter #1",
    "Flame Princess Vs Fern",
    "Prismo Vs The Lich",
    "Peppermint Butler Vs Magic Man",
    "Kickstarter #2",
    "DarkLands Expansion",
    "Custom Community Cards",
    "Land of Legends",
    "Kickstarter #3",
]

CONTAINER_RE = re.compile(
    r'<div[^>]*class="[^"]*card-container[^"]*"[^>]*>',
    re.IGNORECASE,
)
DECK_LINK_RE = re.compile(
    r'<a\b[^>]*aria-label="View deck:\s*([^"]+)"'
    r'[^>]*href="/Deck\?id=(\d+)"[^>]*>',
    re.IGNORECASE,
)
DECK_CARD_RE = re.compile(
    r'<a\b[^>]*class="[^"]*\bdeck-card\b[^"]*"'
    r'[^>]*data-card-id="(\d+)"[^>]*>',
    re.IGNORECASE,
)


def mm(value):
    """Convert millimeters to PDF points."""
    return value * MM_TO_PT


def _http_get(url, timeout=45):
    request = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _attr(tag, name):
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', tag, re.IGNORECASE)
    return html.unescape(match.group(1)) if match else ""


def parse_database(page_html):
    """Extract cards from the Card Database page's data-* attributes."""
    cards = []
    for tag in CONTAINER_RE.findall(page_html):
        card_id = _attr(tag, "data-id")
        name = _attr(tag, "data-name")
        if not card_id or not name:
            continue
        set_ids = []
        for token in _attr(tag, "data-set").split(","):
            match = re.fullmatch(r"#(\d+)#", token.strip())
            if match:
                set_ids.append(int(match.group(1)))
        cards.append({
            "id": int(card_id),
            "name": name,
            "image": _attr(tag, "data-card-image"),
            "custom": _attr(tag, "data-custom").lower() == "true",
            "set_ids": set_ids,
        })
    return cards


def resolve_set(query):
    """Resolve a set name to its (index, official name), ignoring case and spacing."""
    needle = " ".join(query.strip().split()).casefold()
    if not needle:
        return None
    exact = [
        (index, name) for index, name in enumerate(SETS)
        if name.casefold() == needle
    ]
    if exact:
        return exact[0]
    partial = [
        (index, name) for index, name in enumerate(SETS)
        if needle in name.casefold()
    ]
    if len(partial) == 1:
        return partial[0]
    return None


def cards_in_set(cards, set_index):
    matched = [card for card in cards if set_index in card["set_ids"]]
    matched.sort(key=lambda card: (card["name"].casefold(), card["id"]))
    return matched


def parse_deck_listing(page_html):
    """Extract deck names and IDs from a Card Dweeb deck listing."""
    decks = []
    for match in DECK_LINK_RE.finditer(page_html):
        decks.append({
            "id": int(match.group(2)),
            "name": html.unescape(match.group(1)).strip(),
        })
    return decks


def parse_deck_card_ids(page_html):
    """Extract every card ID from a deck, including repeated copies."""
    return [int(match.group(1)) for match in DECK_CARD_RE.finditer(page_html)]


def fetch_decks(progress=None):
    """Fetch available community and official decks from Card Dweeb."""
    decks_by_id = {}
    for source, url in (
        ("Community", DECKS_URL),
        ("Official", OFFICIAL_DECKS_URL),
    ):
        if progress:
            progress(f"Loading {source.lower()} decks...")
        page = _http_get(url).decode("utf-8", errors="replace")
        for deck in parse_deck_listing(page):
            deck["source"] = source
            decks_by_id[deck["id"]] = deck

    if not decks_by_id:
        raise ValueError("No decks were found on Card Dweeb.")

    name_counts = Counter(
        (deck["name"].casefold(), deck["source"])
        for deck in decks_by_id.values()
    )
    decks = []
    for deck in decks_by_id.values():
        label = f'{deck["name"]} [{deck["source"]}]'
        if name_counts[(deck["name"].casefold(), deck["source"])] > 1:
            label += f' (ID {deck["id"]})'
        deck["label"] = label
        decks.append(deck)
    return sorted(
        decks,
        key=lambda deck: (
            deck["source"] != "Official",
            deck["name"].casefold(),
            deck["id"],
        ),
    )


def safe_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
    return cleaned or "card"


def image_urls_for(card):
    filename = os.path.basename(card["image"].split("?")[0])
    folder = "custom" if card["custom"] else "regular"
    candidates = [
        f"{GITHUB_IMAGES}/{folder}/{filename}",
        f"{GITHUB_IMAGES}/upload/{filename}",
        f"{GITHUB_IMAGES}/regular/{filename}",
        f"{CARDDWEEB}/CardImages/{folder}/{filename}",
        f"{CARDDWEEB}/CardImages/upload/{filename}",
        f"{CARDDWEEB}/CardImages/regular/{filename}",
    ]
    unique = []
    seen = set()
    for url in candidates:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def download_card_image(card):
    last_error = None
    for url in image_urls_for(card):
        try:
            data = _http_get(url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            continue
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return data, ".png"
        if data[:2] == b"\xff\xd8":
            return data, ".jpg"
        last_error = ValueError("The response is not an image.")
    raise last_error or RuntimeError("No card image is available.")


def download_set_pack(set_query, zip_path, progress=None):
    """Save all cards in a set to a ZIP file and folder, returning their paths."""
    def report(message):
        if progress:
            progress(message)

    resolved = resolve_set(set_query)
    if not resolved:
        known = ", ".join(SETS)
        raise ValueError(
            f"Unknown set: {set_query!r}. Choose one of these:\n{known}"
        )
    set_index, set_name = resolved

    report("Downloading the Card Database...")
    page = _http_get(DATABASE_URL).decode("utf-8", errors="replace")
    cards = cards_in_set(parse_database(page), set_index)
    if not cards:
        raise ValueError(f"No cards were found for the {set_name} set.")

    folder = os.path.splitext(zip_path)[0]
    os.makedirs(folder, exist_ok=True)
    saved = []
    failed = []

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, card in enumerate(cards, start=1):
            report(f"{index}/{len(cards)}: {card['name']} (id={card['id']})")
            try:
                data, ext = download_card_image(card)
            except Exception as exc:
                failed.append(f"{card['name']} (ID {card['id']}): {exc}")
                continue
            filename = f"{card['id']:03d}_{safe_filename(card['name'])}{ext}"
            dest = os.path.join(folder, filename)
            with open(dest, "wb") as handle:
                handle.write(data)
            archive.write(dest, arcname=filename)
            saved.append(dest)
            time.sleep(0.08)

    report(f"Done: {len(saved)} images, {len(failed)} errors.")
    return set_name, saved, failed


def download_deck_pack(deck_id, deck_name, zip_path, progress=None):
    """Save every card copy in a deck to a ZIP file and folder."""
    def report(message):
        if progress:
            progress(message)

    report(f"Downloading the {deck_name} deck...")
    deck_page = _http_get(f"{CARDDWEEB}/Deck?id={deck_id}").decode(
        "utf-8", errors="replace"
    )
    card_ids = parse_deck_card_ids(deck_page)
    if not card_ids:
        raise ValueError(f"No cards were found in the {deck_name} deck.")

    report("Downloading the Card Database...")
    database_page = _http_get(DATABASE_URL).decode("utf-8", errors="replace")
    cards_by_id = {
        card["id"]: card for card in parse_database(database_page)
    }
    missing_ids = sorted(set(card_ids) - cards_by_id.keys())
    if missing_ids:
        missing = ", ".join(str(card_id) for card_id in missing_ids)
        raise ValueError(
            "These deck card IDs were not found in the Card Database: "
            f"{missing}"
        )

    unique_ids = list(dict.fromkeys(card_ids))
    image_data = {}
    image_errors = {}
    for index, card_id in enumerate(unique_ids, start=1):
        card = cards_by_id[card_id]
        report(
            f"Downloading image {index}/{len(unique_ids)}: {card['name']}"
        )
        try:
            image_data[card_id] = download_card_image(card)
        except Exception as exc:
            image_errors[card_id] = exc
        time.sleep(0.08)

    copy_counts = Counter(card_ids)
    copies_seen = Counter()
    folder = os.path.splitext(zip_path)[0]
    os.makedirs(folder, exist_ok=True)
    saved = []
    failed = []

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, card_id in enumerate(card_ids, start=1):
            card = cards_by_id[card_id]
            copies_seen[card_id] += 1
            copy_number = copies_seen[card_id]
            if card_id in image_errors:
                failed.append(
                    f"{card['name']} (copy {copy_number}): "
                    f"{image_errors[card_id]}"
                )
                continue

            data, ext = image_data[card_id]
            copy_suffix = (
                f"_copy{copy_number:02d}" if copy_counts[card_id] > 1 else ""
            )
            filename = (
                f"{card_id:03d}_{safe_filename(card['name'])}"
                f"{copy_suffix}{ext}"
            )
            dest = os.path.join(folder, filename)
            with open(dest, "wb") as handle:
                handle.write(data)
            archive.write(dest, arcname=filename)
            saved.append(dest)
            report(f"Saving card {index}/{len(card_ids)}: {card['name']}")

    report(f"Done: {len(saved)} card images, {len(failed)} errors.")
    return deck_name, saved, failed


def best_grid(page_w, page_h, margin, gap, card_w, card_h, allow_rotate=True):
    """Find the row and column layout that fits the most cards."""
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin
    if usable_w <= 0 or usable_h <= 0:
        return 0, 0

    best = (0, 0, 0)  # count, columns, rows
    orientations = (
        ((card_w, card_h), (card_h, card_w))
        if allow_rotate else ((card_w, card_h),)
    )
    for w, h in orientations:
        cols = int((usable_w + gap) // (w + gap))
        rows = int((usable_h + gap) // (h + gap))
        count = max(0, cols) * max(0, rows)
        if count > best[0]:
            best = (count, cols, rows)
    return best


def detect_card_size(path):
    """Infer a card preset and orientation from the source image proportions."""
    presets = (
        (63.0, 88.0, "standard"),
        (76.0, 101.0, "hero"),
        (105.0, 130.0, "field"),
    )
    with Image.open(path) as image:
        image_width, image_height = image.size
    if image_width <= 0 or image_height <= 0:
        raise ValueError(f"Could not read image dimensions for {path}.")

    landscape_card = image_width > image_height
    source_ratio = min(image_width, image_height) / max(image_width, image_height)
    normalized_name = re.sub(r"[^a-z0-9]", "", Path(path).stem.casefold())
    is_landscape_type = (
        source_ratio < 0.705
        or any(
            marker in normalized_name
            for marker in (
                "landscape",
                "plains",
                "cornfield",
                "swamp",
                "sandylands",
                "icyl",
                "lavaflats",
            )
        )
    )
    if landscape_card or is_landscape_type:
        card_width, card_height = 105.0, 130.0
        if landscape_card:
            card_width, card_height = card_height, card_width
        return card_width, card_height, "field"

    card_width, card_height, card_type = min(
        presets,
        key=lambda preset: abs(
            source_ratio - min(preset[0], preset[1]) / max(preset[0], preset[1])
        ),
    )
    expected_ratio = min(card_width, card_height) / max(card_width, card_height)
    if abs(source_ratio - expected_ratio) > 0.06:
        raise ValueError(
            f"Could not reliably detect the card type for {Path(path).name}. "
            "Turn off automatic sizing and choose a card type manually."
        )
    return card_width, card_height, card_type


def make_pdf(paths, output_path, card_w_mm, card_h_mm, margin_mm, gap_mm,
             orientation_name, crop_mode, cut_lines, quality_reduction=0,
             auto_detect=False):
    if quality_reduction not in (0, 20, 40, 60):
        raise ValueError("Image quality reduction must be 0%, 20%, 40%, or 60%.")

    page_size = A4 if orientation_name == "Portrait" else landscape(A4)
    page_w, page_h = page_size
    margin, gap = mm(margin_mm), mm(gap_mm)
    pdf = canvas.Canvas(output_path, pagesize=page_size)
    pdf.setTitle("Card Wars - printable cards")
    groups = {}
    if auto_detect:
        for path in paths:
            width_mm, height_mm, card_type = detect_card_size(path)
            groups.setdefault((width_mm, height_mm, card_type), []).append(path)
    else:
        groups[(card_w_mm, card_h_mm, "custom")] = paths

    total_pages = 0
    max_cards_per_page = 0
    first_grid = (0, 0)
    page_started = False
    for (width_mm, height_mm, _), group_paths in groups.items():
        card_w, card_h = mm(width_mm), mm(height_mm)
        count, cols, rows = best_grid(
            page_w, page_h, margin, gap, card_w, card_h,
            allow_rotate=not auto_detect,
        )
        if count < 1:
            raise ValueError(
                f"Cards sized {width_mm:g} x {height_mm:g} mm do not fit "
                "on this page. Reduce the margin or gap, or change orientation."
            )

        usable_w = page_w - 2 * margin
        usable_h = page_h - 2 * margin
        grid_w = cols * card_w + max(0, cols - 1) * gap
        grid_h = rows * card_h + max(0, rows - 1) * gap
        if not auto_detect and (
            grid_w > usable_w + 0.01 or grid_h > usable_h + 0.01
        ):
            card_w, card_h = card_h, card_w
            count, cols, rows = best_grid(
                page_w, page_h, margin, gap, card_w, card_h
            )
            grid_w = cols * card_w + max(0, cols - 1) * gap
            grid_h = rows * card_h + max(0, rows - 1) * gap

        if count < 1:
            raise ValueError("No cards fit on the selected A4 page.")
        if not page_started:
            first_grid = (cols, rows)
        else:
            pdf.showPage()
        page_started = True
        start_x = (page_w - grid_w) / 2
        start_y = (page_h + grid_h) / 2 - card_h
        total_pages += math.ceil(len(group_paths) / count)
        max_cards_per_page = max(max_cards_per_page, count)

        for index, path in enumerate(group_paths):
            slot = index % count
            if index > 0 and slot == 0:
                pdf.showPage()

            col = slot % cols
            row = slot // cols
            x = start_x + col * (card_w + gap)
            y = start_y - row * (card_h + gap)

            if cut_lines:
                pdf.setLineWidth(0.35)
                pdf.setStrokeColorRGB(0.55, 0.55, 0.55)
                pdf.rect(x, y, card_w, card_h, stroke=1, fill=0)

            if quality_reduction:
                with Image.open(path) as source:
                    source.load()
                    if source.mode in ("RGBA", "LA") or "transparency" in source.info:
                        rgba = source.convert("RGBA")
                        background = Image.new("RGB", rgba.size, "white")
                        background.paste(rgba, mask=rgba.getchannel("A"))
                        compressed = background
                    else:
                        compressed = source.convert("RGB")
                    image_data = BytesIO()
                    compressed.save(
                        image_data,
                        format="JPEG",
                        quality=100 - quality_reduction,
                        optimize=True,
                    )
                    image_data.seek(0)
                image = ImageReader(image_data)
            else:
                image = ImageReader(path)
            iw, ih = image.getSize()
            if iw <= 0 or ih <= 0:
                continue

            scale = (
                max(card_w / iw, card_h / ih)
                if crop_mode == "Fill (crop edges)"
                else min(card_w / iw, card_h / ih)
            )
            draw_w, draw_h = iw * scale, ih * scale
            dx = x + (card_w - draw_w) / 2
            dy = y + (card_h - draw_h) / 2

            pdf.saveState()
            if crop_mode == "Fill (crop edges)":
                clip = pdf.beginPath()
                clip.rect(x, y, card_w, card_h)
                pdf.clipPath(clip, stroke=0, fill=0)
            pdf.drawImage(
                image, dx, dy, width=draw_w, height=draw_h,
                preserveAspectRatio=True, mask="auto"
            )
            pdf.restoreState()

    pdf.save()
    return max_cards_per_page, total_pages, first_grid


class CardWarsApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Card Wars - A4 PDF Creator")
        self.root.geometry("760x860")
        self.root.minsize(700, 800)
        self.paths = []
        self._download_busy = False
        self.deck_choices = {}

        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        ttk.Label(
            outer, text="Adventure Time - Card Wars",
            font=("Segoe UI", 16, "bold")
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Button(
            outer, text="Choose images...", command=self.choose_files
        ).grid(row=1, column=0, sticky="w")
        self.files_label = ttk.Label(outer, text="No images selected.")
        self.files_label.grid(row=1, column=1, columnspan=2, sticky="w", padx=10)

        download = ttk.LabelFrame(
            outer, text="Download a set from the Card Dweeb database", padding=10
        )
        download.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(14, 4))
        download.columnconfigure(1, weight=1)

        ttk.Label(download, text="Set:").grid(row=0, column=0, sticky="w")
        self.set_var = tk.StringVar(value="Finn Vs Jake")
        ttk.Combobox(
            download, textvariable=self.set_var, values=SETS, width=42
        ).grid(row=0, column=1, sticky="w", padx=8)
        self.download_button = ttk.Button(
            download, text="Download set to ZIP...",
            command=self.download_set
        )
        self.download_button.grid(row=0, column=2, sticky="e")
        ttk.Label(
            download,
            text=(
                "The app finds every card in the selected set (for example, "
                "Finn Vs Jake), downloads the full-size images, and saves "
                "them to a ZIP file and folder."
            ),
            wraplength=660,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        deck_download = ttk.LabelFrame(
            outer, text="Download a deck from Card Dweeb", padding=10
        )
        deck_download.grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(10, 4)
        )
        deck_download.columnconfigure(1, weight=1)

        ttk.Label(deck_download, text="Deck:").grid(
            row=0, column=0, sticky="w"
        )
        self.deck_var = tk.StringVar()
        self.deck_box = ttk.Combobox(
            deck_download, textvariable=self.deck_var,
            state="readonly", width=39
        )
        self.deck_box.grid(row=0, column=1, sticky="ew", padx=8)
        self.load_decks_button = ttk.Button(
            deck_download, text="Load deck list", command=self.load_decks
        )
        self.load_decks_button.grid(row=0, column=2, sticky="e", padx=(0, 6))
        self.download_deck_button = ttk.Button(
            deck_download, text="Download deck to ZIP...",
            command=self.download_deck, state="disabled"
        )
        self.download_deck_button.grid(row=0, column=3, sticky="e")
        ttk.Label(
            deck_download,
            text=(
                "Loads community and official decks. Every copy in a deck "
                "is included as a separate image."
            ),
            wraplength=660,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        ttk.Label(outer, text="Card type:").grid(
            row=4, column=0, sticky="w", pady=(12, 4)
        )
        self.preset = tk.StringVar(value="Standard card")
        preset_box = ttk.Combobox(
            outer, textvariable=self.preset, values=list(PRESETS.keys()),
            state="readonly", width=25
        )
        preset_box.grid(row=4, column=1, sticky="w", pady=(12, 4))
        preset_box.bind("<<ComboboxSelected>>", self.apply_preset)

        self.width_var = tk.StringVar(value="63")
        self.height_var = tk.StringVar(value="88")
        self.margin_var = tk.StringVar(value="5")
        self.gap_var = tk.StringVar(value="2")
        self.orientation_var = tk.StringVar(value="Portrait")
        self.fit_var = tk.StringVar(value="Fit (show entire image)")
        self.compression_var = tk.StringVar(
            value="40% quality reduction (recommended)"
        )
        self.cut_var = tk.BooleanVar(value=True)

        fields = [
            ("Card width (mm):", self.width_var),
            ("Card height (mm):", self.height_var),
            ("A4 margin (mm):", self.margin_var),
            ("Gap between cards (mm):", self.gap_var),
        ]
        for i, (label, var) in enumerate(fields, start=5):
            ttk.Label(outer, text=label).grid(
                row=i, column=0, sticky="w", pady=5
            )
            ttk.Entry(outer, textvariable=var, width=12).grid(
                row=i, column=1, sticky="w", pady=5
            )

        ttk.Label(outer, text="Page orientation:").grid(
            row=9, column=0, sticky="w", pady=5
        )
        ttk.Combobox(
            outer, textvariable=self.orientation_var,
            values=["Portrait", "Landscape"], state="readonly", width=22
        ).grid(row=9, column=1, sticky="w", pady=5)

        ttk.Label(outer, text="Image fit:").grid(
            row=10, column=0, sticky="w", pady=5
        )
        ttk.Combobox(
            outer, textvariable=self.fit_var,
            values=[
                "Fit (show entire image)",
                "Fill (crop edges)"
            ],
            state="readonly", width=30
        ).grid(row=10, column=1, sticky="w", pady=5)

        ttk.Label(outer, text="Image compression:").grid(
            row=11, column=0, sticky="w", pady=5
        )
        ttk.Combobox(
            outer, textvariable=self.compression_var,
            values=[
                "100% quality (original images)",
                "20% quality reduction (high quality)",
                "40% quality reduction (recommended)",
                "60% quality reduction (smallest file)",
            ],
            state="readonly", width=30
        ).grid(row=11, column=1, sticky="w", pady=5)

        ttk.Checkbutton(
            outer, text="Show cut lines",
            variable=self.cut_var
        ).grid(row=12, column=0, columnspan=2, sticky="w", pady=(7, 2))

        note = (
            "The app creates multiple pages if all cards do not fit on one page. "
            "When printing, choose 100% / Actual size."
        )
        ttk.Label(outer, text=note, wraplength=660).grid(
            row=13, column=0, columnspan=3, sticky="w", pady=(12, 12)
        )

        ttk.Button(
            outer, text="Create PDF...", command=self.generate
        ).grid(row=14, column=0, sticky="w", pady=5)

        self.status = ttk.Label(outer, text="Ready.")
        self.status.grid(row=14, column=1, columnspan=2, sticky="w", padx=10)

    def choose_files(self):
        chosen = filedialog.askopenfilenames(
            title="Choose card images",
            filetypes=[("PNG images", "*.png"), ("All files", "*.*")]
        )
        if chosen:
            self.paths = list(chosen)
            self.files_label.config(text=f"Selected: {len(self.paths)} images")
            self.status.config(text="Images loaded.")

    def _set_download_controls(self, busy):
        state = "disabled" if busy else "normal"
        self.download_button.config(state=state)
        self.load_decks_button.config(state=state)
        deck_state = (
            "normal" if not busy and self.deck_choices else "disabled"
        )
        self.download_deck_button.config(state=deck_state)

    def load_decks(self):
        if self._download_busy:
            return

        self._download_busy = True
        self._set_download_controls(busy=True)
        self.status.config(text="Loading deck list...")

        def worker():
            try:
                decks = fetch_decks(
                    progress=lambda msg: self.root.after(
                        0, lambda m=msg: self.status.config(text=m)
                    )
                )
                self.root.after(0, lambda result=decks: self._decks_loaded(result))
            except Exception as exc:
                self.root.after(0, lambda error=exc: self._decks_loaded(None, error))

        threading.Thread(target=worker, daemon=True).start()

    def _decks_loaded(self, decks, error=None):
        self._download_busy = False
        if error:
            self._set_download_controls(busy=False)
            self.status.config(text="Failed to load decks.")
            messagebox.showerror("Deck list error", str(error))
            return

        self.deck_choices = {deck["label"]: deck for deck in decks}
        values = list(self.deck_choices)
        self.deck_box.config(values=values)
        self.deck_var.set(values[0] if values else "")
        self._set_download_controls(busy=False)
        self.status.config(text=f"Loaded {len(values)} decks.")

    def download_set(self):
        if self._download_busy:
            return
        set_query = self.set_var.get().strip()
        if not set_query:
            messagebox.showwarning("Set required", "Enter a set name.")
            return
        if not resolve_set(set_query):
            messagebox.showerror(
                "Unknown set",
                "This set name was not found. Choose one from the list, "
                "for example: Finn Vs Jake"
            )
            return

        slug = safe_filename(set_query).replace(" ", "_")
        output = filedialog.asksaveasfilename(
            title="Save ZIP file",
            defaultextension=".zip",
            filetypes=[("ZIP archive", "*.zip")],
            initialfile=f"{slug}_cards.zip"
        )
        if not output:
            return

        self._download_busy = True
        self._set_download_controls(busy=True)
        self.status.config(text="Starting download...")

        def worker():
            try:
                result = download_set_pack(
                    set_query, output,
                    progress=lambda msg: self.root.after(
                        0, lambda m=msg: self.status.config(text=m)
                    )
                )
                self.root.after(
                    0, lambda r=result: self._download_finished(
                        output, r, None, "set"
                    )
                )
            except Exception as exc:
                self.root.after(
                    0, lambda e=exc: self._download_finished(
                        output, None, e, "set"
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def download_deck(self):
        if self._download_busy:
            return
        selected = self.deck_choices.get(self.deck_var.get())
        if not selected:
            messagebox.showwarning(
                "No deck selected", "Load the deck list and choose a deck first."
            )
            return

        slug = safe_filename(selected["name"]).replace(" ", "_")
        output = filedialog.asksaveasfilename(
            title="Save ZIP file",
            defaultextension=".zip",
            filetypes=[("ZIP archive", "*.zip")],
            initialfile=f"{slug}_cards.zip"
        )
        if not output:
            return

        self._download_busy = True
        self._set_download_controls(busy=True)
        self.status.config(text="Starting download...")

        def worker():
            try:
                result = download_deck_pack(
                    selected["id"], selected["name"], output,
                    progress=lambda msg: self.root.after(
                        0, lambda m=msg: self.status.config(text=m)
                    )
                )
                self.root.after(
                    0, lambda r=result: self._download_finished(
                        output, r, None, "deck"
                    )
                )
            except Exception as exc:
                self.root.after(
                    0, lambda e=exc: self._download_finished(
                        output, None, e, "deck"
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def _download_finished(self, zip_path, result, error, pack_type):
        self._download_busy = False
        self._set_download_controls(busy=False)
        if error:
            self.status.config(text="Download failed.")
            messagebox.showerror("Download error", str(error))
            return

        pack_name, saved, failed = result
        self.paths = saved
        self.files_label.config(
            text=f"Selected: {len(self.paths)} images ({pack_name})"
        )
        folder = os.path.splitext(zip_path)[0]
        extra = ""
        if failed:
            extra = "\n\nFailed:\n" + "\n".join(failed[:12])
            if len(failed) > 12:
                extra += f"\n... and {len(failed) - 12} more cards."
        self.status.config(text=f"Downloaded: {len(saved)} card images.")
        pack_label = "Set" if pack_type == "set" else "Deck"
        messagebox.showinfo(
            f"{pack_label} downloaded",
            f"{pack_name}: {len(saved)} card images.\n\n"
            f"ZIP: {zip_path}\n"
            f"Folder: {folder}\n\n"
            "The images are now loaded in the PDF creator."
            f"{extra}"
        )

    def apply_preset(self, _event=None):
        w, h = PRESETS[self.preset.get()]
        self.width_var.set(str(w))
        self.height_var.set(str(h))

    def generate(self):
        if not self.paths:
            messagebox.showwarning(
                "No images", "Choose or download card images first."
            )
            return

        try:
            width = float(self.width_var.get().replace(",", "."))
            height = float(self.height_var.get().replace(",", "."))
            margin = float(self.margin_var.get().replace(",", "."))
            gap = float(self.gap_var.get().replace(",", "."))
            if min(width, height) <= 0 or min(margin, gap) < 0:
                raise ValueError(
                    "Card dimensions must be positive; margins and gaps "
                    "cannot be negative."
                )
        except ValueError as exc:
            messagebox.showerror(
                "Invalid settings", f"Check the dimensions.\n{exc}"
            )
            return

        output = filedialog.asksaveasfilename(
            title="Save PDF",
            defaultextension=".pdf",
            filetypes=[("PDF document", "*.pdf")],
            initialfile="card_wars_print.pdf"
        )
        if not output:
            return

        try:
            count, pages, grid = make_pdf(
                self.paths, output, width, height, margin, gap,
                self.orientation_var.get(), self.fit_var.get(), self.cut_var.get(),
                {"100% quality (original images)": 0,
                 "20% quality reduction (high quality)": 20,
                 "40% quality reduction (recommended)": 40,
                 "60% quality reduction (smallest file)": 60}[
                     self.compression_var.get()
                 ],
            )
        except Exception as exc:
            messagebox.showerror("PDF creation error", str(exc))
            return

        self.status.config(
            text=f"Done: {pages} pages, about {count} cards per page."
        )
        messagebox.showinfo(
            "PDF created",
            f"The PDF was created successfully:\n{output}\n\n"
            f"Images: {len(self.paths)}\n"
            f"Pages: {pages}\n"
            f"Layout: {grid[0]} columns x {grid[1]} rows "
            f"(up to {count} images per page)"
        )


if __name__ == "__main__":
    root = tk.Tk()
    app = CardWarsApp(root)
    root.mainloop()

const form = document.querySelector("#pdf-form");
const input = document.querySelector("#images");
const dropZone = document.querySelector("#drop-zone");
const previews = document.querySelector("#previews");
const selection = document.querySelector("#selection");
const status = document.querySelector("#status");
const createButton = document.querySelector("#create-button");
const preset = document.querySelector("#preset");
const setSelect = document.querySelector("#set-select");
const deckSelect = document.querySelector("#deck-select");
const loadDecksButton = document.querySelector("#load-decks");
const downloadSetButton = document.querySelector("#download-set");
const downloadDeckButton = document.querySelector("#download-deck");
const cardDweebStatus = document.querySelector("#carddweeb-status");
const autoDetect = document.querySelector("#auto-detect");
const cardTypeControls = [
  document.querySelector("#preset"),
  document.querySelector("#width"),
  document.querySelector("#height"),
];
const maxLocalUploadBytes = 256 * 1024 * 1024;
let selectedFiles = [];
let previewUrls = [];
let selectedPacks = [];
let deckChoices = new Map();
let cardDweebBusy = false;

const presets = {
  standard: [63, 88],
  hero: [76, 101],
  field: [105, 130],
};

function renderSelection() {
  previewUrls.forEach((url) => URL.revokeObjectURL(url));
  previewUrls = [];
  previews.replaceChildren();

  const downloadedCount = selectedPacks.reduce((count, pack) => count + pack.images, 0);
  const totalCount = selectedFiles.length + downloadedCount;
  if (!totalCount) {
    selection.textContent = "No images selected yet.";
    return;
  }

  selection.textContent = `${totalCount} image${totalCount === 1 ? "" : "s"} ready to print.`;
  let shown = 0;
  selectedFiles.slice(0, 16).forEach((file) => {
    const url = URL.createObjectURL(file);
    previewUrls.push(url);
    const image = document.createElement("img");
    image.className = "preview";
    image.src = url;
    image.alt = file.name;
    image.title = file.name;
    previews.append(image);
    shown += 1;
  });

  for (const pack of selectedPacks) {
    for (let index = 0; index < pack.images && shown < 16; index += 1) {
      const image = document.createElement("img");
      image.className = "preview";
      image.src = `/api/packs/${pack.token}/images/${index}`;
      image.alt = `${pack.name} card ${index + 1}`;
      image.title = image.alt;
      previews.append(image);
      shown += 1;
    }
  }
  if (totalCount > shown) {
    const more = document.createElement("span");
    more.className = "selection";
    more.textContent = `+${totalCount - shown} more`;
    previews.append(more);
  }
}

function showFiles(files) {
  selectedFiles = Array.from(files).filter((file) => file.type.startsWith("image/"));
  renderSelection();
}

input.addEventListener("change", () => showFiles(input.files));

preset.addEventListener("change", () => {
  const dimensions = presets[preset.value];
  if (dimensions) {
    document.querySelector("#width").value = dimensions[0];
    document.querySelector("#height").value = dimensions[1];
  }
});

["dragenter", "dragover"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("drag-over");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("drag-over");
  });
});

dropZone.addEventListener("drop", (event) => showFiles(event.dataTransfer.files));

autoDetect.addEventListener("change", () => {
  cardTypeControls.forEach((control) => {
    control.disabled = autoDetect.checked;
  });
});

async function readJsonResponse(response) {
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error(`Request failed (${response.status}).`);
  }
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
  return result;
}

function setCardDweebBusy(busy, message = "") {
  cardDweebBusy = busy;
  setSelect.disabled = busy;
  deckSelect.disabled = busy || deckChoices.size === 0;
  loadDecksButton.disabled = busy;
  downloadSetButton.disabled = busy;
  downloadDeckButton.disabled = busy || !deckChoices.has(deckSelect.value);
  cardDweebStatus.textContent = message;
  cardDweebStatus.classList.toggle("error", false);
}

async function initializeSets() {
  try {
    const response = await fetch("/api/sets");
    const result = await readJsonResponse(response);
    setSelect.replaceChildren(...result.sets.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      return option;
    }));
  } catch (error) {
    setCardDweebBusy(false, error.message);
    cardDweebStatus.classList.add("error");
  }
}

loadDecksButton.addEventListener("click", async () => {
  if (cardDweebBusy) return;
  setCardDweebBusy(true, "Loading official and community decks from Card Dweeb…");
  try {
    const response = await fetch("/api/decks");
    const result = await readJsonResponse(response);
    deckChoices = new Map(result.decks.map((deck) => [String(deck.id), deck]));
    deckSelect.replaceChildren(...result.decks.map((deck) => {
      const option = document.createElement("option");
      option.value = String(deck.id);
      option.textContent = deck.label;
      return option;
    }));
    deckSelect.disabled = deckChoices.size === 0;
    setCardDweebBusy(false, `Loaded ${deckChoices.size} decks.`);
  } catch (error) {
    setCardDweebBusy(false, error.message);
    cardDweebStatus.classList.add("error");
  }
});

deckSelect.addEventListener("change", () => {
  downloadDeckButton.disabled = cardDweebBusy || !deckChoices.has(deckSelect.value);
});

async function downloadCardPack(endpoint, payload) {
  if (cardDweebBusy) return;
  setCardDweebBusy(true, "Contacting Card Dweeb and downloading card images…");
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const pack = await readJsonResponse(response);
    selectedPacks.push(pack);
    renderSelection();

    const archiveLink = document.createElement("a");
    archiveLink.href = `/api/packs/${pack.token}/archive`;
    archiveLink.download = `${pack.name.replace(/[<>:"/\\|?*]/g, "_")}_cards.zip`;
    document.body.append(archiveLink);
    archiveLink.click();
    archiveLink.remove();

    let message = `${pack.name}: ${pack.images} images loaded and ZIP download started.`;
    if (pack.failures.length) {
      const shown = pack.failures.slice(0, 3).join("; ");
      const remaining = pack.failures.length - Math.min(3, pack.failures.length);
      message += ` ${pack.failures.length} image${pack.failures.length === 1 ? "" : "s"} failed: ${shown}`;
      if (remaining) message += `; and ${remaining} more.`;
    }
    setCardDweebBusy(false, message);
  } catch (error) {
    setCardDweebBusy(false, error.message);
    cardDweebStatus.classList.add("error");
  }
}

downloadSetButton.addEventListener("click", () => {
  if (setSelect.value) {
    downloadCardPack("/api/download-set", { set: setSelect.value });
  }
});

downloadDeckButton.addEventListener("click", () => {
  const deck = deckChoices.get(deckSelect.value);
  if (deck) {
    downloadCardPack("/api/download-deck", { id: deck.id, name: deck.name });
  }
});

initializeSets();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  status.classList.remove("error");
  if (!selectedFiles.length && !selectedPacks.length) {
    status.textContent = "Choose or download at least one card image first.";
    status.classList.add("error");
    return;
  }
  if (!form.reportValidity()) return;
  const localUploadBytes = selectedFiles.reduce((total, file) => total + file.size, 0);
  if (localUploadBytes > maxLocalUploadBytes) {
    status.textContent = "Selected local images exceed the 256 MB upload limit. Split them into smaller PDFs or download the set/deck through Card Dweeb to print server-side.";
    status.classList.add("error");
    return;
  }

  const settings = {
    width: Number(document.querySelector("#width").value),
    height: Number(document.querySelector("#height").value),
    margin: Number(document.querySelector("#margin").value),
    gap: Number(document.querySelector("#gap").value),
    orientation: document.querySelector("#orientation").value,
    fit: document.querySelector("#fit").value,
    cut_lines: document.querySelector("#cut-lines").checked,
    quality_reduction: Number(form.elements.quality.value),
    auto_detect: autoDetect.checked,
  };

  createButton.disabled = true;
  status.textContent = "Creating your PDF. Large decks may take a little while…";
  try {
    let response;
    if (!selectedFiles.length) {
      response = await fetch("/create-pdf-from-packs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          settings,
          pack_tokens: selectedPacks.map((pack) => pack.token),
        }),
      });
    } else {
      const payload = new FormData();
      payload.append("settings", JSON.stringify(settings));
      payload.append(
        "pack_tokens",
        JSON.stringify(selectedPacks.map((pack) => pack.token)),
      );
      selectedFiles.forEach((file) => payload.append("images", file, file.name));
      response = await fetch("/create-pdf", { method: "POST", body: payload });
    }
    if (!response.ok) {
      let message = `PDF creation failed (${response.status}).`;
      try {
        const result = await response.json();
        if (result.error) message = result.error;
      } catch {
        // Keep the HTTP error message when the server response is not JSON.
      }
      throw new Error(message);
    }
    const pdf = await response.blob();
    const url = URL.createObjectURL(pdf);
    const link = document.createElement("a");
    link.href = url;
    link.download = "card_wars_print.pdf";
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    status.textContent = `PDF ready (${(pdf.size / (1024 * 1024)).toFixed(1)} MB).`;
  } catch (error) {
    status.textContent = error instanceof TypeError
      ? "Could not reach the local PDF server. It may have stopped; restart card_wars_web.py and retry."
      : error.message || "Could not create the PDF.";
    status.classList.add("error");
  } finally {
    createButton.disabled = false;
  }
});

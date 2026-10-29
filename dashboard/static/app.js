"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const setText = (id, value) => { byId(id).textContent = value; };
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const absent = "Unavailable";
  let refreshing = false;
  let refreshPromise = null;
  let lastSuccess = null;
  let files = new Map();
  let selectedRecording = "";
  let selectedRecordingUrl = "";
  let latestStatus = null;
  let session = { configured: false, authenticated: false, csrf_token: null };
  let sessionKnown = false;
  let settingsDirty = false;
  let mutationBusy = false;
  let stationIdProposal = null;
  let stationIdError = "";
  let dtmfProposal = null;
  let dtmfError = "";
  let dtmfCodeError = "";
  let dtmfFormCsrf = null;
  let wifiVisibilityProposal = null;
  let wifiVisibilityBusy = false;
  let wifiVisibilityError = "";
  let wifiVisibilityAttempt = null;
  let softwareUpdateProposal = null;
  let softwareUpdateBusy = false;
  let softwareUpdateError = "";
  let softwareUpdateAttempt = null;
  let softwareUpdateCheck = null;
  let softwareUpdateAction = null;
  let softwareUpdateRevision = 0;
  let softwareCheckTimer = null;
  let softwareCheckController = null;
  let softwareCheckGeneration = 0;
  let softwareCheckPageActive = true;
  let radioProgrammingProposal = null;
  let radioProgrammingBusy = false;
  let radioProgrammingError = "";
  let radioProgrammingAttempt = null;
  let radioProgressRefreshing = false;
  let radioPairProposal = null;
  let radioPairError = "";
  let radioPairAttempt = null;
  let radioToolsMode = null;
  let dtmfPasswordMode = null;
  let radioMessageSelection = "";
  let radioMessageListKey = null;
  let radioMessagePreviewKey = "";
  let radioMessageProposal = null;
  let radioMessageError = "";
  let radioMessageActionBusy = false;
  let radioMessageRefreshing = false;
  let radioMessageUncertain = false;
  let activityTimer = null;
  let activityController = null;
  let activityGeneration = 0;
  let activityRunning = false;
  let activityLastPoll = null;
  let dataTimer = null;
  let dataController = null;
  let dataRequestForced = false;
  let dataGeneration = 0;
  let dataPageActive = true;
  let dataBusy = false;
  let dataError = "";
  let dataPacketKey = null;
  let dataSessionToken = null;
  let dataRevision = 0;
  let sstvBusy = false;
  let sstvError = "";
  let sstvGalleryKey = null;
  let sstvPreviewKey = "";
  let sstvSelectedId = "";
  let mediaDeleteProposal = null;
  let mediaDeleteBusy = false;
  let mediaDeleteUncertain = false;
  let mediaRevision = 0;
  const mediaDeleteMessages = { recording: "", image: "" };
  const mediaSelections = {recording:new Map(),image:new Map()};
  const mediaSelectionModes = {recording:false,image:false};
  let mediaSelectionCsrf = null;
  let recordingBulkKey = "";
  let audioContext = null;
  let audioGain = null;
  let liveController = null;
  let nextAudioTime = 0;
  const audioNodes = new Set();
  let liveState = "stopped";
  let liveGeneration = 0;
  const tabNames = ["overview", "live", "data", "archives", "settings"];
  const tabCopy = {
    overview: ["Station overview", "Pi health, radio interfaces, and your portable station at a glance."],
    live: ["Live audio", "Radio audio, directly over the box’s Wi-Fi."],
    data: ["Data", "Receive packets and pictures over the box’s radio input."],
    archives: ["Audio archives", "Control traffic recording and listen to saved radio audio."],
    settings: ["Settings / Admin", "Manage the dashboard and radio audio service on your Pi."]
  };

  function formatBytes(bytes, decimals = 1) {
    if (!finite(bytes) || bytes < 0) return absent;
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : decimals)} ${units[index]}`;
  }

  function formatUptime(seconds) {
    if (!finite(seconds) || seconds < 0) return absent;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 1) return "<1 min";
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ${minutes % 60}m`;
    return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  }

  function localTime(value, includeDate = false) {
    if (typeof value !== "string") return absent;
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return absent;
    return date.toLocaleString(undefined, includeDate
      ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
      : { hour: "numeric", minute: "2-digit", second: "2-digit" });
  }

  function textOrAbsent(value) {
    return typeof value === "string" && value.trim() ? value : absent;
  }

  function pill(id, label, kind = "muted") {
    const element = byId(id);
    element.textContent = label;
    element.className = `pill pill-${kind}`;
  }

  function renderPower(power) {
    const status = byId("power-status");
    status.className = "metric-value text-value";
    if (!power || power.available !== true) {
      status.textContent = absent;
      setText("power-note", "Pi voltage flags unavailable; battery level is not measured.");
      return;
    }
    if (power.under_voltage_now === true) {
      status.textContent = "Low voltage";
      status.classList.add("warning-text");
      setText("power-note", "The Pi is reporting undervoltage now.");
    } else if (power.throttled_now === true) {
      status.textContent = "Throttled";
      status.classList.add("warning-text");
      setText("power-note", "The Pi is reporting throttling now.");
    } else if (power.under_voltage_now === false && power.throttled_now === false) {
      status.textContent = "No current flags";
      status.classList.add("good-text");
      setText("power-note", power.under_voltage_occurred === true
        ? "Undervoltage occurred earlier in this boot."
        : "Pi voltage flags only; battery level is not measured.");
    } else {
      status.textContent = absent;
      setText("power-note", "Pi voltage flags incomplete; battery level is not measured.");
    }
  }

  function renderAudioDevices(id, devices, kind) {
    const list = byId(id);
    list.replaceChildren();
    if (!Array.isArray(devices) || devices.length === 0) {
      const empty = document.createElement("li");
      empty.className = "empty-item";
      empty.textContent = !Array.isArray(devices) ? "Device inventory unavailable." : kind === "serial" ? "No serial / PTT ports detected." : "No audio devices detected.";
      list.append(empty);
      return;
    }
    for (const device of devices) {
      const item = document.createElement("li");
      const name = document.createElement("strong");
      const detail = document.createElement("small");
      if (kind === "serial") {
        name.textContent = textOrAbsent(device.path);
        detail.textContent = textOrAbsent(device.target);
      } else {
        name.textContent = textOrAbsent(device.name);
        detail.textContent = `Card ${finite(device.card) ? device.card : "?"} · ${textOrAbsent(device.id)}`;
      }
      item.append(name, detail);
      list.append(item);
    }
  }

  function recordingUrl(value) {
    if (typeof value !== "string" || !value.startsWith("/recordings/") || value.startsWith("//")) return null;
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin && url.pathname.startsWith("/recordings/") ? url.href : null;
    } catch (_) { return null; }
  }

  function selectRecording(id) {
    const recording = files.get(id);
    const audio = byId("recording-audio");
    const url = recording ? recordingUrl(recording.url) : null;
    selectedRecording = recording && url ? id : "";
    byId("recording-player").hidden = !selectedRecording || mediaSelectionModes.recording;
    renderMediaDeleteControls();
    if (url === selectedRecordingUrl) return;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    selectedRecordingUrl = url || "";
    if (!selectedRecording) return;
    setText("recording-name", textOrAbsent(recording.name));
    setText("recording-detail", `${localTime(recording.modified_at, true)} · ${formatBytes(recording.bytes)}`);
    audio.src = url;
    const download = byId("recording-download");
    download.href = url;
    download.download = typeof recording.name === "string" ? recording.name : "recording";
  }

  function renderRecordings(recordings) {
    const inventory = Array.isArray(recordings?.files) ? recordings.files : null;
    const selectable = inventory ? inventory.filter((file) => typeof file.id === "string" && recordingUrl(file.url)) : [];
    files = new Map(selectable.map((file) => [file.id, file]));
    const count = finite(recordings?.count) ? recordings.count : null;
    setText("recording-summary", count === null ? "Inventory unavailable" : `${count} ${count === 1 ? "recording" : "recordings"} · ${formatBytes(recordings.total_bytes)}`);
    byId("recordings-empty").hidden = selectable.length > 0;
    byId("recording-workspace").hidden = selectable.length === 0;
    setText("recordings-empty-text", inventory === null ? "Recording inventory unavailable." : count > 0 ? "No playable files are listed in the current inventory." : "No recordings yet. Saved traffic recordings will appear here.");
    const select = byId("recording-select");
    select.replaceChildren(new Option("Select a recording…", ""));
    for (const file of selectable) {
      select.add(new Option(`${textOrAbsent(file.name)} · ${formatBytes(file.bytes)}`, file.id));
    }
    if (files.has(selectedRecording)) select.value = selectedRecording;
    else selectRecording("");
    renderMediaDeleteControls();
  }

  function mediaAdmin() {
    return sessionKnown && session.authenticated && typeof session.csrf_token === "string" && !!session.csrf_token;
  }

  function mediaEntry(kind) {
    return kind === "recording" ? files.get(selectedRecording)
      : sstvImages(latestStatus?.sstv).find(image => image.id === sstvSelectedId);
  }

  function mediaEntryRevision(kind, entry) {
    const value = kind === "recording" ? entry?.delete_revision : entry?.revision;
    return typeof value === "string" && /^[0-9a-f]{64}$/.test(value) ? value : null;
  }

  function mediaEntryDeletable(kind, entry) {
    if (!entry || !mediaEntryRevision(kind,entry) || entry.can_delete === false) return false;
    if (kind === "recording") {
      return typeof entry.id === "string" && entry.id.length <= 255 && !/^[.]|[\\/\x00-\x1f]/.test(entry.id) &&
        /\.(wav|ogg|flac|mp3)$/i.test(entry.id) && finite(entry.bytes) && entry.bytes >= 0 &&
        latestStatus?.recorder?.current_file !== entry.id && latestStatus?.recorder?.current_file !== `${entry.id}.part`;
    }
    return sstvId(entry.id) && finite(entry.size_bytes) && entry.size_bytes >= 0 && !(latestStatus?.sstv?.phase === "receiving" && latestStatus.sstv.current?.id === entry.id);
  }

  function mediaPanelVisible(kind) {
    return !document.hidden && byId(kind === "recording" ? "panel-archives" : "panel-data").hidden === false;
  }

  function mediaSnapshot(kind, entry) {
    if (!mediaEntryDeletable(kind,entry)) return null;
    return JSON.stringify([entry.id,mediaEntryRevision(kind,entry),kind === "recording" ? entry.name : entry.mode,
      kind === "recording" ? entry.modified_at : entry.created_at,kind === "recording" ? entry.bytes : entry.size_bytes,
      kind === "image" ? entry.complete : null,kind === "image" ? entry.lines_received : null]);
  }

  function canDeleteMedia(kind) {
    return mediaAdmin() && mediaPanelVisible(kind) && !mediaSelectionModes[kind] && !mutationBusy && !mediaDeleteBusy && !mediaDeleteUncertain &&
      mediaEntryDeletable(kind,mediaEntry(kind));
  }

  function mediaInventory(kind) {
    return kind === "recording" ? [...files.values()] : sstvImages(latestStatus?.sstv);
  }

  function mediaBulkLimit(kind) { return kind === "recording" ? 100 : 50; }
  function mediaPrefix(kind) { return kind === "recording" ? "recording" : "sstv"; }
  function mediaMutationAllowed(kind) {
    return mediaSelectionModes[kind] && mediaAdmin() && mediaPanelVisible(kind) && !mutationBusy && !mediaDeleteBusy && !mediaDeleteUncertain;
  }

  function setMediaSelectionMode(kind,enabled) {
    if (enabled && (!mediaAdmin() || !mediaPanelVisible(kind) || mutationBusy || mediaDeleteBusy || mediaDeleteUncertain ||
        !mediaInventory(kind).some(entry => mediaEntryDeletable(kind,entry)))) return;
    if (mediaDeleteProposal?.kind === kind) closeMediaDeleteConfirmation(false);
    mediaSelectionModes[kind] = enabled;
    mediaSelections[kind].clear();
    if (enabled && kind === "recording") byId("recording-audio").pause();
    renderMediaDeleteControls();
    const focus = byId(`${mediaPrefix(kind)}-${enabled ? "selection-cancel" : "select-multiple"}`);
    if (!focus.hidden && !focus.disabled) focus.focus();
  }

  function mediaProposalCurrent(proposal) {
    if (!mediaAdmin() || session.csrf_token !== proposal.csrf || !mediaPanelVisible(proposal.kind)) return false;
    if (proposal.bulk && !mediaSelectionModes[proposal.kind]) return false;
    if (!proposal.bulk) return mediaSnapshot(proposal.kind,mediaEntry(proposal.kind)) === proposal.snapshot;
    const inventory = new Map(mediaInventory(proposal.kind).map(entry => [entry.id,entry]));
    return mediaSelections[proposal.kind].size === proposal.items.length && proposal.items.every(item =>
      mediaSelections[proposal.kind].get(item.id) === item.snapshot &&
      mediaSnapshot(proposal.kind,inventory.get(item.id)) === item.snapshot);
  }

  function setMediaSelected(kind,id,checked) {
    if (!mediaMutationAllowed(kind)) {
      if (mediaDeleteProposal?.bulk && mediaDeleteProposal.kind === kind) closeMediaDeleteConfirmation(false);
      renderMediaDeleteControls();return;
    }
    const entry = mediaInventory(kind).find(item => item.id === id), snapshot = mediaSnapshot(kind,entry);
    if (checked && snapshot && mediaSelections[kind].size < mediaBulkLimit(kind)) mediaSelections[kind].set(id,snapshot);
    else if (!checked) mediaSelections[kind].delete(id);
    if (mediaDeleteProposal?.bulk && mediaDeleteProposal.kind === kind) closeMediaDeleteConfirmation(false);
    renderMediaDeleteControls();
  }

  function selectAllMedia(kind,checked) {
    if (!mediaMutationAllowed(kind)) {
      if (mediaDeleteProposal?.bulk && mediaDeleteProposal.kind === kind) closeMediaDeleteConfirmation(false);
      renderMediaDeleteControls();return;
    }
    mediaSelections[kind].clear();
    if (checked) for (const entry of mediaInventory(kind).filter(item => mediaEntryDeletable(kind,item)).slice(0,mediaBulkLimit(kind))) {
      mediaSelections[kind].set(entry.id,mediaSnapshot(kind,entry));
    }
    if (mediaDeleteProposal?.bulk && mediaDeleteProposal.kind === kind) closeMediaDeleteConfirmation(false);
    renderMediaDeleteControls();
  }

  function mediaCheckbox(kind,entry) {
    const label = document.createElement("label"); label.className = "media-select-label";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.dataset.mediaId = entry.id;
    checkbox.setAttribute("aria-label",`Select ${kind === "recording" ? entry.id : "picture " + entry.id + ".png"} for deletion`);
    checkbox.addEventListener("change",event => setMediaSelected(kind,entry.id,event.target.checked));
    const caption = document.createElement("span");
    if (kind === "recording") {
      caption.textContent = entry.id;
      const detail = document.createElement("small");detail.textContent = `${localTime(entry.modified_at,true)} · ${formatBytes(entry.bytes)}`;
      caption.append(detail);label.classList.add("media-selection-item");
    } else caption.textContent = "Select picture";
    label.append(checkbox,caption); return label;
  }

  function renderMediaSelections(kind,signedIn) {
    const prefix = mediaPrefix(kind), inventory = mediaInventory(kind), selected = mediaSelections[kind];
    const current = new Map(inventory.map(entry => [entry.id,entry]));
    for (const [id,snapshot] of selected) if (mediaSnapshot(kind,current.get(id)) !== snapshot) selected.delete(id);
    const eligible = inventory.filter(entry => mediaEntryDeletable(kind,entry)), limit = mediaBulkLimit(kind);
    if (!signedIn || !mediaPanelVisible(kind) || !eligible.length) {
      mediaSelectionModes[kind] = false;selected.clear();
      if (mediaDeleteProposal?.bulk && mediaDeleteProposal.kind === kind) closeMediaDeleteConfirmation(false);
    }
    const mode = signedIn && mediaSelectionModes[kind];
    const entryButton = byId(`${prefix}-select-multiple`);
    entryButton.hidden = !signedIn || mode || !inventory.length;
    entryButton.disabled = !mediaAdmin() || !mediaPanelVisible(kind) || mutationBusy || mediaDeleteBusy || mediaDeleteUncertain || !eligible.length;
    entryButton.setAttribute("aria-expanded",String(mode));entryButton.setAttribute("aria-pressed",String(mode));
    byId(`${prefix}-bulk-controls`).hidden = !mode;
    byId(`${prefix}-selection-cancel`).disabled = mediaDeleteBusy;
    if (kind === "recording") {
      byId("recording-picker").hidden = mode;
      byId("recording-player").hidden = mode || !selectedRecording;
    } else {
      byId("sstv-selected").hidden = mode || !mediaPanelVisible(kind) || !inventory.some(entry => entry.id === sstvSelectedId);
      for (const row of byId("sstv-gallery").children) {
        const button = row.children[0], image = current.get(button.dataset.imageId), checked = selected.has(button.dataset.imageId);
        row.classList.toggle("selection-mode",mode);row.classList.toggle("delete-selected",mode && checked);
        if (row.children[1]) {
          row.children[1].hidden = !mode;
          row.children[1].children[1].textContent = checked ? "Selected for deletion" : "Select picture";
        }
        button.setAttribute("aria-label",mode ? `${checked ? "Deselect" : "Select"} picture received ${localTime(image?.created_at,true)} for deletion`
          : `View ${typeof image?.mode === "string" ? image.mode : "SSTV"} picture received ${localTime(image?.created_at,true)}`);
        button.setAttribute("aria-pressed",String(mode ? checked : button.dataset.imageId === sstvSelectedId));
        button.classList.toggle("selected",!mode && button.dataset.imageId === sstvSelectedId);
        button.disabled = mode && (!mediaMutationAllowed(kind) || !mediaEntryDeletable(kind,image) || (!checked && selected.size >= limit));
      }
    }
    if (kind === "recording") {
      const key = JSON.stringify([signedIn,mode,inventory]);
      if (key !== recordingBulkKey) {
        const list = byId("recording-bulk-list");list.replaceChildren();
        if (mode) for (const entry of inventory) list.append(mediaCheckbox(kind,entry));
        recordingBulkKey = key;
      }
    }
    const checkboxes = kind === "recording" ? [...byId("recording-bulk-list").children].map(row => row.children[0])
      : [...byId("sstv-gallery").children].map(row => row.children[1]?.children[0]).filter(Boolean);
    for (const checkbox of checkboxes) {
      const checked = selected.has(checkbox.dataset.mediaId);
      checkbox.checked = signedIn && checked;
      checkbox.disabled = !mediaMutationAllowed(kind) || !mediaEntryDeletable(kind,current.get(checkbox.dataset.mediaId)) || (!checked && selected.size >= limit);
    }
    const all = byId(`${prefix}-select-all`), targetCount = Math.min(eligible.length,limit);
    all.checked = signedIn && targetCount > 0 && selected.size === targetCount;
    all.indeterminate = signedIn && selected.size > 0 && selected.size < targetCount;
    all.disabled = !mediaMutationAllowed(kind) || !targetCount;
    setText(`${prefix}-selected-count`,`${selected.size} selected`);
    byId(`${prefix}-delete-selected`).disabled = !mediaMutationAllowed(kind) || !selected.size;
    setText(`${prefix}-selection-help`,eligible.length > limit
      ? `Select all chooses the first ${limit} eligible ${kind === "recording" ? "recordings" : "pictures"}. Delete up to ${limit} per batch; review another batch afterward.`
      : `${kind === "recording" ? "Choose recordings below." : "Tap pictures or check their boxes to choose which to delete."} ${eligible.length} saved ${kind === "recording" ? eligible.length === 1 ? "recording" : "recordings" : eligible.length === 1 ? "picture" : "pictures"} can be selected; active or unavailable files cannot be deleted.`);
  }

  function renderMediaDeleteControls() {
    const signedIn = mediaAdmin();
    if (!signedIn || mediaSelectionCsrf !== session.csrf_token) {
      mediaSelections.recording.clear();mediaSelections.image.clear();
      mediaSelectionModes.recording = false;mediaSelectionModes.image = false;
      mediaSelectionCsrf = signedIn ? session.csrf_token : null;
    }
    for (const [kind,buttonId,messageId] of [["recording","recording-delete","recording-delete-message"],["image","sstv-delete","sstv-delete-message"]]) {
      renderMediaSelections(kind,signedIn);
      byId(buttonId).hidden = !signedIn || mediaSelectionModes[kind];
      byId(buttonId).disabled = !canDeleteMedia(kind);
      byId(messageId).hidden = !signedIn;
      message(messageId,signedIn ? mediaDeleteMessages[kind] : "",/deleted\.$/.test(mediaDeleteMessages[kind]) ? "success" : "error");
    }
    if (mediaDeleteProposal && (!signedIn || session.csrf_token !== mediaDeleteProposal.csrf || !mediaPanelVisible(mediaDeleteProposal.kind))) closeMediaDeleteConfirmation(false);
    byId("media-delete-apply").disabled = !mediaDeleteProposal || mediaDeleteBusy || mutationBusy || !mediaProposalCurrent(mediaDeleteProposal);
  }

  function closeMediaDeleteConfirmation(restoreFocus = true) {
    const kind = mediaDeleteProposal?.kind, bulk = mediaDeleteProposal?.bulk;
    mediaDeleteProposal = null;
    const dialog = byId("media-delete-dialog");
    if (dialog.open) dialog.close();
    setText("media-delete-title","Delete this saved file?");
    setText("media-delete-description","");byId("media-delete-items").replaceChildren();byId("media-delete-items").hidden=true;message("media-delete-error","");
    byId("media-delete-apply").disabled = true;
    if (restoreFocus && kind) {
      const button = byId(`${mediaPrefix(kind)}-${bulk ? "delete-selected" : "delete"}`);
      if (!button.hidden && !button.disabled) button.focus();
    }
  }

  function showMediaDeleteConfirmation(kind,bulk=false) {
    if (bulk ? !mediaMutationAllowed(kind) || !mediaSelections[kind].size : !canDeleteMedia(kind)) return;
    const entries = bulk ? mediaInventory(kind).filter(entry => mediaSelections[kind].has(entry.id)) : [mediaEntry(kind)];
    if (!entries.length || entries.length > mediaBulkLimit(kind) || entries.some(entry => !mediaEntryDeletable(kind,entry))) return;
    const entry = entries[0];
    const proposal = {kind,bulk,id:entry.id,revision:mediaEntryRevision(kind,entry),snapshot:mediaSnapshot(kind,entry),csrf:session.csrf_token,
      items:entries.map(item => ({id:item.id,revision:mediaEntryRevision(kind,item),snapshot:mediaSnapshot(kind,item)}))};
    mediaDeleteProposal = proposal; mediaDeleteMessages[kind] = "";
    setText("media-delete-title",kind === "recording" ? "Delete this recording?" : "Delete this picture?");
    const name = kind === "recording" ? entry.id : `${typeof entry.mode === "string" ? entry.mode : "SSTV"} · ${entry.id}.png`;
    const details = `${localTime(kind === "recording" ? entry.modified_at : entry.created_at,true)} · ${formatBytes(kind === "recording" ? entry.bytes : entry.size_bytes)}`;
    setText("media-delete-description",`${name}\n${details}${kind === "image" && !entry.complete ? ` · Partial (${entry.lines_received}/${entry.height} lines)` : ""}`);
    const reviewList = byId("media-delete-items");reviewList.replaceChildren();reviewList.hidden = !bulk;
    if (bulk) {
      setText("media-delete-title",`Delete ${entries.length} ${kind === "recording" ? entries.length === 1 ? "recording" : "recordings" : entries.length === 1 ? "picture" : "pictures"}?`);
      const total = entries.reduce((sum,item) => sum + (kind === "recording" ? item.bytes : item.size_bytes),0);
      setText("media-delete-description",`${entries.length} selected · ${formatBytes(total)} total. Review every file below.`);
      for (const item of entries) {
        const row = document.createElement("li");row.textContent = `${kind === "recording" ? item.id : item.id + ".png"}\n${localTime(kind === "recording" ? item.modified_at : item.created_at,true)} · ${formatBytes(kind === "recording" ? item.bytes : item.size_bytes)}`;
        reviewList.append(row);
      }
    }
    setText("media-delete-effect",bulk
      ? "This permanently deletes every listed file from the Pi. Download copies first if you want to keep them. Current reception, recording and live radio operation continue."
      : kind === "recording"
      ? "This permanently deletes this saved recording from the Pi. Download a copy first if you want to keep it. Current recording and live radio operation continue."
      : "This permanently deletes this picture and its gallery information from the Pi. Download a copy first if you want to keep it. SSTV reception and live radio operation continue.");
    message("media-delete-error","");
    const dialog = byId("media-delete-dialog");
    if (typeof dialog.showModal !== "function") {
      closeMediaDeleteConfirmation(false);mediaDeleteMessages[kind]="Use a current browser to review and confirm deletion.";renderMediaDeleteControls();return;
    }
    dialog.showModal();renderMediaDeleteControls();byId("media-delete-cancel").focus();
  }

  async function mediaJson(path, csrf, body) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(),8000);
    const options = {cache:"no-store",credentials:"same-origin",signal:controller.signal,headers:{Accept:"application/json"}};
    if (body) Object.assign(options,{method:"POST",body:JSON.stringify(body),headers:{...options.headers,"Content-Type":"application/json","X-CSRF-Token":csrf}});
    try {
      const response = await fetch(path,options);
      let result;
      try { result = await response.json(); } catch (_) { throw new Error("The Pi returned an unreadable media response."); }
      if (!response.ok) {
        if ((response.status === 401 || response.status === 403) && session.csrf_token === csrf) {
          session.authenticated = false;session.csrf_token = null;renderAdmin();
        }
        throw new Error(typeof result?.error === "string" ? result.error : `Media request failed (${response.status}).`);
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The Pi took too long to respond.");
      if (error instanceof TypeError) throw new Error("Cannot reach the Pi. Check your connection.");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function updateMediaInventory(kind, result) {
    if (kind === "recording") {
      if (!result?.recordings || !Array.isArray(result.recordings.files)) throw new Error("The Pi returned an unreadable recording inventory.");
      mediaRevision += 1;
      latestStatus = {...latestStatus,recordings:result.recordings,...(result.recorder ? {recorder:result.recorder} : {})};
      renderRecordings(result.recordings);
    } else {
      if (!result?.sstv || !Array.isArray(result.sstv.images)) throw new Error("The Pi returned an unreadable picture inventory.");
      mediaRevision += 1; dataRevision += 1;
      latestStatus = {...latestStatus,sstv:result.sstv};renderData();
    }
  }

  async function applyMediaDeletion() {
    const proposal = mediaDeleteProposal;
    if (!proposal || !byId("media-delete-dialog").open || !(proposal.bulk ? mediaMutationAllowed(proposal.kind) : canDeleteMedia(proposal.kind)) || !mediaProposalCurrent(proposal)) return;
    mediaDeleteBusy = true;mutationBusy = true;stopDataPolling();renderAdmin();
    let issued = false;
    try {
      const fresh = await mediaJson(proposal.kind === "recording" ? "/api/recordings/status" : "/api/sstv/status",proposal.csrf);
      if (!mediaAdmin() || session.csrf_token !== proposal.csrf) return;
      updateMediaInventory(proposal.kind,fresh);
      if (mediaDeleteProposal !== proposal || !byId("media-delete-dialog").open || !mediaProposalCurrent(proposal)) {
        closeMediaDeleteConfirmation(false);
        mediaDeleteMessages[proposal.kind]="The selected file changed or is no longer available. Review its current details before deleting.";
        return;
      }
      closeMediaDeleteConfirmation(false);
      mediaRevision += 1;if (proposal.kind === "image") dataRevision += 1;
      issued = true;
      mediaDeleteMessages[proposal.kind]=proposal.bulk ? `Deleting ${proposal.items.length} selected files…` : proposal.kind === "recording" ? "Deleting recording…" : "Deleting picture…";renderMediaDeleteControls();
      const endpoint = proposal.kind === "recording" ? "/api/recordings/delete" : "/api/sstv/delete";
      const result = await mediaJson(endpoint + (proposal.bulk ? "-many" : ""),proposal.csrf,proposal.bulk
        ? {items:proposal.items.map(item => ({id:item.id,revision:item.revision}))} : {id:proposal.id,revision:proposal.revision});
      if (!mediaAdmin() || session.csrf_token !== proposal.csrf) return;
      updateMediaInventory(proposal.kind,result);
      mediaDeleteUncertain=false;
      if (proposal.bulk) {
        const report = result.deletion, requested = new Set(proposal.items.map(item => item.id));
        if (!report || !Array.isArray(report.deleted) || !Array.isArray(report.failed) ||
            report.deleted.some(id => !requested.has(id)) || report.failed.some(item => !requested.has(item?.id)) ||
            proposal.items.some(item => !report.deleted.includes(item.id) && !report.failed.some(failure => failure.id === item.id))) throw new Error("The Pi returned an unreadable deletion report.");
        for (const id of report.deleted) mediaSelections[proposal.kind].delete(id);
        const inventory = new Map(mediaInventory(proposal.kind).map(entry => [entry.id,entry]));
        for (const failure of report.failed) {
          const snapshot = mediaSnapshot(proposal.kind,inventory.get(failure.id));
          if (snapshot && !report.deleted.includes(failure.id)) mediaSelections[proposal.kind].set(failure.id,snapshot);
        }
        const reasons = {changed:"a file changed",missing:"a file was already missing",storage_error:"a storage error occurred",durability_unconfirmed:"storage confirmation is pending",cleanup_pending:"picture metadata cleanup is pending",not_attempted:"some files were not attempted"};
        const warnings = [...new Set(report.failed.map(item => reasons[item.reason] || "a file could not be confirmed"))];
        mediaDeleteMessages[proposal.kind] = report.failed.length
          ? `${report.deleted.length} of ${proposal.items.length} files deleted; ${warnings.join("; ")}. Review any remaining selection before trying again.`
          : `${report.deleted.length} ${proposal.kind === "recording" ? report.deleted.length === 1 ? "recording" : "recordings" : report.deleted.length === 1 ? "picture" : "pictures"} deleted.`;
      } else mediaDeleteMessages[proposal.kind]=proposal.kind === "recording" ? "Recording deleted." : "Picture deleted.";
    } catch (error) {
      closeMediaDeleteConfirmation(false);
      if (mediaAdmin() && session.csrf_token === proposal.csrf) {
        mediaDeleteUncertain=issued;
        mediaDeleteMessages[proposal.kind]=`${error.message} ${issued ? "Refresh to check whether deletion finished before trying again." : "No deletion was requested. Refresh and review the file again."}`;
      }
    } finally {
      mediaDeleteBusy=false;mutationBusy=false;renderAdmin();
    }
  }

  byId("recording-delete").addEventListener("click",() => showMediaDeleteConfirmation("recording"));
  byId("sstv-delete").addEventListener("click",() => showMediaDeleteConfirmation("image"));
  for (const kind of ["recording","image"]) {
    const prefix = mediaPrefix(kind);
    byId(`${prefix}-select-multiple`).addEventListener("click",() => setMediaSelectionMode(kind,true));
    byId(`${prefix}-selection-cancel`).addEventListener("click",() => setMediaSelectionMode(kind,false));
    byId(`${prefix}-select-all`).addEventListener("change",event => selectAllMedia(kind,event.target.checked));
    byId(`${prefix}-delete-selected`).addEventListener("click",() => showMediaDeleteConfirmation(kind,true));
  }
  byId("media-delete-cancel").addEventListener("click",() => closeMediaDeleteConfirmation());
  byId("media-delete-dialog").addEventListener("cancel",event => {event.preventDefault();closeMediaDeleteConfirmation();});
  byId("media-delete-dialog").addEventListener("close",() => {if (!byId("media-delete-dialog").open) closeMediaDeleteConfirmation(false);});
  byId("media-delete-apply").addEventListener("click",applyMediaDeletion);

  function operationState(data) {
    const service = data?.svxlink || {};
    const radioActive = service.mode === "repeater" && service.state === "active" &&
      service.transmit_control === "enabled" && data?.setup?.rf_enabled === true;
    const capture = data?.audio?.capture, playback = data?.audio?.playback;
    const missingInterfaces = radioActive && (Array.isArray(capture) && !capture.some(card => card?.id === "AIOC_RX") ||
      Array.isArray(playback) && !playback.some(card => card?.id === "AIOC_TX"));
    if (missingInterfaces) return {key:"hardware",eyebrow:"Radio hardware needs attention",label:"Check radio interfaces",kind:"amber"};
    if (radioActive) return {key:"live",eyebrow:"Live operation",label:"Live repeater",kind:"good"};
    if (service.mode === "receive_only" && service.state === "active") return {key:"receive",eyebrow:"Receive-only operation",label:"Receive-only",kind:"good"};
    if (service.state === "failed") return {key:"failed",eyebrow:"Radio service needs attention",label:"Radio service failed",kind:"error"};
    if (service.installed === false) return {key:"setup",eyebrow:"Initial setup",label:"Setup required",kind:"amber"};
    if (service.state === "deactivating") return {key:"stopping",eyebrow:"Stopping radio services",label:"Stopping radio services",kind:"amber"};
    if (["inactive","stopped"].includes(service.state)) return {key:"stopped",eyebrow:"Radio services stopped",label:"Radio services stopped",kind:"amber"};
    if (service.mode === "bench" && service.state === "active") return {key:"setup",eyebrow:"Station setup",label:"Station setup",kind:"amber"};
    if (service.state === "activating") return {key:"starting",eyebrow:"Starting radio services",label:"Starting radio services",kind:"amber"};
    if (service.mode === "unavailable" && data?.is_pi === false) return {key:"preview",eyebrow:"Dashboard preview",label:"Dashboard preview",kind:"muted"};
    return {key:"unknown",eyebrow:"Station dashboard",label:"Radio status unavailable",kind:"muted"};
  }

  function renderOperationStatus(data, interrupted = false) {
    const state = interrupted ? {key:"unknown",eyebrow:"Connection interrupted",label:"Radio status unavailable",kind:"muted"} : operationState(data);
    setText("station-mode",`Portable repeater / ${state.eyebrow}`);
    setText("brand-subtitle",state.key === "live" ? "Live repeater" : state.key === "receive" ? "Receive-only audio" : "Local station dashboard");
    pill("phase-badge",state.label,state.kind);
  }

  function renderChannelPair(data) {
    const plan = data?.setup?.radio_plan || {};
    const radio = data?.radio_programming;
    const pair = radio?.pair;
    const pending = radio?.pending === true || radioVerificationRoles(radio).length > 0 || pair?.verification_required === true || pair?.activation_required === true;
    const savedPair = pair?.stored_verified === true && pair.catalog_changed !== true;
    const verified = !pending && pair?.catalog_changed !== true &&
      (plan.verified === true || typeof plan.verified !== "boolean" && savedPair);
    const pairPlan = verified && savedPair ? pair.plan : null;
    const rx = finite(pairPlan?.rx_hz) ? (pairPlan.rx_hz / 1000000).toFixed(6) : plan.rx_mhz;
    const tx = finite(pairPlan?.tx_hz) ? (pairPlan.tx_hz / 1000000).toFixed(6) : plan.tx_mhz;
    const tone = pairPlan && Object.hasOwn(pairPlan,"tone_hz") ? pairPlan.tone_hz : plan.tone_hz;
    setText("rx-frequency", typeof rx === "string" ? rx : "—");
    setText("tx-frequency", typeof tx === "string" ? tx : "—");
    setText("tone-frequency", typeof tone === "string" || finite(tone) ? `${tone} Hz` : tone === null ? "Off" : absent);
    setText("radio-power", textOrAbsent(pairPlan?.power || plan.power));
    setText("channels-title", verified ? operationState(data).key === "live" ? "Active channel pair" : "Verified channel pair" : pending ? "Channel pair needs verification" : "Radio channel pair");
    setText("tone-label", "Tone");
    setText("power-label", "TX power");
    const selectedId = pair?.pair_id || plan.pair_id;
    const preset = pairPresets(radio).find((entry) => entry.id === selectedId);
    const label = typeof plan.label === "string" ? plan.label : preset?.label;
    const rxName = typeof plan.rx_name === "string" ? plan.rx_name : preset?.rx_name;
    const txName = typeof plan.tx_name === "string" ? plan.tx_name : preset?.tx_name;
    const names = rxName && txName ? `${rxName} / ${txName}` : pairPlan?.name;
    const caption = verified ? [label,names].filter((value) => typeof value === "string" && value.trim()).join(" · ") : "";
    setText("channel-pair-caption",caption);
    byId("channel-pair-caption").hidden = !caption;
    setText("rf-notice", verified ? "Values from the last verified pair. Dots show receiver squelch and the Pi’s transmit command."
      : pending ? "Radio work is unfinished. The channel pair is not yet verified."
      : pair?.catalog_changed === true ? "The channel catalog changed. Verify the selected pair again."
      : operationState(data).key === "live" ? "Live mode is running. Verify a channel pair to confirm these saved values."
      : "Configure and verify a channel pair to display its saved values.");
  }

  function renderRadioActivity(activity) {
    for (const [role,key] of [["rx","receiving"],["tx","transmitting"]]) {
      const known = activity?.available === true && typeof activity[key] === "boolean";
      const active = known && activity[key];
      const dot = byId(`${role}-activity`);
      dot.className = `radio-activity-dot activity-${known ? active ? "on" : "off" : "unknown"}`;
      const label = role === "rx" ? known ? `Receiver squelch ${active ? "open" : "closed"}` : "Receiver activity unavailable"
        : known ? `Transmit command ${active ? "active" : "idle"}` : "Transmit activity unavailable";
      dot.setAttribute("aria-label",label); dot.setAttribute("title",label);
    }
  }

  async function pollRadioActivity(generation) {
    if (!activityRunning || document.hidden || generation !== activityGeneration || activityController) return;
    const controller = new AbortController();
    activityController = controller;
    const timeout = setTimeout(() => controller.abort(),1200);
    try {
      const response = await fetch("/api/activity",{cache:"no-store",signal:controller.signal,headers:{Accept:"application/json"}});
      if (!response.ok) throw new Error("Activity unavailable");
      const activity = await response.json();
      if (!activity || typeof activity.available !== "boolean" ||
          ![activity.receiving,activity.transmitting].every((value) => value === null || typeof value === "boolean")) throw new Error("Invalid activity");
      if (generation === activityGeneration && !controller.signal.aborted && !document.hidden) {
        activityLastPoll = Date.now(); renderRadioActivity(activity);
      }
    } catch (_) {
      if (generation === activityGeneration) { activityLastPoll = Date.now(); renderRadioActivity(null); }
    } finally {
      clearTimeout(timeout);
      if (activityController === controller) activityController = null;
      if (activityRunning && generation === activityGeneration && !document.hidden) {
        activityTimer = setTimeout(() => { activityTimer = null; return pollRadioActivity(generation); },500);
      }
    }
  }

  function startRadioActivity() {
    if (activityRunning || document.hidden) return;
    activityRunning = true;
    return pollRadioActivity(++activityGeneration);
  }

  function stopRadioActivity() {
    activityRunning = false; activityGeneration += 1;
    if (activityTimer !== null) clearTimeout(activityTimer);
    activityTimer = null;
    activityController?.abort(); activityController = null;
    activityLastPoll = null; renderRadioActivity(null);
  }

  function renderStatus(data) {
    latestStatus = data;
    if (!dataBusy && data.data && typeof data.data === "object" && data.data.admin_required !== true) dataError = "";
    if (data.radio_messages && typeof data.radio_messages === "object") radioMessageUncertain = false;
    renderChannelPair(data);
    if (document.hidden) renderRadioActivity(null);
    else if (activityLastPoll === null || Date.now() - activityLastPoll > 1500) renderRadioActivity(data.radio_activity);
    renderOperationStatus(data);
    setText("cpu-temp", finite(data.cpu_temp_c) ? `${data.cpu_temp_c.toFixed(1)} °C` : absent);
    setText("cpu-load", finite(data.load_1m) ? `1-minute load ${data.load_1m.toFixed(2)}` : "Load unavailable");
    byId("cpu-temp").classList.toggle("warning-text", finite(data.cpu_temp_c) && data.cpu_temp_c >= 80);
    setText("uptime", formatUptime(data.uptime_seconds));
    setText("hostname", textOrAbsent(data.hostname));
    setText("disk-free", formatBytes(data.disk?.free_bytes));
    setText("disk-total", finite(data.disk?.total_bytes) ? `of ${formatBytes(data.disk.total_bytes)} total` : "Total unavailable");
    renderPower(data.power);

    const network = data.network || {};
    pill("hotspot-status", network.hotspot_active === true ? "Hotspot active" : network.hotspot_active === false ? "Hotspot inactive" : "Unavailable", network.hotspot_active === true ? "good" : network.hotspot_active === false ? "amber" : "muted");
    setText("network-ssid", textOrAbsent(network.ssid));
    setText("dashboard-address", textOrAbsent(network.dashboard_url));
    setText("network-addresses", Array.isArray(network.addresses) && network.addresses.length ? network.addresses.join(" · ") : absent);
    const svxlink = data.svxlink || {};
    const serviceStates = { active: ["Active", "good"], inactive: ["Inactive", "amber"], unavailable: ["Unavailable", "muted"], unknown: ["Unknown", "muted"] };
    const service = serviceStates[svxlink.state] || ["Unavailable", "muted"];
    pill("svxlink-state", ...service);
    setText("svxlink-installed", svxlink.installed === true ? "Installed" : svxlink.installed === false ? "Not installed" : absent);
    setText("transmit-control", svxlink.transmit_control === "enabled" ? "Enabled" : svxlink.transmit_control === "disabled" ? "Disabled" : absent);
    setText("memory-available", finite(data.memory?.available_bytes) ? `${formatBytes(data.memory.available_bytes)} / ${formatBytes(data.memory.total_bytes)}` : absent);
    setText("clock-status", data.clock_synchronized === true ? "Synchronized" : data.clock_synchronized === false ? "Not synchronized" : absent);
    const audio = data.audio || {};
    pill("aioc-count", finite(audio.usb_aioc_count) ? `${audio.usb_aioc_count} ${audio.usb_aioc_count === 1 ? "AIOC" : "AIOCs"} detected` : "AIOCs unavailable", audio.usb_aioc_count === 2 ? "good" : "muted");
    renderAudioDevices("capture-devices", audio.capture, "capture");
    renderAudioDevices("playback-devices", audio.playback, "playback");
    renderAudioDevices("serial-devices", audio.serial_ports, "serial");
    renderRecordings(data.recordings);
    const notes = byId("setup-notes");
    notes.replaceChildren();
    const setupNotes = Array.isArray(data.setup?.notes) ? data.setup.notes.filter((note) => typeof note === "string" && note.trim()) : [];
    for (const text of setupNotes) {
      const item = document.createElement("li"); item.textContent = text; notes.append(item);
    }
    notes.hidden = setupNotes.length === 0;
    setText("settings-ssid", textOrAbsent(network.ssid));
    setText("settings-address", textOrAbsent(network.dashboard_url));
    if (data.admin && typeof data.admin.configured === "boolean" && typeof data.admin.authenticated === "boolean") {
      session.configured = data.admin.configured;
      session.authenticated = data.admin.authenticated;
      if (!session.authenticated) session.csrf_token = null;
      sessionKnown = true;
    }
    renderAdmin();
    renderWifiVisibility(data.wifi_visibility);
    renderSoftwareUpdates(data.software_updates);
    renderRadioProgramming(data.radio_programming);
    renderSettings(data.settings);
    renderRecorder(data.recorder);
    renderLive(data.live_audio);
    setText("version-label", typeof data.version === "string" ? `v${data.version}` : "");
    setText("updated-label", `Pi update ${localTime(data.updated_at)} · Refreshes every 10 seconds`);
  }

  async function refresh() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = doRefresh();
    try { return await refreshPromise; }
    finally { refreshPromise = null; }
  }

  async function refreshAfterMutation() {
    if (refreshPromise) await refreshPromise;
    return refresh();
  }

  async function doRefresh() {
    refreshing = true;
    byId("refresh-button").disabled = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    const updateRevision = softwareUpdateRevision;
    const dataSnapshotRevision = dataRevision;
    const mediaSnapshotRevision = mediaRevision;
    try {
      const response = await fetch("/api/status", { cache: "no-store", signal: controller.signal, headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`Status ${response.status}`);
      const data = await response.json();
      if (!data || typeof data !== "object" || Array.isArray(data) || typeof data.version !== "string") throw new Error("Invalid status data");
      // A lightweight check result or POST may finish during the slower system snapshot.
      if (updateRevision !== softwareUpdateRevision) data.software_updates = latestStatus?.software_updates;
      if (dataSnapshotRevision !== dataRevision) { data.data = latestStatus?.data; data.sstv = latestStatus?.sstv; }
      if (mediaSnapshotRevision !== mediaRevision) data.recordings = latestStatus?.recordings;
      if (mediaSnapshotRevision === mediaRevision && !mediaDeleteBusy) mediaDeleteUncertain = false;
      renderStatus(data);
      lastSuccess = new Date();
      byId("connection-banner").className = "connection-banner";
      setText("connection-message", data.is_pi === true ? "Connected to the Pi · Live system readings" : "Connected to the dashboard · This host is not identified as a Raspberry Pi");
    } catch (_) {
      renderOperationStatus(null,true);
      byId("connection-banner").className = "connection-banner offline";
      setText("connection-message", lastSuccess
        ? `Connection lost. Readings below are from ${lastSuccess.toLocaleTimeString()}. Stay on the box’s Wi-Fi; reconnecting automatically.`
        : "Cannot reach the dashboard service. Check your connection to the box’s Wi-Fi. Retrying automatically.");
    } finally {
      clearTimeout(timeout);
      refreshing = false;
      byId("refresh-button").disabled = false;
    }
  }

  function selectTab(name, focus = false) {
    const active = tabNames.includes(name) ? name : "overview";
    for (const tabName of tabNames) {
      const selected = tabName === active;
      const button = byId(`tab-${tabName}`);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      byId(`panel-${tabName}`).hidden = !selected;
      if (selected && focus) button.focus();
    }
    setText("page-title", tabCopy[active][0]);
    setText("page-intro", tabCopy[active][1]);
    if (location.hash !== `#${active}`) history.replaceState(null, "", `#${active}`);
    renderData();
    if (active === "data") refreshDataStatus(true);
  }

  function message(id, value, kind = "") {
    setText(id, value);
    byId(id).className = `form-message ${kind}`.trim();
  }

  function renderAdmin() {
    const signedIn = sessionKnown && session.authenticated;
    byId("admin-options").hidden=!signedIn;
    byId("recorder-admin-controls").hidden=!signedIn;
    if (!signedIn) {
      wifiVisibilityProposal=null;softwareUpdateProposal=null;radioProgrammingProposal=null;radioPairProposal=null;dtmfProposal=null;stationIdProposal=null;radioMessageProposal=null;
      closeMediaDeleteConfirmation(false);
      mediaDeleteMessages.recording = ""; mediaDeleteMessages.image = ""; mediaDeleteUncertain = false;
      clearDtmfPasswords();byId("radio-pair-confirm-password").value="";
      byId("radio-message-file").value="";
      for (const id of ["wifi-confirm-dialog","software-confirm-dialog","radio-confirm-dialog","radio-pair-confirm-dialog","dtmf-confirm-dialog","station-id-confirm-dialog","radio-message-confirm-dialog"]) {
        if (byId(id).open) byId(id).close();
      }
    }
    renderDtmf();
    renderStationIdentification();
    const updatePending = latestStatus?.software_updates?.pending === true || softwareUpdateBusy || radioTaskPending() || dtmfCodePending() || messageTaskPending();
    pill("admin-state", !sessionKnown ? "Checking" : signedIn ? "Signed in" : session.configured ? "Signed out" : "Not configured", signedIn ? "good" : sessionKnown ? "amber" : "muted");
    setText("admin-description", !sessionKnown ? "Checking admin access…" : signedIn
      ? "You can manage recording, station settings, Wi-Fi, software updates, and radio programming."
      : session.configured ? "Sign in to manage recording, station settings, Wi-Fi, software updates, and radio programming."
      : "Set an admin password on the Pi before browser controls can be used. See the setup guide.");
    byId("admin-login-form").hidden = !sessionKnown || signedIn || !session.configured;
    byId("logout-button").hidden = !signedIn;
    renderSetupGuidance();
    byId("settings-fields").disabled = !signedIn || mutationBusy || updatePending || !latestStatus?.settings;
    byId("recording-toggle").disabled = !signedIn || mutationBusy || updatePending || !latestStatus?.recorder;
    byId("login-button").disabled = mutationBusy;
    byId("logout-button").disabled = mutationBusy;
    byId("recording-auth-note").hidden = signedIn;
    setText("settings-lock-note", signedIn ? "Changes apply to this Pi" : "Admin sign-in required to edit");
    const wifi = latestStatus?.wifi_visibility;
    const wifiPending = wifi?.pending === true;
    const preview = wifi?.preview === true;
    byId("wifi-visibility-fields").disabled = mutationBusy || updatePending || wifiVisibilityBusy || wifiPending || !signedIn || (!preview && wifi?.available !== true);
    byId("wifi-confirm-apply").disabled = preview || !signedIn || mutationBusy || updatePending || wifiVisibilityBusy || wifiPending || wifi?.available !== true;
    setText("wifi-visibility-lock-note", preview ? "Preview the confirmation dialog; Wi-Fi changes are disabled here" : signedIn ? "Changes apply only after you confirm" : "Admin sign-in required to edit");
    renderSoftwareUpdateControls();
    renderRadioProgrammingControls();
    renderRadioPairControls();
    renderAdminLayout();
    renderRadioMessages();
    renderData();
    renderMediaDeleteControls();
  }

  function radioToolsNeedAttention(state = latestStatus?.radio_programming) {
    return radioTaskPending() || state?.success === false || !!radioProgrammingError || !!radioPairError ||
      radioVerificationRoles(state).length > 0 || state?.pair?.verification_required === true ||
      state?.pair?.activation_required === true || state?.pair?.catalog_changed === true;
  }

  function renderAdminLayout() {
    const state=latestStatus?.radio_programming;
    const radioCapability=state?.available === true && state.configured === true && state.preview !== true;
    const attention=radioToolsNeedAttention(state);
    const verified=["rx","tx"].every((role) => state?.roles?.[role]?.verified === true);
    const mode=attention ? "attention" : radioCapability && verified ? "complete" : radioCapability ? "setup" : "unavailable";
    if (mode !== radioToolsMode || attention) byId("radio-tools").open=mode === "attention" || mode === "setup";
    radioToolsMode=mode;
    setText("radio-tools-summary",attention ? "Radio work needs attention" : mode === "complete" ? "Both radios verified · Open for advanced tools"
      : mode === "setup" ? "Radio setup is incomplete" : "Set up the Pi radio helper to use these tools");
    byId("radio-plan-grid").hidden=!radioCapability;
    const pairCapability=radioCapability && state?.catalog_configured === true && state?.pair_switching?.enabled === true && pairPresets(state).length > 0;
    byId("radio-pair-picker").hidden=!pairCapability;
    byId("radio-pair-help").hidden=!pairCapability;
    byId("radio-pair-saved").hidden=!state?.pair && !pairCapability;
    byId("radio-pair-description").hidden=!state?.pair && !pairCapability;
    byId("radio-pair-message").hidden=!pairCapability && !attention;
    byId("radio-pair-progress-help").hidden=state?.action !== "change-pair" || !attention;
    const wifi=latestStatus?.wifi_visibility;
    byId("wifi-visibility-fields").hidden=wifi?.available !== true || wifi.preview === true;
    byId("wifi-visibility-description").hidden=wifi?.available !== true;
    byId("wifi-visibility-lock-note").hidden=wifi?.available !== true;
    byId("wifi-visibility-details").hidden=typeof wifi?.profile_hidden !== "boolean" && wifi?.pending !== true;
    byId("software-update-actions").hidden=latestStatus?.software_updates?.available !== true || latestStatus?.software_updates?.preview === true;
    byId("software-updates-lock-note").hidden=latestStatus?.software_updates?.available !== true;
    const identification=latestStatus?.station_identification;
    const identificationAvailable=identification?.available === true && typeof identification.enabled === "boolean";
    byId("station-id-enabled").hidden=!identificationAvailable;
    byId("station-id-effect").hidden=!identificationAvailable;
    byId("station-id-lock-note").hidden=!identificationAvailable;
    const dtmf=latestStatus?.dtmf_control;
    const dtmfAvailable=dtmf?.available === true || dtmf?.enabled === true;
    for (const id of ["dtmf-enabled","dtmf-announcements","dtmf-announcements-help","dtmf-menu-help"]) byId(id).hidden=!dtmfAvailable;
    const codeAvailable=dtmf?.code_change?.available === true;
    const codeAttention=dtmfCodePending() || !!dtmfCodeError || dtmf?.code_change?.success === false;
    const passwordMode=codeAttention ? "attention" : codeAvailable && dtmf?.configured !== true ? "setup" : "ready";
    byId("dtmf-password-tools").hidden=!codeAvailable && !codeAttention;
    byId("dtmf-code-form").hidden=!codeAvailable;
    if (!sessionKnown || !session.authenticated) byId("dtmf-password-tools").open=false;
    else if (codeAttention || passwordMode === "setup" && dtmfPasswordMode !== passwordMode) byId("dtmf-password-tools").open=true;
    dtmfPasswordMode=passwordMode;
  }

  function validCommit(value) {
    return typeof value === "string" && /^[0-9a-f]{40}$/i.test(value);
  }

  function softwareVersion(version, commit, fallback = absent) {
    const name = typeof version === "string" && version.trim() ? version : "";
    const revision = validCommit(commit) ? commit.slice(0, 7) : "";
    return name && revision ? `${name} · ${revision}` : name || revision || fallback;
  }

  function softwareReleaseOrder(updates) {
    const parse = value => typeof value === "string" && /^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$/.test(value)
      ? value.split(".").map(Number) : null;
    const installed = parse(updates?.current_version || latestStatus?.version);
    const latest = parse(updates?.latest_version);
    if (!installed || !latest) return null;
    for (let index = 0; index < installed.length; index++) {
      if (latest[index] !== installed[index]) return latest[index] > installed[index] ? 1 : -1;
    }
    return 0;
  }

  function softwareUpdateOffered(updates) {
    const order = softwareReleaseOrder(updates);
    return order !== null && order >= 0 && updates?.check_stale !== true && updates?.can_install !== false &&
      updates?.update_available === true && validCommit(updates?.latest_commit) &&
      (!validCommit(updates?.current_commit) || updates.current_commit.toLowerCase() !== updates.latest_commit.toLowerCase());
  }

  function canManageSoftware(updates = latestStatus?.software_updates) {
    return sessionKnown && session.authenticated && typeof session.csrf_token === "string" && !!session.csrf_token &&
      updates?.available === true && updates?.preview !== true && updates.pending !== true &&
      !mutationBusy && !softwareUpdateBusy && !wifiVisibilityBusy && latestStatus?.wifi_visibility?.pending !== true && !radioTaskPending() && !dtmfCodePending() && !messageTaskPending();
  }

  function softwareInstalledKey(updates) {
    return JSON.stringify([updates?.current_version || latestStatus?.version || null, updates?.current_commit || null]);
  }

  function softwareTargetKey(updates) {
    return JSON.stringify([updates?.latest_version || null, updates?.latest_commit || null]);
  }

  function freshSoftwareCheck(updates = latestStatus?.software_updates, complete = false) {
    const check = softwareUpdateCheck;
    if (!check) return false;
    if (!sessionKnown || !session.authenticated || updates?.available !== true || updates.preview === true || updates.check_stale === true || session.csrf_token !== check.csrf ||
        softwareInstalledKey(updates) !== check.installed) {
      softwareUpdateCheck = null;
      return false;
    }
    // A check must be issued by this page and acknowledged with its new ID.
    // Saved version details can be displayed, but a cached result cannot offer an update.
    if (!check.requestId) return false;
    if (updates?.action !== "check" || updates.request_id !== check.requestId || updates.success === false) {
      softwareUpdateCheck = null;
      return false;
    }
    if (updates.pending === true) {
      if (check.target !== null) softwareUpdateCheck = null;
      return false;
    }
    if (updates.success !== true || !validCommit(updates.latest_commit) || softwareReleaseOrder(updates) === null) {
      softwareUpdateCheck = null;
      return false;
    }
    const target = softwareTargetKey(updates);
    if (check.target === null && complete) check.target = target;
    if (check.target !== target) {
      if (check.target !== null) softwareUpdateCheck = null;
      return false;
    }
    return true;
  }

  function renderSoftwareUpdateControls() {
    const updates = latestStatus?.software_updates;
    const checkedHere = freshSoftwareCheck(updates);
    const enabled = canManageSoftware(updates);
    const canInstall = enabled && checkedHere && softwareUpdateOffered(updates);
    byId("software-check").disabled = !enabled;
    byId("software-install").disabled = !canInstall;
    byId("software-confirm-apply").disabled = !canInstall;
    renderSoftwareCheckProgress();
    setText("software-updates-lock-note", updates?.preview === true ? "Updates run on the Pi; this preview cannot install software"
      : !sessionKnown || !session.authenticated ? "Admin sign-in required to manage updates"
      : radioTaskPending() ? "A radio programming task is in progress"
      : updates?.pending === true || softwareUpdateBusy ? "An update task is in progress"
      : !freshSoftwareCheck(updates) ? "Check for updates in this session before installing"
      : "Changes apply only after you confirm installation");
  }

  function softwareCheckAllowed() {
    const updates = latestStatus?.software_updates;
    return softwareCheckPageActive && !document.hidden && sessionKnown && session.authenticated &&
      updates?.available === true && updates.preview !== true;
  }

  function softwareCheckPending() {
    return softwareCheckAllowed() && latestStatus?.software_updates?.pending === true &&
      latestStatus.software_updates.action === "check";
  }

  function stopSoftwareCheckPolling() {
    if (softwareCheckTimer !== null) clearTimeout(softwareCheckTimer);
    softwareCheckTimer = null;
    if (softwareCheckController) softwareCheckController.abort();
    softwareCheckController = null;
    softwareCheckGeneration += 1;
  }

  function scheduleSoftwareCheckPoll() {
    if (!softwareCheckPending() || softwareUpdateBusy) {
      stopSoftwareCheckPolling();
      return;
    }
    if (softwareCheckTimer !== null || softwareCheckController) return;
    softwareCheckTimer = setTimeout(() => {
      softwareCheckTimer = null;
      refreshSoftwareCheck();
    }, 750);
  }

  function renderSoftwareCheckProgress() {
    const checking = softwareCheckAllowed() && (softwareCheckPending() ||
      softwareUpdateBusy && softwareUpdateAction === "check");
    byId("software-check-progress").hidden = !checking;
    byId("software-check-progress-bar").removeAttribute("value");
    byId("software-updates-card").setAttribute("aria-busy", String(checking));
    setText("software-check", checking ? "Checking…" : "Check for updates");
    setText("software-check-progress-note", "Checking GitHub for the latest dashboard version…");
    scheduleSoftwareCheckPoll();
  }

  async function refreshSoftwareCheck() {
    if (!softwareCheckPending() || softwareUpdateBusy || softwareCheckController) return;
    if (softwareCheckTimer !== null) clearTimeout(softwareCheckTimer);
    softwareCheckTimer = null;
    const csrf = session.csrf_token;
    const expected = latestStatus.software_updates;
    const installed = softwareInstalledKey(expected);
    const generation = softwareCheckGeneration;
    const controller = new AbortController();
    softwareCheckController = controller;
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch("/api/software-updates/status", {
        cache: "no-store", credentials: "same-origin", signal: controller.signal,
        headers: { Accept: "application/json" }
      });
      const result = await response.json();
      if (generation !== softwareCheckGeneration || controller.signal.aborted || session.csrf_token !== csrf) return;
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          session.authenticated = false;
          session.csrf_token = null;
          renderAdmin();
        }
        throw new Error("Update status unavailable");
      }
      const updates = result.software_updates;
      if (!updates || typeof updates !== "object" || Array.isArray(updates)) throw new Error("Unreadable update status");
      if (generation !== softwareCheckGeneration || controller.signal.aborted || !softwareCheckPending() ||
          session.csrf_token !== csrf || softwareInstalledKey(latestStatus.software_updates) !== installed ||
          latestStatus.software_updates.request_id !== expected.request_id) return;
      if (updates.available === true && updates.preview !== true &&
          (updates.action !== "check" || updates.request_id !== expected.request_id || softwareInstalledKey(updates) !== installed)) return;
      softwareUpdateRevision += 1;
      latestStatus = { ...latestStatus, software_updates: updates };
      renderSoftwareUpdates(updates);
    } catch (_) {
      if (generation === softwareCheckGeneration && softwareCheckPending()) {
        setText("software-check-progress-note", "Update status connection interrupted. Reconnecting automatically…");
      }
    } finally {
      clearTimeout(timeout);
      if (softwareCheckController === controller) softwareCheckController = null;
      if (generation === softwareCheckGeneration) scheduleSoftwareCheckPoll();
    }
  }

  function renderSoftwareUpdates(updates) {
    const checkedHere = freshSoftwareCheck(updates, true);
    const pending = updates?.pending === true;
    const install = updates?.action === "install";
    const failed = !pending && updates?.success === false;
    const order = softwareReleaseOrder(updates);
    const updateAvailable = checkedHere && softwareUpdateOffered(updates);
    const checked = checkedHere && (order < 0 || updates?.update_available === false ||
      validCommit(updates?.current_commit) && updates.current_commit.toLowerCase() === updates.latest_commit.toLowerCase());
    if (softwareUpdateAttempt && typeof updates?.request_id === "string" && updates.request_id !== softwareUpdateAttempt.priorRequestId) {
      softwareUpdateError = "";
      if (!pending) softwareUpdateAttempt = null;
    }
    setText("software-current-version", softwareVersion(updates?.current_version || latestStatus?.version, updates?.current_commit));
    setText("software-latest-version", softwareVersion(updates?.latest_version, updates?.latest_commit, "Not checked"));
    setText("software-checked-at", typeof updates?.checked_at === "string" ? localTime(updates.checked_at, true) : "Not checked");
    pill("software-updates-state", pending ? install ? "Installing" : "Checking" : updates?.preview === true ? "Preview" : failed ? install ? "Update failed" : "Check failed" : updates?.available !== true ? "Unavailable" : updateAvailable ? "Update available" : checked ? "Up to date" : "Not checked",
      pending ? "amber" : failed ? "error" : updateAvailable ? "amber" : checked ? "good" : "muted");
    const detail = typeof updates?.message === "string" && updates.message.trim() ? updates.message : "";
    if (softwareUpdateError) message("software-updates-message", softwareUpdateError, "error");
    else if (pending) message("software-updates-message", detail || (install ? "Installing the dashboard update. This page reconnects automatically after the service restarts." : "Checking GitHub for a dashboard update…"));
    else if (updates?.preview === true) message("software-updates-message", "Preview only — update checks and installation are disabled on this computer.");
    else if (failed) message("software-updates-message", detail || "The update task did not complete. Check the Pi’s internet connection and try again.", "error");
    else if (updates?.success === true && install) message("software-updates-message", `${detail || "Dashboard software updated."} Reload this page to use the updated dashboard, then sign in again.`, "success");
    else if (updates?.available === true && !checkedHere) message("software-updates-message", updates?.check_stale === true || validCommit(updates?.latest_commit)
      ? "The saved update check needs refreshing. Select Check for updates to check the installed version."
      : "Select Check for updates while the Pi has internet access.");
    else if (checkedHere && order < 0) message("software-updates-message", "The installed dashboard is newer than the checked release. No update is needed.");
    else message("software-updates-message", updates?.available !== true ? detail || "Software updates are unavailable. Install the current Pi update helper first." : updateAvailable ? detail || "A dashboard update is available. Review it before installing." : checked ? "The dashboard is up to date." : "Select Check for updates while the Pi has internet access.");
    renderAdmin();
  }

  function dtmfKey(value) {
    return value && typeof value === "object" ? JSON.stringify({available:value.available,configured:value.configured,bridge_ready:value.bridge_ready,enabled:value.enabled,status_announcements:value.status_announcements,data_controls:value.data_controls?.available === true,code_pending:value.code_change?.pending,code_request:value.code_change?.request_id}) : "null";
  }

  function dtmfMaintenanceClear(ignoreBusy = false) {
    const radio = latestStatus?.radio_programming;
    return sessionKnown && session.authenticated && typeof session.csrf_token === "string" && !!session.csrf_token &&
      (ignoreBusy || !mutationBusy) && !softwareUpdateBusy && !wifiVisibilityBusy && !radioProgrammingBusy &&
      latestStatus?.software_updates?.pending !== true && latestStatus?.wifi_visibility?.pending !== true &&
      latestStatus?.dtmf_control?.code_change?.pending !== true &&
      !radioTaskPending() && !messageTaskPending() && !radioVerificationRoles(radio).length && radio?.pair?.verification_required !== true;
  }

  function canManageDtmf(ignoreBusy = false) {
    const value = latestStatus?.dtmf_control;
    return dtmfMaintenanceClear(ignoreBusy) && typeof value?.enabled === "boolean" &&
      (value.available === true || value.enabled === true);
  }

  function stationIdentificationKey(value) {
    return value && typeof value === "object" ? JSON.stringify({available:value.available,enabled:value.enabled}) : "null";
  }

  function canManageStationIdentification(ignoreBusy = false) {
    const value=latestStatus?.station_identification;
    return dtmfMaintenanceClear(ignoreBusy) && session.configured === true &&
      value?.available === true && typeof value.enabled === "boolean";
  }

  function renderStationIdentification() {
    const value=latestStatus?.station_identification;
    const known=typeof value?.enabled === "boolean", enabled=value?.enabled === true;
    byId("station-id-enabled").setAttribute("aria-checked",String(enabled));
    byId("station-id-enabled").disabled=!canManageStationIdentification();
    setText("station-id-switch-value",known ? enabled ? "On" : "Off" : "Unknown");
    pill("station-id-state",value?.available !== true || !known ? "Unavailable" : enabled ? "On" : "Off",value?.available === true && enabled ? "good" : "muted");
    setText("station-id-help",value?.available !== true ? "Complete live repeater setup on the Pi to manage automatic identification."
      : !known ? "Automatic station identification status is unavailable."
      : enabled ? "Automatic station identification is on. Spoken DTMF prompts are controlled separately."
      : "Automatic station identification is off. You must provide required station identification manually. Spoken DTMF prompts are unaffected.");
    setText("station-id-lock-note",!sessionKnown || !session.authenticated ? "Admin sign-in required to change automatic identification."
      : value?.available !== true || !known ? "Automatic station identification is unavailable on this Pi."
      : !canManageStationIdentification() ? "Resolve current maintenance or unfinished radio work before changing automatic identification."
      : "Toggle to review and confirm the change.");
    message("station-id-message",stationIdError,stationIdError ? "error" : "");
    if (stationIdProposal && (!sessionKnown || !session.authenticated || session.csrf_token !== stationIdProposal.csrf ||
        session.configured !== stationIdProposal.configured || stationIdentificationKey(value) !== stationIdProposal.state)) {
      stationIdProposal=null;
      if (byId("station-id-confirm-dialog").open) byId("station-id-confirm-dialog").close();
    }
    byId("station-id-confirm-apply").disabled=!stationIdProposal || !canManageStationIdentification() || !byId("station-id-confirm-dialog").open;
  }

  function closeStationIdentificationConfirmation() {
    stationIdProposal=null;
    if (byId("station-id-confirm-dialog").open) byId("station-id-confirm-dialog").close();
    renderStationIdentification();
    if (!byId("station-id-enabled").disabled) byId("station-id-enabled").focus();
  }

  function canChangeDtmfPassword(ignoreBusy = false) {
    return dtmfMaintenanceClear(ignoreBusy) && latestStatus?.dtmf_control?.code_change?.available === true;
  }

  function clearDtmfPasswords() {
    for (const id of ["dtmf-new-code","dtmf-admin-password"]) byId(id).value="";
  }

  function renderDtmf() {
    const value = latestStatus?.dtmf_control;
    const enabled = value?.enabled === true, dataControls = value?.data_controls?.available === true;
    setText("dtmf-description",dataControls
      ? "Use a private radio password to change recording, spoken status, automatic identification, APRS decoding, or SSTV decoding."
      : "Use a private radio password to change recording, spoken status, or automatic identification.");
    setText("dtmf-help",`Enter the six-character DTMF password, press # and release PTT. Choose 0 to hear current settings. To change a setting, choose 1 for status announcements, 2 for recording, ${dataControls ? "3 for automatic station identification, 4 for APRS decoding, or 5 for SSTV decoding" : "or 3 for automatic station identification"}, then 0 for off or 1 for on. Press # and release PTT after each choice, then wait for the prompt. Use * to cancel. Each prompt gives you 30 seconds of idle time after it finishes. A valid option starts a fresh window; a completed action needs the password again. Recording commands affect the current session; the startup preference stays unchanged.`);
    byId("dtmf-data-help").hidden = dataControls || value?.available !== true;
    setText("dtmf-data-help",dataControls ? "" : "Install the radio-control update on the Pi to add APRS and SSTV menu options.");
    byId("dtmf-enabled").setAttribute("aria-checked",String(enabled));
    byId("dtmf-enabled").disabled = !canManageDtmf();
    setText("dtmf-switch-value",enabled ? "On" : "Off");
    const announcements = value?.status_announcements !== false;
    byId("dtmf-announcements").setAttribute("aria-checked",String(announcements));
    byId("dtmf-announcements").disabled = !canManageDtmf();
    setText("dtmf-announcements-value",announcements ? "On" : "Off");
    pill("dtmf-state",!value ? "Unavailable" : value.code_change?.pending === true ? "Changing password" : value.configured !== true ? "Not configured" : value.bridge_ready !== true ? "Bridge unavailable" : enabled ? "Enabled" : "Off",enabled && value?.available === true && !value.code_change?.pending ? "good" : "muted");
    setText("dtmf-lock-note",!sessionKnown || !session.authenticated ? "Admin sign-in required to change DTMF control."
      : value?.code_change?.pending === true ? "Wait for the private password change to finish. Radio DTMF commands are paused."
      : value?.configured !== true ? "Set a private DTMF password below or configure it locally on the Pi first."
      : value?.bridge_ready !== true ? "The local DTMF bridge must be running before control can be enabled."
      : !canManageDtmf() ? "Resolve current maintenance or unfinished radio work before changing DTMF control."
      : "Toggle to review and confirm the change.");
    message("dtmf-message",dtmfError,dtmfError ? "error" : "");
    const canPassword = canChangeDtmfPassword();
    for (const id of ["dtmf-new-code","dtmf-admin-password"]) byId(id).disabled=!canPassword;
    byId("dtmf-change-code").disabled=!canPassword;
    const change=value?.code_change;
    message("dtmf-code-message",dtmfCodeError || change?.message || (!canPassword ? "The Pi password helper and admin sign-in are required." : ""),dtmfCodeError || change?.success === false ? "error" : change?.success === true ? "success" : "");
    byId("dtmf-confirm-apply").disabled = !dtmfProposal || !canManageDtmf() || !byId("dtmf-confirm-dialog").open;
    if (!session.authenticated || dtmfFormCsrf !== session.csrf_token) clearDtmfPasswords();
    dtmfFormCsrf=session.csrf_token;
    if (dtmfProposal && (!session.authenticated || session.csrf_token !== dtmfProposal.csrf)) {
      dtmfProposal=null;
      if (byId("dtmf-confirm-dialog").open) byId("dtmf-confirm-dialog").close();
    }
  }

  function closeDtmfConfirmation() {
    dtmfProposal=null;
    if (byId("dtmf-confirm-dialog").open) byId("dtmf-confirm-dialog").close();
    renderDtmf();
    if (!byId("dtmf-enabled").disabled) byId("dtmf-enabled").focus();
  }

  function dtmfCodePending() {
    return latestStatus?.dtmf_control?.code_change?.pending === true;
  }

  function radioTaskPending() {
    return radioProgrammingBusy || latestStatus?.radio_programming?.pending === true;
  }

  function messageTaskPending() {
    return radioMessageActionBusy || radioMessageUncertain || latestStatus?.radio_messages?.upload_pending === true || latestStatus?.radio_messages?.job?.pending === true;
  }

  function radioPlanKey(plan) {
    if (!plan || typeof plan !== "object") return null;
    const selected = {};
    for (const role of ["rx", "tx"]) {
      const item = plan[role];
      if (!item || item.memory !== 1 || typeof item.name !== "string" ||
          typeof item.frequency_mhz !== "string" || !/^\d{3}\.\d{3,6}$/.test(item.frequency_mhz) ||
          item.mode !== "NFM" || typeof item.transmit !== "boolean" ||
          !["High", "Low"].includes(item.power) ||
          ![item.receive_tone_hz,item.transmit_tone_hz].every((tone) => tone === null || finite(tone) && tone >= 60 && tone <= 260)) return null;
      selected[role] = { memory: item.memory, name: item.name, frequency_mhz: item.frequency_mhz,
        mode: item.mode, transmit: item.transmit, power: item.power,
        receive_tone_hz: item.receive_tone_hz, transmit_tone_hz: item.transmit_tone_hz };
    }
    return JSON.stringify(selected);
  }

  function radioPlanDescription(role, plan) {
    const item = plan[role];
    const tone = role === "rx" ? `Receive tone ${item.receive_tone_hz ?? "off"}${item.receive_tone_hz ? " Hz" : ""}`
      : `Transmit tone ${item.transmit_tone_hz ?? "off"}${item.transmit_tone_hz ? " Hz" : ""}`;
    return `Memory ${item.memory} · ${item.mode} · ${tone} · ${item.power} power${item.transmit ? "" : " · Transmit disabled"} · Auto Lock on`;
  }

  function canManageRadios(state = latestStatus?.radio_programming) {
    return sessionKnown && session.authenticated && typeof session.csrf_token === "string" && !!session.csrf_token &&
      state?.available === true && state.configured === true && state.preview !== true && state.pending !== true &&
      !mutationBusy && !radioProgrammingBusy && !softwareUpdateBusy &&
      latestStatus?.software_updates?.pending !== true && !wifiVisibilityBusy && latestStatus?.wifi_visibility?.pending !== true && !dtmfCodePending() && !messageTaskPending();
  }

  function canRequestRadio(role, action, state = latestStatus?.radio_programming) {
    if (!["rx", "tx"].includes(role) || !["inspect", "program", "verify", "repair-upload"].includes(action) || !canManageRadios(state)) return false;
    return radioActionAllowed(role, action, state);
  }

  function radioVerificationRoles(state) {
    const roles = ["rx", "tx", "programmer"].filter((role) => state?.roles?.[role]?.verification_required === true);
    if (state?.verification_required === true && ["rx", "tx", "programmer"].includes(state.role) && !roles.includes(state.role)) roles.push(state.role);
    return roles;
  }

  function radioConnectionLabel(role) {
    return role === "programmer" ? "External programmer" : `${role.toUpperCase()} radio`;
  }

  function externalProgrammerProfile(state) {
    return state?.roles?.programmer?.model === "UV5R" ? "UV-5R / UV5R" : "UV-5RX / 5RX";
  }

  function renderUnfinishedRadioNotice(state) {
    const unfinished=radioVerificationRoles(state);
    byId("radio-unfinished-notice").hidden=!unfinished.length || state?.pending === true;
    if (!unfinished.length || state?.pending === true) {
      setText("radio-unfinished-title","");setText("radio-unfinished-detail","");
      byId("radio-recovery-link").removeAttribute("href");
      return;
    }
    const external=unfinished.includes("programmer");
    const labels=unfinished.map(radioConnectionLabel).join(", ");
    setText("radio-unfinished-title",unfinished.length === 1 ? `${labels} upload unfinished` : `Uploads unfinished: ${labels}`);
    setText("radio-unfinished-detail",`${external
      ? `The external programmer has an unfinished radio upload${unfinished.length > 1 ? `; the unfinished connections are ${labels}` : ""}. New RX/TX programming stays locked even when those radios show verified. Open the editor and select “External programmer”. Its ${externalProgrammerProfile(state)} profile stays locked to the unfinished upload. Confirm the same physical radio is connected before choosing Verify upload, or read it to review recovery options.`
      : `${labels} still ${unfinished.length === 1 ? "needs" : "need"} verification. New inspection and programming stay locked. Use the matching Verify button below, or open the editor and select the unfinished radio to review recovery options. Radio audio remains paused.`}`);
    byId("radio-recovery-link").setAttribute("href","/radio-editor");
  }

  function radioActionAllowed(role, action, state) {
    const unfinished = radioVerificationRoles(state);
    if (action === "repair-upload") return repairableRadioUpload(state, role);
    if (unfinished.length && (action !== "verify" || !unfinished.includes(role))) return false;
    const radio = state.roles?.[role];
    return action === "inspect" || (action === "program" ? state.catalog_configured === true && /^[0-9a-f]{64}$/.test(state.catalog_digest || "") && !!radioPlanKey(state.plan) && radio?.inspected === true && radio?.compatible === true && radio.verification_required !== true : radio?.verification_required === true || radio?.programmed === true);
  }

  function repairableRadioUpload(state, role) {
    const radio=state?.roles?.[role];
    return radio?.verification_required === true && ["editor","repeater"].includes(radio.program_kind) &&
      typeof radio.pending_id === "string" && /^[0-9a-f]{32}$/.test(radio.pending_id) &&
      state?.pair?.verification_required !== true && !["rx","tx","programmer"].some((item) =>
        state?.roles?.[item]?.verification_required === true && state.roles[item].program_kind === "pair");
  }

  function pairPresetKey(preset) {
    if (!preset || typeof preset.id !== "string" || !/^[a-z][a-z0-9_-]{0,15}$/.test(preset.id) ||
        typeof preset.label !== "string" || !preset.label.trim() || preset.label.length > 80 ||
        !Number.isInteger(preset.index) || preset.index < 1 || preset.index > 9 ||
        preset.rx_name !== `BOX-RX${preset.index}` || preset.tx_name !== `BOX-TX${preset.index}` ||
        typeof preset.name !== "string" || preset.name !== `${preset.rx_name} / ${preset.tx_name}` ||
        !Number.isInteger(preset.input_hz) || preset.input_hz < 144000000 || preset.input_hz >= 148000000 || preset.input_hz % 10 ||
        !Number.isInteger(preset.output_hz) || preset.output_hz < 420000000 || preset.output_hz >= 450000000 || preset.output_hz % 10 ||
        !(preset.tone_hz === null || finite(preset.tone_hz) && preset.tone_hz >= 60 && preset.tone_hz <= 260)) return null;
    return JSON.stringify(preset);
  }

  function pairPresets(state = latestStatus?.radio_programming) {
    const presets = Array.isArray(state?.pair_switching?.presets) ? state.pair_switching.presets : [];
    const seen = new Set();
    return presets.filter((preset) => { if (!pairPresetKey(preset) || seen.has(preset.id)) return false; seen.add(preset.id); return true; }).slice(0,9);
  }

  function radioPairKey(state = latestStatus?.radio_programming) {
    const pair=state?.pair;
    return pair && typeof pair === "object" && !Array.isArray(pair) ? JSON.stringify(pair) : "null";
  }

  function canChangeRadioPair(state = latestStatus?.radio_programming) {
    if (!canManageRadios(state) || state.catalog_configured !== true || !/^[0-9a-f]{64}$/.test(state.catalog_digest || "") || state?.pair_switching?.enabled !== true || !pairPresets(state).length) return false;
    if (["rx","tx"].some((role) => state.roles?.[role]?.compatible === false)) return false;
    const unfinished=radioVerificationRoles(state);
    if (!unfinished.length) return true;
    const pair=state?.pair;
    return pair?.catalog_changed !== true && pair?.verification_required === true && typeof pair.request_id === "string" && /^[0-9a-f]{32}$/.test(pair.request_id)
      && unfinished.every((role) => ["rx","tx"].includes(role));
  }

  function pairFrequency(hz) { return Number.isInteger(hz) ? `${(hz/1000000).toFixed(3)} MHz` : absent; }

  function pairPlanText(pair) {
    const plan=pair?.plan;
    if (!plan || !Number.isInteger(plan.rx_hz) || !Number.isInteger(plan.tx_hz)) return "No complete verified pair record; per-radio settings may differ.";
    return `${typeof plan.name === "string" ? plan.name : "Saved pair"} · RX input ${pairFrequency(plan.rx_hz)} → TX output ${pairFrequency(plan.tx_hz)}`;
  }

  function pairRoleVerified(pair, role) { return pair?.roles?.[role] === true || pair?.roles?.[role]?.verified === true; }

  function renderRadioPairControls() {
    const state=latestStatus?.radio_programming;
    if (radioPairProposal && (!sessionKnown || !session.authenticated || session.csrf_token !== radioPairProposal.csrf)) {
      radioPairProposal=null;
      byId("radio-pair-confirm-password").value="";
      if (byId("radio-pair-confirm-dialog").open) byId("radio-pair-confirm-dialog").close();
    }
    const allowed=canChangeRadioPair(state);
    const selected=pairPresets(state).find((preset) => preset.id === byId("radio-pair-select").value);
    byId("radio-pair-select").disabled=!allowed;
    byId("radio-pair-change").disabled=!allowed || !selected;
    byId("radio-pair-confirm-apply").disabled=!allowed || !radioPairProposal || !byId("radio-pair-confirm-dialog").open ||
      !byId("radio-pair-confirm-password").value || byId("radio-pair-confirm-password").value.length > 1024;
    setText("radio-pair-lock-note", !sessionKnown || !session.authenticated ? "Admin sign-in and a fresh password confirmation are required."
      : radioTaskPending() ? "A radio task is in progress. Keep both radios connected and powered."
      : radioVerificationRoles(state).includes("programmer") ? "The external programmer upload is unfinished. Resolve it in the radio editor before changing the pair."
      : state?.pair?.verification_required === true && state.pair.catalog_changed === true ? "Restore the pending pair’s private catalog on the Pi before retrying."
      : state?.catalog_configured !== true ? "Configure channel pairs locally on the Pi first."
      : state?.pair_switching?.enabled !== true ? "Paired switching needs initial radio checks on this Pi."
      : !allowed ? "Resolve other maintenance or unfinished radio work before changing the pair."
      : "Every change requires the admin password again and a review of both frequencies.");
  }

  function renderRadioPair(state) {
    const presets=pairPresets(state);
    const select=byId("radio-pair-select");
    const previous=select.value;
    const optionsKey=JSON.stringify(presets);
    if (select.dataset.presetKey !== optionsKey) {
      select.replaceChildren();
      if (!presets.length) { const option=document.createElement("option"); option.value=""; option.textContent="Channel pairs unavailable"; select.append(option); }
      for (const preset of presets) {
        const option=document.createElement("option"); option.value=preset.id;
        option.textContent=`${preset.label} · ${preset.name} · ${pairFrequency(preset.input_hz)} → ${pairFrequency(preset.output_hz)}`;
        select.append(option);
      }
      select.dataset.presetKey=optionsKey;
      select.value=presets.some((preset) => preset.id === previous) ? previous : presets[0]?.id || "";
    }
    const pair=state?.pair;
    const pairPending=state?.pending === true && state.action === "change-pair";
    const stored=pair?.stored_verified === true && pairRoleVerified(pair,"rx") && pairRoleVerified(pair,"tx");
    const activationRequired=pair?.activation_required === true;
    const verificationRequired=pair?.verification_required === true;
    const failed=state?.action === "change-pair" && state.success === false && !pairPending;
    const staleCatalogRecord=pair?.catalog_changed === true && !pairPending && !verificationRequired && !failed;
    const catalogReviewMessage="The private channel catalog changed. Choose a pair and complete a new paired programming task to verify it against the current catalog.";
    if (radioPairAttempt && typeof state?.request_id === "string" && state.request_id !== radioPairAttempt.priorRequestId) {
      radioPairError=""; if (!pairPending) radioPairAttempt=null;
    }
    setText("radio-pair-current",stored ? `${activationRequired ? "Saved pair verified" : "Configured pair"} · ${pairPlanText(pair)}` : staleCatalogRecord ? "New pair verification needed" : "No complete verified pair record");
    setText("radio-pair-current-detail",stored ? activationRequired
      ? "Both saved images match. Activation is still unconfirmed; follow the task result before using the repeater. This is not live telemetry."
      : "Both radios' saved settings were verified together. This is the configured pair record, not a live reading of their selected frequencies."
      : staleCatalogRecord ? catalogReviewMessage
      : pair ? `Requested pair: ${pairPlanText(pair)}. ${verificationRequired || failed ? "The pair is incomplete; radio audio stays paused. Review the activity log before retrying." : "Wait for both radios to finish before treating this as a configured pair."}`
        : "Inspecting or programming one radio does not establish a verified pair.");
    pill("radio-pair-state",pairPending ? "Changing pair" : verificationRequired || failed ? "Pair incomplete" : staleCatalogRecord ? "Catalog changed" : activationRequired ? "Activation needed" : stored ? "Configured" : state?.pair_switching?.enabled === true ? "Ready" : "Radio checks needed",
      pairPending || staleCatalogRecord || activationRequired ? "amber" : verificationRequired || failed ? "error" : stored ? "good" : "muted");
    byId("radio-pair-stages").hidden=!pair || staleCatalogRecord || stored && !pairPending && !activationRequired && !verificationRequired && !failed;
    for (const role of ["rx","tx"]) {
      const saved=pair?.roles?.[role];
      setText(`radio-pair-stage-${role}`,`${role.toUpperCase()} · ${pairRoleVerified(pair,role) ? "Saved settings verified" : saved?.written === true ? "Write started; verification pending" : pairPending ? "Waiting / in progress" : "Not verified"}`);
    }
    const detail=typeof state?.pair_switching?.message === "string" ? state.pair_switching.message : "";
    message("radio-pair-message",radioPairError || (pairPending ? "Programming and verifying RX, then TX. Follow the shared radio activity log below."
      : failed || verificationRequired ? "The paired operation is incomplete. Radio audio remains paused. A reviewed retry reads and backs up both radios again; it does not automatically roll back a completed write."
        : stored && activationRequired ? "Stored settings verified; activation still needs confirmation."
          : stored ? "The configured pair record has been verified. Handhelds must use the matching split frequencies."
            : staleCatalogRecord ? catalogReviewMessage
              : detail || "Paired changes remain disabled until both radios pass their initial radio checks."),radioPairError || failed || verificationRequired ? "error" : stored && !activationRequired ? "success" : "");
    renderRadioPairControls();
  }

  function renderRadioProgrammingControls() {
    const state = latestStatus?.radio_programming;
    const unfinished=radioVerificationRoles(state);
    for (const role of ["rx", "tx"]) {
      for (const action of ["inspect", "program", "verify"]) byId(`radio-${action}-${role}`).disabled = !canRequestRadio(role, action, state);
      byId(`radio-repair-${role}`).hidden=!repairableRadioUpload(state,role);
      byId(`radio-repair-${role}`).disabled=!canRequestRadio(role,"repair-upload",state);
    }
    const proposal = radioProgrammingProposal;
    byId("radio-confirm-apply").disabled = !proposal || !canRequestRadio(proposal.role, proposal.action, state);
    setText("radio-programming-lock-note", state?.preview === true ? "Radio programming runs on the Pi; this preview cannot access radios"
      : !sessionKnown || !session.authenticated ? "Admin sign-in required to program radios"
      : radioTaskPending() ? "A radio task is in progress. Keep the radio connected and powered."
      : latestStatus?.software_updates?.pending === true || softwareUpdateBusy ? "Wait for the software update task to finish"
      : state?.available !== true || state.configured !== true ? "Install the programming helper and configure the RX / TX AIOC assignments on the Pi"
      : state?.catalog_configured !== true ? "Configure channel pairs locally on the Pi before programming defaults. Read-only inspection and the editor remain available."
      : unfinished.includes("programmer") ? `External programmer upload unfinished. Open /radio-editor and select External programmer with the ${externalProgrammerProfile(state)} profile; new RX/TX programming stays locked.`
      : unfinished.length ? `${unfinished.map(radioConnectionLabel).join(", ")} ${unfinished.length === 1 ? "needs" : "need"} verification. Use the matching Verify button, or open /radio-editor to review recovery.`
      : "Inspect each radio first. Programming starts only after you confirm.");
    renderUnfinishedRadioNotice(state);
  }

  function renderRadioProgramming(state) {
    const pending = state?.pending === true;
    const verificationRoles = radioVerificationRoles(state);
    const needsVerification = verificationRoles.length > 0;
    const failed = !pending && state?.success === false;
    const role = state?.role === "programmer" ? "Programming port" : ["rx", "tx"].includes(state?.role) ? state.role.toUpperCase() : "Radio";
    const actions = { inspect: "Inspecting", program: "Programming", verify: "Verifying", "repair-upload": "Repairing", "editor-read": "Reading", "editor-write": "Programming", "change-pair": "Changing pair" };
    if (radioProgrammingAttempt && typeof state?.request_id === "string" && state.request_id !== radioProgrammingAttempt.priorRequestId) {
      radioProgrammingError = "";
      if (!pending) radioProgrammingAttempt = null;
    }
    pill("radio-programming-state", pending ? state?.action === "change-pair" ? "Changing pair · RX → TX" : `${actions[state?.action] || "Working"} ${role}`
      : state?.preview === true ? "Preview" : needsVerification ? verificationRoles.length === 1 && verificationRoles[0] === "programmer" ? "Verify external programmer" : "Verify radio"
      : failed ? "Check radio" : state?.available !== true || state.configured !== true ? "Not configured"
      : state?.success === true ? "Task complete" : "Ready", pending || needsVerification ? "amber" : failed ? "error" : state?.success === true ? "good" : "muted");
    const validPlan = radioPlanKey(state?.plan);
    for (const itemRole of ["rx", "tx"]) {
      if (validPlan) {
        setText(`program-${itemRole}-frequency`, `${Number(state.plan[itemRole].frequency_mhz).toFixed(3)} MHz`);
        setText(`program-${itemRole}-detail`, radioPlanDescription(itemRole, state.plan));
      }
      if (!validPlan) { setText(`program-${itemRole}-frequency`,"Not configured"); setText(`program-${itemRole}-detail`,"Configure channel pairs locally on the Pi first."); }
      const radio = state?.roles?.[itemRole];
      setText(`radio-${itemRole}-identity`, radio?.inspected === true
        ? `UV-5RX / 5RX profile · Firmware: ${textOrAbsent(radio.firmware)}${radio.compatible !== true ? " · Profile review needed" : radio.verification_required === true ? " · Verification needed" : radio.verified === true ? radio.program_kind === "pair" ? " · Paired channel settings verified; default template shown above" : radio.program_kind === "editor" ? " · Custom edits verified; default plan shown above" : radio.program_kind === "repeater" ? " · Default plan verified" : " · Saved upload verified; reapply the default plan for High power and Auto Lock" : " · Ready to program"}`
        : "UV-5RX / 5RX profile · Inspect this radio before programming.");
    }
    const backup = typeof state?.backup_id === "string" && state.backup_id.trim() ? state.backup_id : "";
    byId("radio-programming-backup").hidden = !backup;
    setText("radio-programming-backup", backup ? `Local backup: ${backup}` : "");
    const detail = typeof state?.message === "string" && state.message.trim() ? state.message : "";
    if (radioProgrammingError) message("radio-programming-message", radioProgrammingError, "error");
    else if (pending) message("radio-programming-message", detail || `${actions[state?.action] || "Working on"} the ${role} radio. Keep it connected and powered.`);
    else if (verificationRoles.includes("programmer")) message("radio-programming-message", `${state?.role === "programmer" && detail ? `${detail} ` : ""}The external programmer has an unfinished upload. New RX/TX programming remains locked even if RX and TX show verified. Open the radio editor, select External programmer with the ${externalProgrammerProfile(state)} profile, and confirm which physical radio is connected before verifying or reviewing recovery. Radio audio remains paused.`, "error");
    else if (needsVerification && state?.action === "change-pair") message("radio-programming-message", `${detail ? `${detail} ` : ""}The paired operation is incomplete and radio audio remains paused. Review the paired task result before a fresh, password-confirmed retry. Recovery editing is available at /radio-editor.`, "error");
    else if (needsVerification) message("radio-programming-message", `${detail ? `${detail} ` : ""}${verificationRoles.map(radioConnectionLabel).join(" and ")} ${verificationRoles.length === 1 ? "needs" : "need"} verification. Use the matching Verify button to read back the saved upload. Radio audio remains paused until verification succeeds. If the radio does not reconnect, restart it before retrying. Recovery editing is available at /radio-editor.`.trim(), "error");
    else if (state?.preview === true) message("radio-programming-message", "Preview only — radio inspection and programming are disabled on this computer.");
    else if (failed) message("radio-programming-message", detail || "The radio task did not complete. Review the connection and result before trying again.", "error");
    else message("radio-programming-message", detail || (state?.available !== true || state.configured !== true ? "Install the current Pi helper and configure the assigned AIOC for each radio." : "Connect and inspect the selected radio before programming."), state?.success === true ? "success" : "");
    renderRadioProgress(state);
    renderRadioPair(state);
    renderAdmin();
  }

  function renderRadioProgress(state) {
    const metric = state?.progress;
    const valid = metric && Number.isInteger(metric.current) && Number.isInteger(metric.total)
      && metric.total > 0 && metric.current >= 0 && metric.current <= metric.total
      && Number.isInteger(metric.percent) && metric.percent >= 0 && metric.percent <= 100
      && ["blocks", "steps"].includes(metric.unit);
    const events = Array.isArray(state?.events) ? state.events.slice(-20).filter((event) =>
      event && typeof event.message === "string" && event.message.length <= 1000 && typeof event.time === "string") : [];
    byId("radio-progress-panel").hidden = !radioToolsNeedAttention(state) && !valid && !events.length;
    const phases = { queued: "Queued", preflight: "Checking connection", reading: "Reading radio",
      "backing-up": "Saving local backup", writing: "Writing changes", verifying: "Verifying radio",
      inspected: "Inspection complete", verified: "Verification complete", "editor-ready": "Radio read complete",
      "verification-required": "Verification needs attention", configured: "Pair configured", "pair-preflight": "Checking both radios", "pair-verifying": "Verifying both radios", "activation-required": "Activation confirmation needed", failed: "Operation stopped" };
    const pairPhase=state?.action === "change-pair" && typeof state.phase === "string" ? /^(reading|backing-up|writing|verifying)-(rx|tx)$/.exec(state.phase) : null;
    const phase = phases[pairPhase ? pairPhase[1] : state?.phase] || (state?.pending === true ? "Working on radio" : state?.success === true ? "Complete" : "Radio activity");
    setText("radio-progress-phase", `${pairPhase ? `${pairPhase[2].toUpperCase()} · ` : state?.role === "programmer" ? "Programming port · " : ["rx", "tx"].includes(state?.role) ? `${state.role.toUpperCase()} · ` : ""}${phase}`);
    const bar = byId("radio-progress-bar");
    if (valid) {
      bar.value = metric.percent;
      bar.setAttribute("value", String(metric.percent));
      setText("radio-progress-count", `${metric.current} / ${metric.total} ${metric.unit} · ${metric.percent}%`);
    } else {
      bar.removeAttribute("value");
      setText("radio-progress-count", state?.pending === true ? "In progress" : "");
    }
    setText("radio-progress-note", state?.pending === true ? state.action === "change-pair" ? "RX and TX are processed in sequence. Progress is for the current step; keep both radios connected and powered." : "Progress is for the current step. Keep the radio connected and powered."
      : radioToolsNeedAttention(state) ? "Review the result above before another operation. The activity log is retained."
      : "Last operation complete. Progress is for its last step.");
    const lines = events.map((event) => {
      const time = new Date(event.time);
      return `${Number.isNaN(time.getTime()) ? "" : `${time.toLocaleTimeString()}  `}${event.message}`;
    }).join("\n");
    const log = byId("radio-progress-events");
    if (log.textContent !== lines) log.textContent = lines;
    byId("radio-progress-log").hidden = !events.length;
  }

  async function refreshRadioProgress() {
    if (document.hidden || radioProgressRefreshing || latestStatus?.radio_programming?.pending !== true) return;
    radioProgressRefreshing = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch("/api/radio-programming/status", {
        cache: "no-store", signal: controller.signal, headers: { Accept: "application/json" }
      });
      if (!response.ok) throw new Error("Radio status unavailable");
      const data = await response.json();
      if (!data?.radio_programming || typeof data.radio_programming !== "object" || Array.isArray(data.radio_programming)) {
        throw new Error("Invalid radio status");
      }
      latestStatus = { ...latestStatus, radio_programming: data.radio_programming };
      renderRadioProgramming(data.radio_programming);
    } catch (_) {
      setText("radio-progress-note", "Progress connection interrupted. Stay on the box’s Wi-Fi; reconnecting automatically. Do not repeat the upload.");
    } finally {
      clearTimeout(timeout);
      radioProgressRefreshing = false;
    }
  }

  function renderSetupGuidance() {
    // Configuration belongs to the box, not a browser session or sign-in state.
    // A saved AP is still configured while inactive or its helper is unavailable.
    const data = latestStatus;
    const hotspotConfigured = typeof data?.wifi_visibility?.profile_hidden === "boolean" || data?.network?.hotspot_active === true;
    byId("admin-setup-help").hidden = !sessionKnown || session.configured;
    for (const id of ["network-setup-help", "access-setup-help", "hotspot-setup-help", "checklist-wifi"]) {
      byId(id).hidden = !data || hotspotConfigured;
    }
    byId("checklist-pi").hidden = !data || (data.is_pi === true && data.svxlink?.installed === true);
    const radios = data?.radio_programming;
    const operation = operationState(data);
    const liveCommissioned=operation.key === "live";
    const capture = data?.audio?.capture;
    const playback = data?.audio?.playback;
    const mappedAudio = Array.isArray(capture) && capture.some((card) => card?.id === "AIOC_RX")
      && Array.isArray(playback) && playback.some((card) => card?.id === "AIOC_TX");
    const aiocsAssigned = liveCommissioned && mappedAudio || data?.is_pi === true && radios?.configured === true && radios.preview === false
      && Array.isArray(radios.configured_roles) && ["rx", "tx"].every((role) => radios.configured_roles.includes(role)
        && radios.roles?.[role]?.inspected === true && radios.roles[role].compatible === true)
      && mappedAudio;
    byId("checklist-aioc").hidden = aiocsAssigned;
    setText("interfaces-description",aiocsAssigned ? "Stable RX capture and TX playback roles are assigned. Detected devices are listed below."
      : operation.key === "hardware" ? "An assigned radio audio interface is missing from the current device inventory. Check the RX and TX AIOC connections."
      : mappedAudio ? "RX capture and TX playback devices are detected. Their saved assignments still need confirmation."
      : "Detected devices are listed as the operating system sees them. Confirm the RX capture and TX playback assignments on the Pi.");
    const operationAttention = ["stopped","failed","hardware"].includes(operation.key) || radioToolsNeedAttention(radios);
    setText("commissioning-eyebrow",operationAttention ? "Radio status" : "Station setup");
    setText("commissioning-title",operationAttention ? "Radio status needs attention" : "Complete station setup");
    setText("commissioning-label",operationAttention ? "Status checks" : "Setup checklist");
    setText("checklist-aioc-title",operationAttention ? "Check radio interfaces" : "Assign both AIOCs");
    setText("checklist-aioc-description",operationAttention ? "Check that the RX and TX AIOCs are connected in their assigned USB ports and visible to the Pi." : "Identify each interface, label RX and TX, and confirm stable audio and serial device mappings.");
    setText("checklist-radio-title",operationAttention ? "Restore radio operation" : "Check receive and transmit operation");
    setText("checklist-radio-description",operationAttention ? "Review the radio service status and any unfinished programming before restoring live operation." : "Verify PTT and receive detection, adjust audio levels, and configure identification and time-out limits before on-air testing.");
    setText("transmit-setup-help",operationAttention ? "Live radio operation needs attention. Check the radio service, connected interfaces, and any unfinished programming." : "Configure and test audio routing, receive detection, PTT, station identification, and time-out limits before on-air use.");
    byId("checklist-radio").hidden=liveCommissioned;
    byId("transmit-setup-help").hidden=liveCommissioned;
    byId("commissioning-card").hidden=!!data && ["checklist-pi","checklist-wifi","checklist-aioc","checklist-radio"].every((id) => byId(id).hidden) && !radioToolsNeedAttention(radios);
    const receiveMonitorReady = ["receive_only", "repeater"].includes(data?.svxlink?.mode);
    const audioSeen = typeof data?.live_audio?.last_packet_at === "string" && data.live_audio.last_packet_at.trim() !== "";
    setText("audio-setup-title",operationAttention ? "Check radio audio" : "First audio setup");
    setText("audio-setup-description",operationAttention ? "Check the Pi’s radio service and connected AIOCs. A stopped service or missing interface prevents radio audio from reaching this player." : "The receive AIOC must be assigned and the Pi audio service enabled before this player can receive traffic. A connected USB interface alone does not confirm radio audio.");
    setText("input-setup-help",operationAttention ? "Restore the radio audio service and check its connected AIOC on the Pi." : "Assign the receive source and enable its audio service on the Pi first.");
    byId("audio-setup-help").hidden = !data || liveCommissioned || (receiveMonitorReady && audioSeen);
    byId("input-setup-help").hidden = !data || receiveMonitorReady;
    byId("input-enabled").setAttribute("aria-describedby", !data || receiveMonitorReady ? "input-enabled-help" : "input-enabled-help input-setup-help");
  }

  function renderWifiVisibility(wifi) {
    const hidden = typeof wifi?.profile_hidden === "boolean" ? wifi.profile_hidden : null;
    const requested = typeof wifi?.requested_hidden === "boolean" ? wifi.requested_hidden : null;
    const ssid = typeof wifi?.ssid === "string" && wifi.ssid.trim() ? wifi.ssid : latestStatus?.network?.ssid;
    const networkName = typeof ssid === "string" && ssid.trim() ? ssid : "the box’s Wi-Fi";
    const pending = wifi?.pending === true;
    const preview = wifi?.preview === true;
    const applied = !pending && wifi?.success === true && requested !== null && hidden === requested;
    const failed = !pending && wifi?.success === false;
    const detail = typeof wifi?.message === "string" && wifi.message.trim() ? wifi.message : "";
    if (wifiVisibilityAttempt && typeof wifi?.request_id === "string" && wifi.request_id !== wifiVisibilityAttempt.priorRequestId && requested === wifiVisibilityAttempt.hidden) {
      wifiVisibilityError = "";
      if (!pending) wifiVisibilityAttempt = null;
    }
    setText("wifi-visibility-ssid", textOrAbsent(ssid));
    setText("wifi-visibility-current", hidden === null ? "Unknown" : hidden ? "Hidden" : "Broadcast in Wi-Fi lists");
    const switchOn = pending && requested !== null ? !requested : hidden === false;
    byId("wifi-broadcast").setAttribute("aria-checked", String(switchOn));
    setText("wifi-switch-value", hidden === null && !pending ? "Unknown" : switchOn ? "On" : "Off");
    pill("wifi-visibility-state", pending ? "Applying" : preview ? "Preview" : failed ? "Not applied" : wifi?.available !== true ? "Unavailable" : hidden === null ? "Unknown" : hidden ? "Hidden" : "Visible", pending ? "amber" : failed ? "error" : wifi?.available === true && hidden !== null ? "good" : "muted");
    if (pending || wifiVisibilityBusy) {
      message("wifi-visibility-message", `Applying — Wi-Fi may disconnect briefly. Reconnect to ${networkName} and refresh.`);
    } else if (preview) {
      message("wifi-visibility-message", "Preview only — applies on the Pi. No Wi-Fi settings are changed on this computer.");
    } else if (wifiVisibilityError) {
      message("wifi-visibility-message", wifiVisibilityError, "error");
    } else if (!wifi) {
      message("wifi-visibility-message", "Wi-Fi visibility is unavailable. Refresh to check the Pi connection.");
    } else if (wifi.available !== true) {
      message("wifi-visibility-message", detail || "Wi-Fi visibility controls are unavailable on this device.");
    } else if (failed) {
      message("wifi-visibility-message", detail || "The requested visibility could not be applied. Check the saved visibility above before trying again.", "error");
    } else if (applied) {
      message("wifi-visibility-message", detail || "Wi-Fi visibility applied. The network name and password are unchanged.", "success");
    } else {
      message("wifi-visibility-message", detail || "The saved visibility above comes from the Pi’s Wi-Fi profile.");
    }
    renderAdmin();
  }

  async function readJson(response) {
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("The Pi returned an unreadable response."); }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        session.authenticated = false;
        session.csrf_token = null;
        renderAdmin();
      }
      throw new Error(typeof data.error === "string" ? data.error : typeof data.message === "string" ? data.message : `Request failed (${response.status}).`);
    }
    return data;
  }

  async function loadSession() {
    try {
      const response = await fetch("/api/session", { cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" } });
      const data = await readJson(response);
      if (typeof data.configured !== "boolean" || typeof data.authenticated !== "boolean") throw new Error("Admin status is unavailable.");
      session = { configured: data.configured, authenticated: data.authenticated, csrf_token: typeof data.csrf_token === "string" ? data.csrf_token : null };
      sessionKnown = true;
      renderAdmin();
    } catch (_) {
      if (!sessionKnown) message("admin-message", "Admin status is unavailable. Check the Pi connection and refresh.", "error");
    }
  }

  async function postJson(path, body, authenticated = true) {
    if (authenticated && (!session.authenticated || !session.csrf_token)) {
      await loadSession();
      if (!session.authenticated || !session.csrf_token) throw new Error("Sign in as admin before changing this setting.");
    }
    const headers = { "Content-Type": "application/json", Accept: "application/json" };
    if (authenticated) headers["X-CSRF-Token"] = session.csrf_token;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      return await readJson(await fetch(path, { method: "POST", credentials: "same-origin", cache: "no-store", headers, body: JSON.stringify(body), signal: controller.signal }));
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The Pi took too long to respond. Refresh to check whether the change was applied.");
      if (error instanceof TypeError) throw new Error("Cannot reach the Pi. Check your Wi-Fi connection and try again.");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function radioMessageEntries() {
    const entries = latestStatus?.radio_messages?.messages;
    return Array.isArray(entries) ? entries.filter((entry) => entry && /^[0-9a-f]{32}$/.test(entry.id) &&
      /^[0-9a-f]{64}$/.test(entry.sha256) && typeof entry.name === "string" &&
      finite(entry.duration_seconds) && entry.duration_seconds > 0 && entry.duration_seconds <= 30 &&
      finite(entry.bytes) && entry.bytes > 0) : [];
  }

  function radioMessagePreviewUrl(entry) {
    if (!entry || typeof entry.preview_url !== "string") return null;
    const expected = `/api/messages/${entry.id}/audio`;
    try {
      const url = new URL(entry.preview_url, location.origin);
      return url.origin === location.origin && url.pathname === expected && !url.search && !url.hash &&
        entry.preview_url === expected ? url.href : null;
    } catch (_) { return null; }
  }

  function radioMessageMaintenanceClear(ignoreBusy = false) {
    const radio = latestStatus?.radio_programming;
    return sessionKnown && session.authenticated && typeof session.csrf_token === "string" && !!session.csrf_token &&
      (ignoreBusy || !mutationBusy) && !radioMessageActionBusy && !radioMessageUncertain && !radioProgrammingBusy && !softwareUpdateBusy && !wifiVisibilityBusy &&
      latestStatus?.software_updates?.pending !== true && latestStatus?.wifi_visibility?.pending !== true &&
      !radioTaskPending() && !dtmfCodePending() && !messageTaskPending() && !radioVerificationRoles(radio).length && radio?.pair?.verification_required !== true &&
      latestStatus?.radio_messages?.upload_pending !== true && latestStatus?.radio_messages?.job?.pending !== true;
  }

  function radioMessageSnapshot(action, entry) {
    const state = latestStatus?.radio_messages;
    if (!entry || !session.authenticated || !session.csrf_token) return null;
    const snapshot = {action, id:entry.id, sha256:entry.sha256, csrf:session.csrf_token};
    if (action === "broadcast") {
      if (typeof state?.pair_id !== "string" || !state.pair_id || !/^[0-9a-f]{64}$/.test(state.catalog_digest) ||
          !/^[0-9a-f]{32}$/.test(state.invocation_id)) return null;
      Object.assign(snapshot, {pair_id:state.pair_id, catalog_digest:state.catalog_digest, invocation_id:state.invocation_id});
    }
    return snapshot;
  }

  function radioMessageAllowed(action, ignoreBusy = false) {
    const state = latestStatus?.radio_messages;
    const entry = radioMessageEntries().find((item) => item.id === radioMessageSelection);
    if (state?.available !== true || !radioMessageMaintenanceClear(ignoreBusy)) return false;
    if (action === "upload") return state.can_upload === true;
    return !!entry && (action === "delete" || state.can_broadcast === true && !!radioMessageSnapshot(action, entry));
  }

  function radioMessageProposalCurrent(proposal) {
    const entry = radioMessageEntries().find((item) => item.id === radioMessageSelection);
    const current = radioMessageSnapshot(proposal.action, entry);
    return !!current && JSON.stringify(current) === JSON.stringify(proposal) && radioMessageAllowed(proposal.action, true);
  }

  function clearRadioMessagePreview() {
    if (!radioMessagePreviewKey) return;
    const audio = byId("radio-message-audio");
    audio.pause(); audio.removeAttribute("src"); audio.load();
    radioMessagePreviewKey = "";
  }

  function renderRadioMessages() {
    const signedIn = sessionKnown && session.authenticated;
    const state = latestStatus?.radio_messages;
    byId("radio-messages-card").hidden = !signedIn;
    const entries = signedIn ? radioMessageEntries() : [];
    const select = byId("radio-message-select");
    const key = JSON.stringify(entries.map((entry) => [entry.id,entry.sha256,entry.name]));
    if (key !== radioMessageListKey) {
      select.replaceChildren();
      const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "Select a message…"; select.append(placeholder);
      for (const entry of entries) {
        const option = document.createElement("option"); option.value = entry.id; option.textContent = entry.name; select.append(option);
      }
      radioMessageListKey = key;
    }
    if (!entries.some((entry) => entry.id === radioMessageSelection)) radioMessageSelection = "";
    select.value = radioMessageSelection;
    select.disabled = !signedIn || !entries.length || radioMessageActionBusy;
    byId("radio-message-library").hidden = !entries.length;
    byId("radio-messages-empty").hidden = entries.length > 0;
    const entry = entries.find((item) => item.id === radioMessageSelection);
    const url = radioMessagePreviewUrl(entry);
    const previewKey = url ? `${entry.id}:${entry.sha256}:${url}` : "";
    if (previewKey !== radioMessagePreviewKey) {
      clearRadioMessagePreview();
      if (url) { byId("radio-message-audio").src = url; radioMessagePreviewKey = previewKey; }
    }
    byId("radio-message-preview").hidden = !entry;
    byId("radio-message-audio").hidden = !url;
    setText("radio-message-name", entry?.name || "");
    setText("radio-message-detail", entry ? `${entry.duration_seconds.toFixed(1)} seconds · ${formatBytes(entry.bytes)} · ${localTime(entry.created_at,true)}${url ? "" : " · Preview unavailable"}` : "");
    const uploadAllowed = radioMessageAllowed("upload");
    const limits = state?.limits;
    setText("radio-message-limits", `WAV, MP3 or M4A · Up to ${finite(limits?.max_duration_seconds) ? limits.max_duration_seconds : 30} seconds and ${formatBytes(finite(limits?.max_upload_bytes) ? limits.max_upload_bytes : 8388608,0)} · ${finite(limits?.max_messages) ? limits.max_messages : 10} saved messages`);
    const draft = byId("radio-message-file").files?.[0];
    setText("radio-message-file-detail", draft ? `${draft.name} · ${formatBytes(draft.size)}` : "No file selected");
    byId("radio-message-file").disabled = !uploadAllowed;
    byId("radio-message-upload").disabled = !uploadAllowed || !draft;
    byId("radio-message-delete").disabled = !radioMessageAllowed("delete");
    byId("radio-message-broadcast").disabled = !radioMessageAllowed("broadcast");
    const pending = messageTaskPending();
    const job = state?.job;
    const phases = {queued:"Queued", accepted:"Preparing broadcast", playing:"Broadcasting", finished:"Broadcast complete", failed:"Broadcast failed", rejected:"Broadcast rejected", interrupted:"Broadcast interrupted", expired:"Broadcast expired"};
    pill("radio-messages-state", pending ? "In progress" : state?.available === true ? `${entries.length} saved` : "Unavailable", pending ? "amber" : state?.available === true ? "good" : "muted");
    setText("radio-messages-availability", state?.reason || (state?.available === true ? "Preview is local. Each broadcast sends the selected clip once on the configured pair." : "Install and configure the Pi message helper to use saved broadcasts."));
    byId("radio-message-job").hidden = !pending && !job;
    byId("radio-message-job-progress").hidden = !pending;
    setText("radio-message-job-phase", state?.upload_pending === true ? "Saving audio message" : radioMessageUncertain ? "Confirming message result" : radioMessageActionBusy ? "Processing message request" : phases[job?.phase] || (pending ? "Message in progress" : "Message result"));
    setText("radio-message-job-detail", job?.message || (pending ? "Keep the Pi connected. The current message task will appear here." : ""));
    message("radio-message-message", radioMessageError, radioMessageError ? "error" : "");
    if (radioMessageProposal && !radioMessageProposalCurrent(radioMessageProposal)) {
      radioMessageProposal = null;
      if (byId("radio-message-confirm-dialog").open) byId("radio-message-confirm-dialog").close();
      radioMessageError = "The message, radio status, or admin session changed. Review the current details again.";
      message("radio-message-message", radioMessageError, "error");
    }
    byId("radio-message-confirm-apply").disabled = !radioMessageProposal || !radioMessageAllowed(radioMessageProposal.action) || !byId("radio-message-confirm-dialog").open;
  }

  function closeRadioMessageConfirmation() {
    radioMessageProposal = null;
    if (byId("radio-message-confirm-dialog").open) byId("radio-message-confirm-dialog").close();
    renderRadioMessages();
  }

  function showRadioMessageConfirmation(action) {
    if (!radioMessageAllowed(action)) return;
    const entry = radioMessageEntries().find((item) => item.id === radioMessageSelection);
    radioMessageProposal = radioMessageSnapshot(action,entry);
    radioMessageError = "";
    const broadcast = action === "broadcast";
    setText("radio-message-confirm-title", broadcast ? "Broadcast this message once?" : "Delete this saved message?");
    setText("radio-message-confirm-description", `${entry.name} · ${entry.duration_seconds.toFixed(1)} seconds`);
    setText("radio-message-confirm-effect", broadcast
      ? "The repeater will transmit this clip once using the current configured channel pair. New recordings include it when recording and outgoing audio are enabled."
      : "This removes the saved clip from the Pi’s message library. Existing traffic recordings are kept.");
    setText("radio-message-confirm-apply", broadcast ? "Broadcast once" : "Delete message");
    message("radio-message-confirm-error", "");
    const dialog = byId("radio-message-confirm-dialog");
    if (typeof dialog.showModal !== "function") {
      radioMessageProposal = null; radioMessageError = "Use a current browser to review and confirm this action."; renderRadioMessages(); return;
    }
    dialog.showModal(); renderRadioMessages(); byId("radio-message-confirm-cancel").focus();
  }

  async function applyRadioMessage() {
    const proposal = radioMessageProposal;
    if (!proposal || !byId("radio-message-confirm-dialog").open || !radioMessageAllowed(proposal.action)) return;
    mutationBusy = true; renderAdmin();
    const previousUpdate = lastSuccess;
    await refreshAfterMutation();
    mutationBusy = false;
    if (lastSuccess === previousUpdate || radioMessageProposal !== proposal || !byId("radio-message-confirm-dialog").open || !radioMessageProposalCurrent(proposal)) {
      closeRadioMessageConfirmation(); radioMessageError = "The current message and radio status could not be confirmed. Review the details and try again."; renderAdmin(); return;
    }
    closeRadioMessageConfirmation();
    radioMessageActionBusy = true; radioMessageError = ""; renderAdmin();
    if (proposal.action === "broadcast") radioMessageUncertain = true;
    try {
      const payload = {id:proposal.id,sha256:proposal.sha256};
      if (proposal.action === "broadcast") Object.assign(payload,{pair_id:proposal.pair_id,catalog_digest:proposal.catalog_digest,invocation_id:proposal.invocation_id});
      const result = await postJson(`/api/messages/${proposal.action}`,payload);
      if (!result.radio_messages || typeof result.radio_messages !== "object") throw new Error("The Pi returned an unreadable message status.");
      radioMessageUncertain = false;
      latestStatus = {...latestStatus,radio_messages:result.radio_messages};
    } catch (error) { radioMessageError = `${error.message} Check the message result before trying again.`; }
    finally { radioMessageActionBusy = false; await refreshAfterMutation(); renderAdmin(); }
  }

  async function uploadRadioMessage(event) {
    event.preventDefault();
    if (!radioMessageAllowed("upload")) return;
    const file = byId("radio-message-file").files?.[0];
    const limits = latestStatus.radio_messages.limits;
    const extension = typeof file?.name === "string" ? file.name.split(".").pop().toLowerCase() : "";
    if (!file || !finite(file.size) || file.size <= 0 || file.size > Math.min(limits?.max_upload_bytes || 8388608,8388608) || !["wav","mp3","m4a"].includes(extension)) {
      radioMessageError = "Choose a nonempty WAV, MP3 or M4A file up to 8 MB. The Pi checks the 30-second duration before saving."; renderRadioMessages(); return;
    }
    const csrf = session.csrf_token;
    radioMessageActionBusy = true; radioMessageError = ""; renderAdmin();
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(),60000);
    try {
      const response = await fetch("/api/messages/upload", {method:"POST",cache:"no-store",credentials:"same-origin",signal:controller.signal,
        headers:{"Content-Type":"application/octet-stream","X-File-Name":encodeURIComponent(file.name),"X-CSRF-Token":csrf,Accept:"application/json"},body:file});
      const result = await readJson(response);
      if (!result.radio_messages || typeof result.radio_messages !== "object") throw new Error("The Pi returned an unreadable message status.");
      if (session.authenticated && session.csrf_token === csrf) {
        latestStatus = {...latestStatus,radio_messages:result.radio_messages};
        radioMessageSelection = typeof result.message?.id === "string" ? result.message.id : "";
        byId("radio-message-file").value = "";
      }
    } catch (error) { radioMessageError = error.name === "AbortError" ? "The upload timed out. Check the message library before uploading again." : error.message; }
    finally { clearTimeout(timeout); radioMessageActionBusy = false; await refreshAfterMutation(); renderAdmin(); }
  }

  async function refreshRadioMessages() {
    if (radioMessageRefreshing || document.hidden || !session.authenticated || !messageTaskPending() || radioMessageActionBusy) return;
    const csrf = session.csrf_token;
    radioMessageRefreshing = true;
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(),8000);
    try {
      const response = await fetch("/api/messages",{cache:"no-store",credentials:"same-origin",signal:controller.signal,headers:{Accept:"application/json"}});
      const result = await readJson(response);
      if (session.authenticated && session.csrf_token === csrf && result.radio_messages && typeof result.radio_messages === "object") {
        radioMessageUncertain = false; latestStatus = {...latestStatus,radio_messages:result.radio_messages}; renderAdmin();
      }
    } catch (_) { /* Keep the pending state until the Pi confirms a result. */ }
    finally { clearTimeout(timeout); radioMessageRefreshing = false; }
  }

  byId("radio-message-upload-form").addEventListener("submit",uploadRadioMessage);
  byId("radio-message-file").addEventListener("change",() => { radioMessageError = ""; renderRadioMessages(); });
  byId("radio-message-select").addEventListener("change",(event) => { radioMessageSelection = event.target.value; renderRadioMessages(); });
  byId("radio-message-broadcast").addEventListener("click",() => showRadioMessageConfirmation("broadcast"));
  byId("radio-message-delete").addEventListener("click",() => showRadioMessageConfirmation("delete"));
  byId("radio-message-confirm-cancel").addEventListener("click",closeRadioMessageConfirmation);
  byId("radio-message-confirm-apply").addEventListener("click",applyRadioMessage);
  byId("radio-message-confirm-dialog").addEventListener("cancel",(event) => { event.preventDefault(); closeRadioMessageConfirmation(); });
  byId("radio-message-confirm-dialog").addEventListener("close",() => { radioMessageProposal = null; });
  byId("radio-message-audio").addEventListener("play",() => { if (liveState !== "stopped") stopListening(true); });

  function renderSettings(settings) {
    if (!settings || settingsDirty) return;
    byId("station-name").value = typeof settings.station_name === "string" ? settings.station_name : "";
    byId("retention-days").value = finite(settings.retention_days) ? settings.retention_days : "";
    byId("archive-limit").value = finite(settings.max_archive_mb) ? settings.max_archive_mb : "";
    byId("recording-threshold").value = finite(settings.recording_threshold_dbfs) ? settings.recording_threshold_dbfs : -45;
    byId("input-enabled").checked = settings.input_enabled === true;
    byId("monitor-transmit").checked = settings.monitor_transmit_audio !== false;
    byId("record-startup").checked = settings.record_on_startup === true;
    byId("keep-indefinitely").checked = settings.keep_indefinitely === true;
    renderSettingsSwitches();
    renderRecordingThreshold();
    if (typeof settings.station_name === "string" && settings.station_name.trim()) {
      const nameNode = document.querySelector(".brand > span:last-child").firstChild;
      if (nameNode?.nodeType === Node.TEXT_NODE) nameNode.nodeValue = settings.station_name;
      document.title = `${settings.station_name} · Local dashboard`;
    }
  }

  function renderSettingsSwitches() {
    setText("input-enabled-value", byId("input-enabled").checked ? "On" : "Off");
    setText("monitor-transmit-value", byId("monitor-transmit").checked ? "On" : "Off");
    setText("record-startup-value", byId("record-startup").checked ? "On" : "Off");
    const keepIndefinitely = byId("keep-indefinitely").checked;
    setText("keep-indefinitely-value", keepIndefinitely ? "On" : "Off");
    byId("retention-days").disabled = keepIndefinitely;
    byId("retention-days").hidden = keepIndefinitely;
    byId("retention-infinity").hidden = !keepIndefinitely;
    byId("retention-days-unit").hidden = keepIndefinitely;
    setText("retention-infinity", "∞");
    setText("retention-days-help", keepIndefinitely ? "Clips are kept until storage space is needed." : "Older archived files are removed automatically.");
  }

  function renderRecordingThreshold() {
    const slider = byId("recording-threshold");
    const threshold = Number(slider.value);
    const description = threshold <= -55
      ? "High sensitivity: quiet speech and background noise can start a recording."
      : threshold <= -35
        ? "Medium sensitivity: adjust against your receiver’s normal speech and idle noise."
        : "Low sensitivity: louder audio is needed to start a recording; quiet speech may be missed.";
    setText("recording-threshold-value", `${threshold} dBFS`);
    setText("recording-threshold-description", description);
    slider.setAttribute("aria-valuetext", `${threshold} dBFS. ${description}`);
  }

  function renderRecorder(recorder) {
    if (!recorder) {
      pill("recorder-state", "Unavailable");
      setText("recorder-description", "Recording status unavailable.");
      setText("recorder-message", "Connect to the Pi to check the recorder.");
      return;
    }
    if (!mutationBusy) byId("recording-toggle").checked = recorder.enabled === true;
    let label = "Off", detail = "No new audio is being saved.", kind = "muted";
    if (recorder.active === true) {
      label = "Recording"; kind = "good";
      detail = typeof recorder.current_file === "string" ? `Saving ${recorder.current_file}` : "Saving radio audio.";
    } else if (recorder.enabled === true) {
      label = "Armed"; kind = "amber"; detail = "Waiting for radio audio above the recording threshold.";
    }
    if (typeof recorder.last_error === "string" && recorder.last_error.trim()) {
      label = "Check audio"; kind = "error"; detail = recorder.last_error;
    }
    pill("recorder-state", label, kind);
    setText("recorder-description", recorder.enabled === true ? "Recording is enabled for the selected radio audio feed." : "Recording is turned off. Existing archives remain available.");
    setText("recorder-message", detail);
  }

  function liveUrl(source) {
    if (typeof source !== "string" || !source.startsWith("/") || source.startsWith("//")) return null;
    try {
      const url = new URL(source, location.origin);
      return url.origin === location.origin && url.pathname === "/api/live.pcm" ? url.href : null;
    } catch (_) { return null; }
  }

  function audioSupported() {
    return Boolean((window.AudioContext || window.webkitAudioContext) && window.ReadableStream && window.AbortController);
  }

  function renderLive(live) {
    const configured = live?.configured === true;
    const receiving = live?.receiving === true;
    const outgoing = live?.source === "tx_mix";
    const sourceError = typeof live?.last_error === "string" && live.last_error.trim();
    pill("live-source-state", !live ? "Unavailable" : sourceError ? "Check feed" : !configured ? "Not configured" : receiving ? "Feed connected" : "Waiting for feed", sourceError ? "error" : receiving ? "good" : configured ? "amber" : "muted");
    setText("live-input-state", !live ? absent : sourceError ? live.last_error : !configured ? "Radio feed not configured"
      : outgoing ? receiving ? "Repeater audio arriving · outgoing included" : "Repeater audio · waiting for traffic"
      : receiving ? "Receiver audio arriving · receiver only" : "Receiver audio · waiting for traffic");
    setText("live-listeners", finite(live?.listeners) ? String(live.listeners) : absent);
    setText("live-format", finite(live?.sample_rate) && finite(live?.channels) ? `${(live.sample_rate / 1000).toFixed(live.sample_rate % 1000 ? 1 : 0)} kHz · ${live.channels === 1 ? "mono" : `${live.channels} channels`}` : absent);
    setText("live-last-packet", live?.last_packet_at ? localTime(live.last_packet_at) : "No audio received yet");
    const canListen = configured && !sourceError && audioSupported() && live?.format === "f32le" && live?.channels === 1 && finite(live?.sample_rate) && liveUrl(live.stream_url);
    byId("listen-button").disabled = !canListen || liveState !== "stopped";
    byId("stop-button").disabled = liveState === "stopped";
    if (liveState === "stopped") {
      setText("live-player-state", !audioSupported() ? "Browser audio is unavailable" : !live ? "Waiting for the Pi" : sourceError ? "Check the radio feed" : !configured ? "Ready when the receiver is connected" : "Ready to listen");
      setText("live-player-description", !audioSupported() ? "Try a current Safari, Chrome, Firefox, or Edge browser, or use the audio archives."
        : sourceError ? live.last_error : !configured ? "Assign the receiver audio source and enable its feed on the Pi to start listening."
        : outgoing ? "Tap Start listening to hear received traffic, menu replies, identification and broadcast messages. This is the Pi’s audio feed, not an off-air reception test."
        : "Tap Start listening to hear the receiver. Outgoing repeater audio is not included in the current feed.");
    }
  }

  function dataPanelVisible() {
    return dataPageActive && !document.hidden && byId("panel-data").hidden === false;
  }

  function dataAdmin() {
    return sessionKnown && session.authenticated && typeof session.csrf_token === "string" && !!session.csrf_token;
  }

  function canChangeData() {
    const value = latestStatus?.data;
    return dataAdmin() && !dataBusy && !mutationBusy && typeof value?.enabled === "boolean" &&
      (value.available === true || value.enabled === true);
  }

  function stopDataPolling() {
    if (dataTimer !== null) clearTimeout(dataTimer);
    dataTimer = null; dataGeneration += 1;
    dataController?.abort(); dataController = null;
    dataRequestForced = false;
  }

  function scheduleDataPoll() {
    if (!dataPanelVisible() || !dataAdmin() || dataBusy || sstvBusy || mediaDeleteBusy) {
      stopDataPolling(); return;
    }
    if (!decodingEnabled()) {
      if (!dataController || !dataRequestForced) stopDataPolling();
      return;
    }
    if (dataTimer !== null || dataController) return;
    dataTimer = setTimeout(() => { dataTimer = null; return refreshDataStatus(); },1000);
  }

  function decodingEnabled() {
    return latestStatus?.data?.enabled === true || latestStatus?.sstv?.enabled === true;
  }

  function dataPackets(value) {
    return Array.isArray(value?.packets) ? value.packets.slice(0,100).filter((packet) => packet &&
      typeof packet.id === "string" && packet.id.length > 0 && packet.id.length <= 128 &&
      typeof packet.source === "string" && typeof packet.destination === "string" &&
      typeof packet.payload === "string" && typeof packet.raw === "string") : [];
  }

  function renderData() {
    const signedIn = dataAdmin();
    const value = signedIn ? latestStatus?.data : null;
    byId("data-card").hidden = !signedIn;
    byId("data-auth-note").hidden = signedIn;
    if (!signedIn) {
      dataSessionToken = null; dataError = ""; sstvError = "";
      stopDataPolling();
    }
    if (!dataBusy) byId("data-enabled").checked = value?.enabled === true;
    byId("data-enabled").disabled = !canChangeData();
    setText("data-enabled-value",dataBusy ? "Saving…" : value?.enabled === true ? "On" : "Off");
    const error = typeof value?.last_error === "string" ? value.last_error : "";
    pill("data-state", dataBusy ? "Updating" : !value || value.admin_required === true ? "Unavailable"
      : value.available !== true ? "Decoder unavailable" : value.enabled !== true ? "Off"
      : value.input_enabled !== true ? "Audio feed off" : error ? "Check decoder" : value.running === true ? "Listening" : "Waiting for audio",
      dataBusy ? "amber" : error ? "error" : value?.running === true ? "good" : "muted");
    setText("data-description", !value || value.admin_required === true ? "Connect and sign in to check received-data decoding."
      : value.available !== true ? value.reason || "The receiver decoder needs its one-time Pi setup before it can be enabled."
      : value.enabled !== true ? "Decoding is off. Saved packets remain available for export."
      : value.input_enabled !== true ? "Decoding is enabled and waiting for the radio audio feed."
      : "Listening to the RX radio for APRS / AX.25 AFSK1200 packets. Listening in the browser is optional.");
    setText("data-source", [value?.pair_label,value?.rx_name].filter((item) => typeof item === "string" && item.trim()).join(" · ") || "RX input · current radio channel");
    setText("data-mode",typeof value?.mode === "string" ? value.mode : "APRS / AX.25 AFSK1200");
    setText("data-count",finite(value?.packet_count) ? String(value.packet_count) : absent);
    setText("data-last-packet",typeof value?.last_packet_at === "string" ? localTime(value.last_packet_at,true) : "None yet");
    const maxPackets = finite(value?.limits?.max_packets) ? value.limits.max_packets : 1000;
    const maxMiB = finite(value?.limits?.max_bytes) ? value.limits.max_bytes / 1048576 : 8;
    setText("data-limits-note",`APRS / AX.25 at 1200 baud. Exports contain decoded packets, not reconstructed files. The saved log keeps up to ${maxPackets.toLocaleString()} packets or ${Number.isInteger(maxMiB) ? maxMiB : maxMiB.toFixed(1)} MiB; older packets are removed as needed.`);
    byId("data-input-note").hidden = value?.enabled !== true || value?.input_enabled === true;
    message("data-message",dataError || error,dataError || error ? "error" : "");
    const packets = dataPackets(value);
    const key = JSON.stringify(packets);
    const list = byId("data-packets");
    if (key !== dataPacketKey) {
      const position = list.scrollTop;
      list.replaceChildren();
      for (const packet of packets) {
        const row = document.createElement("article"); row.className = "data-packet"; row.setAttribute("role","listitem");
        const header = document.createElement("div"); header.className = "data-packet-header";
        const route = document.createElement("strong"), timestamp = document.createElement("time");
        const path = Array.isArray(packet.path) ? packet.path.filter((item) => typeof item === "string").join(", ") : "";
        route.textContent = `${packet.source} → ${packet.destination}${path ? " · via " + path : ""}`;
        timestamp.textContent = localTime(packet.time,true);
        if (typeof packet.time === "string") timestamp.setAttribute("datetime",packet.time);
        header.append(route,timestamp);
        const payload = document.createElement("pre"); payload.textContent = packet.payload;
        const details = document.createElement("details"), summary = document.createElement("summary"), raw = document.createElement("pre");
        summary.textContent = "Packet details"; raw.textContent = packet.raw;
        details.append(summary,raw); row.append(header,payload,details); list.append(row);
      }
      if (finite(position)) list.scrollTop = position;
      dataPacketKey = key;
    }
    byId("data-empty").hidden = packets.length > 0;
    setText("data-packet-summary",packets.length ? `Showing the latest ${packets.length} of ${finite(value?.packet_count) ? value.packet_count : packets.length} saved packets · exports include the saved log` : "No decoded packets yet");
    const exportable = signedIn && finite(value?.packet_count) && value.packet_count > 0;
    byId("data-downloads").hidden = !exportable;
    for (const [id,format] of [["data-download-json","json"],["data-download-text","txt"]]) {
      if (exportable) byId(id).setAttribute("href",`/api/data/export?format=${format}`);
      else byId(id).removeAttribute("href");
    }
    renderSstv();
    if (signedIn && dataSessionToken !== session.csrf_token) {
      dataSessionToken = session.csrf_token;
      if (dataPanelVisible()) refreshDataStatus(true);
    }
    scheduleDataPoll();
  }

  async function refreshDataStatus(force = false) {
    if (!dataPanelVisible() || !dataAdmin() || dataBusy || sstvBusy || mediaDeleteBusy || dataController || (!force && !decodingEnabled())) return;
    if (dataTimer !== null) clearTimeout(dataTimer);
    dataTimer = null;
    const generation = dataGeneration, csrf = session.csrf_token;
    const controller = new AbortController(); dataController = controller; dataRequestForced = force;
    const timeout = setTimeout(() => controller.abort(),4000);
    try {
      const response = await fetch("/api/data/status",{cache:"no-store",credentials:"same-origin",signal:controller.signal,headers:{Accept:"application/json"}});
      const result = await response.json();
      if (generation !== dataGeneration || controller.signal.aborted || csrf !== session.csrf_token || !dataAdmin() || !dataPanelVisible()) return;
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) { session.authenticated = false; session.csrf_token = null; renderAdmin(); }
        throw new Error("Data status unavailable");
      }
      if (!result.data || typeof result.data !== "object" || Array.isArray(result.data)) throw new Error("Unreadable data status");
      dataRevision += 1; dataError = ""; latestStatus = {...latestStatus,data:result.data,sstv:result.sstv && typeof result.sstv === "object" && !Array.isArray(result.sstv) ? result.sstv : latestStatus?.sstv}; renderData();
    } catch (_) {
      if (generation === dataGeneration && dataAdmin() && dataPanelVisible()) {
        dataError = "Data status connection interrupted. Reconnecting automatically…"; message("data-message",dataError,"error"); renderSstv();
      }
    } finally {
      clearTimeout(timeout);
      if (dataController === controller) { dataController = null; dataRequestForced = false; }
      if (generation === dataGeneration) scheduleDataPoll();
    }
  }

  byId("data-admin-link").addEventListener("click",() => selectTab("settings",true));
  byId("data-enabled").addEventListener("change",async() => {
    if (!canChangeData()) { renderData(); return; }
    const enabled = byId("data-enabled").checked, csrf = session.csrf_token;
    dataBusy = true; mutationBusy = true; dataError = ""; stopDataPolling(); renderAdmin();
    try {
      const result = await postJson("/api/data/decoding",{enabled});
      if (!result.data || typeof result.data.enabled !== "boolean") throw new Error("The Pi returned an unreadable decoder status.");
      if (dataAdmin() && session.csrf_token === csrf) {
        dataRevision += 1; latestStatus = {...latestStatus,data:result.data};
      }
    } catch (error) { dataError = error.message; }
    finally { dataBusy = false; mutationBusy = false; renderAdmin(); }
  });

  function canChangeSstv() {
    const value = latestStatus?.sstv;
    return dataAdmin() && !sstvBusy && !mutationBusy && typeof value?.enabled === "boolean" &&
      (value.available === true || value.enabled === true);
  }

  function sstvId(value) { return typeof value === "string" && /^[0-9a-f]{32}$/.test(value); }
  function sstvDimensions(value) {
    return Number.isInteger(value?.width) && Number.isInteger(value?.height) &&
      value.width >= 1 && value.width <= 1024 && value.height >= 1 && value.height <= 1024;
  }
  function sstvImages(value) {
    return Array.isArray(value?.images) ? value.images.slice(0,50).filter(image => image && sstvId(image.id) &&
      typeof image.complete === "boolean" && sstvDimensions(image) && Number.isInteger(image.lines_received) &&
      image.lines_received >= 1 && image.lines_received <= image.height && typeof image.created_at === "string" &&
      Number.isFinite(new Date(image.created_at).getTime())) : [];
  }
  function sstvImageUrl(id, download = false) {
    return sstvId(id) ? `/api/sstv/images/${id}.png${download ? "?download=1" : ""}` : "";
  }
  function currentSstv(value) {
    const current = value?.current;
    return current && sstvId(current.id) && sstvDimensions(current) &&
      Number.isSafeInteger(current.revision) && current.revision >= 0 &&
      Number.isInteger(current.lines_received) && current.lines_received >= 0 && current.lines_received <= current.height ? current : null;
  }
  function clearSstvImage(element) {
    element.removeAttribute("src"); element.hidden = true;
  }
  function renderSstv() {
    const signedIn = dataAdmin(), value = signedIn ? latestStatus?.sstv : null;
    const visible = signedIn && dataPanelVisible();
    byId("sstv-card").hidden = !signedIn;
    if (!sstvBusy) byId("sstv-enabled").checked = value?.enabled === true;
    byId("sstv-enabled").disabled = !canChangeSstv();
    setText("sstv-enabled-value",sstvBusy ? "Saving…" : value?.enabled === true ? "On" : "Off");
    const error = typeof value?.last_error === "string" ? value.last_error : "";
    const current = currentSstv(value), receiving = value?.phase === "receiving" && current !== null;
    pill("sstv-state",sstvBusy ? "Updating" : !value || value.admin_required === true ? "Unavailable"
      : value.available !== true ? "Decoder unavailable" : value.enabled !== true ? "Off"
      : value.input_enabled !== true ? "Audio feed off" : error ? "Check decoder"
      : dataError ? "Connection interrupted" : receiving ? "Receiving picture"
      : value.phase === "interrupted" ? "Reception interrupted" : "Waiting for picture",
      sstvBusy ? "amber" : error ? "error" : receiving && !dataError ? "good" : "muted");
    setText("sstv-description",!value || value.admin_required === true ? "Connect and sign in to check SSTV reception."
      : value.available !== true ? value.reason || "The SSTV receiver needs its one-time Pi setup before it can be enabled."
      : value.enabled !== true ? "SSTV decoding is off. Saved pictures remain in the gallery."
      : value.input_enabled !== true ? "SSTV is enabled and waiting for the radio audio feed."
      : receiving ? "Receiving a Robot 36 picture from the RX radio. The preview follows the image lines received."
      : value.phase === "interrupted" ? "Reception ended before the whole picture arrived. Any saved partial picture is marked in the gallery."
      : "Waiting for a Robot 36 signal on the RX radio’s current channel. Browser listening is optional.");
    byId("sstv-input-note").hidden = value?.enabled !== true || value?.input_enabled === true;
    message("sstv-message",sstvError || error || dataError,sstvError || error || dataError ? "error" : "");
    byId("sstv-reception").hidden = !current;
    const preview = byId("sstv-current-image");
    const previewKey = visible && current && current.lines_received > 0 ? `${current.id}:${current.revision}` : "";
    if (previewKey !== sstvPreviewKey) {
      setText("sstv-preview-empty","Waiting for the first image lines.");
      clearSstvImage(preview);
      if (previewKey) {
        preview.setAttribute("src",`/api/sstv/current.png?id=${current.id}&revision=${current.revision}`);
        preview.hidden = false;
      }
      sstvPreviewKey = previewKey;
    }
    byId("sstv-preview-empty").hidden = Boolean(previewKey);
    if (current) {
      const progress = Math.min(100,Math.max(0,current.lines_received / current.height * 100));
      byId("sstv-progress").value = progress;
      byId("sstv-progress").setAttribute("aria-valuetext",`${current.lines_received} of ${current.height} image lines received`);
      setText("sstv-current-title",receiving ? "Receiving picture" : value?.phase === "interrupted" ? "Picture interrupted" : "Latest reception");
      setText("sstv-current-detail",`${typeof value.mode === "string" ? value.mode : "Robot 36"} · ${current.width} × ${current.height}${typeof current.started_at === "string" ? " · " + localTime(current.started_at,true) : ""}`);
      setText("sstv-progress-note",`${current.lines_received} of ${current.height} lines · ${Math.floor(progress)}%${dataError ? " · Preview paused while reconnecting" : ""}`);
    } else {
      byId("sstv-progress").value = 0; byId("sstv-progress").removeAttribute("aria-valuetext");
      setText("sstv-current-title","Receiving picture"); setText("sstv-current-detail",""); setText("sstv-progress-note","Waiting for image data.");
    }
    const images = sstvImages(value);
    const galleryKey = JSON.stringify([visible,signedIn,images]);
    if (galleryKey !== sstvGalleryKey) {
      const gallery = byId("sstv-gallery"); gallery.replaceChildren();
      for (const image of images) {
        const item = document.createElement("article"); item.className = "sstv-gallery-item"; item.setAttribute("role","listitem");
        const button = document.createElement("button"); button.type = "button"; button.className = "sstv-thumbnail";
        button.setAttribute("aria-label",`View ${typeof image.mode === "string" ? image.mode : "SSTV"} picture received ${localTime(image.created_at,true)}`);
        button.dataset.imageId = image.id;
        const thumbnail = document.createElement("img"); thumbnail.alt = "Received SSTV picture"; thumbnail.loading = "lazy"; thumbnail.decoding = "async";
        thumbnail.width = image.width; thumbnail.height = image.height;
        if (visible) thumbnail.setAttribute("src",sstvImageUrl(image.id));
        const caption = document.createElement("span"); caption.textContent = `${localTime(image.created_at,true)} · ${image.complete ? "Complete" : "Partial · " + image.lines_received + "/" + image.height + " lines"}`;
        button.append(thumbnail,caption); button.addEventListener("click",() => {
          if (!dataAdmin() || !dataPanelVisible() || !sstvImages(latestStatus?.sstv).some(item => item.id === image.id)) return;
          if (mediaSelectionModes.image) { setMediaSelected("image",image.id,!mediaSelections.image.has(image.id));return; }
          sstvSelectedId = image.id; renderSstv();
        });
        item.append(button);if (signedIn) item.append(mediaCheckbox("image",image));gallery.append(item);
      }
      sstvGalleryKey = galleryKey;
    }
    if (!images.some(image => image.id === sstvSelectedId)) sstvSelectedId = images[0]?.id || "";
    for (const row of byId("sstv-gallery").children) {
      const button = row.children[0], selected = button.dataset.imageId === sstvSelectedId;
      button.setAttribute("aria-pressed",String(selected));
      button.classList.toggle("selected",selected);
    }
    const selected = visible ? images.find(image => image.id === sstvSelectedId) : null;
    byId("sstv-selected").hidden = !selected;
    const selectedImage = byId("sstv-selected-image");
    if (selected) {
      const source = sstvImageUrl(selected.id);
      if (selectedImage.getAttribute("src") !== source) selectedImage.setAttribute("src",source);
      selectedImage.hidden = false;
      setText("sstv-selected-title",`${typeof selected.mode === "string" ? selected.mode : "Received picture"}${selected.complete ? "" : " · Partial"}`);
      setText("sstv-selected-detail",`${localTime(selected.created_at,true)} · ${selected.width} × ${selected.height} · ${selected.complete ? "Complete" : selected.lines_received + "/" + selected.height + " lines received"}${finite(selected.size_bytes) ? " · " + formatBytes(selected.size_bytes) : ""}`);
      byId("sstv-download").setAttribute("href",sstvImageUrl(selected.id,true));
    } else {
      clearSstvImage(selectedImage); byId("sstv-download").removeAttribute("href");
      setText("sstv-selected-title","Received picture"); setText("sstv-selected-detail","");
    }
    if (!signedIn) sstvSelectedId = "";
    byId("sstv-empty").hidden = images.length > 0;
    setText("sstv-gallery-summary",images.length ? `${images.length} saved picture${images.length === 1 ? "" : "s"} · newest first` : "No received pictures yet");
    const maximum = finite(value?.limits?.max_images) ? value.limits.max_images : 50;
    const mib = finite(value?.limits?.max_bytes) ? value.limits.max_bytes / 1048576 : 100;
    setText("sstv-limits-note",`The gallery keeps up to ${maximum.toLocaleString()} pictures or ${Number.isInteger(mib) ? mib : mib.toFixed(1)} MiB; the oldest pictures are removed when a limit is reached. Picture quality depends on the radio signal and audio level.`);
    renderMediaDeleteControls();
  }
  byId("sstv-current-image").addEventListener("error",() => {
    if (!dataAdmin() || !dataPanelVisible()) return;
    clearSstvImage(byId("sstv-current-image")); sstvPreviewKey = ""; byId("sstv-preview-empty").hidden = false;
    setText("sstv-preview-empty","Preview not available yet. Waiting for the next image update.");
  });
  byId("sstv-enabled").addEventListener("change",async() => {
    if (!canChangeSstv()) { renderData(); return; }
    const enabled = byId("sstv-enabled").checked, csrf = session.csrf_token;
    sstvBusy = true; mutationBusy = true; sstvError = ""; stopDataPolling(); renderAdmin();
    try {
      const result = await postJson("/api/sstv/decoding",{enabled});
      if (!result.sstv || typeof result.sstv.enabled !== "boolean") throw new Error("The Pi returned an unreadable SSTV status.");
      if (dataAdmin() && session.csrf_token === csrf) {
        dataRevision += 1; latestStatus = {...latestStatus,sstv:result.sstv};
      }
    } catch (error) { sstvError = error.message; }
    finally { sstvBusy = false; mutationBusy = false; renderAdmin(); }
  });

  function emitLiveAudio(state = liveState) {
    if (typeof window.dispatchEvent !== "function" || typeof CustomEvent !== "function") return;
    try {
      window.dispatchEvent(new CustomEvent("repeater-live-audio", {detail:{context:audioContext,output:audioGain,state,
        muted:Number(byId("listen-volume").value) === 0,until:nextAudioTime}}));
    } catch (_) { /* An optional visualizer must not interrupt audio playback. */ }
  }

  function clearAudioQueue() {
    for (const node of audioNodes) {
      try { node.stop(); node.disconnect(); } catch (_) { /* Already ended. */ }
    }
    audioNodes.clear();
  }

  function stopListening(clearError = false) {
    liveGeneration += 1;
    liveController?.abort();
    liveController = null;
    clearAudioQueue();
    emitLiveAudio("stopped");
    if (audioContext) audioContext.close().catch(() => {});
    audioContext = null;
    audioGain = null;
    nextAudioTime = 0;
    liveState = "stopped";
    byId("listening-strip").hidden = true;
    byId("tab-live-dot").hidden = true;
    document.querySelector(".live-player").classList.remove("listening");
    if (clearError) message("live-error", "");
    renderLive(latestStatus?.live_audio);
  }

  function scheduleAudio(bytes, sampleRate, generation) {
    if (!audioContext || generation !== liveGeneration) return;
    // Keep at most one second from a network burst. Stale traffic must not accumulate.
    const frameCount = bytes.byteLength / 4;
    let offset = Math.max(0, frameCount - sampleRate);
    if (nextAudioTime - audioContext.currentTime > 0.9) {
      clearAudioQueue();
      nextAudioTime = audioContext.currentTime + 0.08;
    }
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const framesPerBuffer = Math.max(1, Math.round(sampleRate / 10));
    while (offset < frameCount) {
      if (nextAudioTime - audioContext.currentTime > 1) {
        clearAudioQueue();
        nextAudioTime = audioContext.currentTime + 0.08;
      }
      const frames = Math.min(framesPerBuffer, frameCount - offset);
      const buffer = audioContext.createBuffer(1, frames, sampleRate);
      const channel = buffer.getChannelData(0);
      for (let index = 0; index < frames; index += 1) {
        const sample = view.getFloat32((offset + index) * 4, true);
        channel[index] = Number.isFinite(sample) ? Math.max(-1, Math.min(1, sample)) : 0;
      }
      const node = audioContext.createBufferSource();
      node.buffer = buffer;
      node.connect(audioGain);
      nextAudioTime = Math.max(nextAudioTime, audioContext.currentTime + 0.08);
      node.start(nextAudioTime);
      nextAudioTime += frames / sampleRate;
      audioNodes.add(node);
      node.onended = () => { audioNodes.delete(node); node.disconnect(); };
      offset += frames;
    }
    liveState = "playing";
    emitLiveAudio();
    setText("live-player-state", "Listening on this device");
    setText("live-player-description", "Radio audio is playing. Quiet periods are normal between transmissions.");
    byId("listening-strip").hidden = false;
    byId("tab-live-dot").hidden = false;
    document.querySelector(".live-player").classList.add("listening");
  }

  async function startListening() {
    const live = latestStatus?.live_audio;
    const url = liveUrl(live?.stream_url);
    if (liveState !== "stopped" || !url || live?.configured !== true || !audioSupported()) return;
    const generation = ++liveGeneration;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    message("live-error", "");
    liveState = "connecting";
    renderLive(live);
    setText("live-player-state", "Connecting to radio audio…");
    setText("live-player-description", "Stay on the box’s Wi-Fi. The stream will begin when radio audio arrives.");
    byId("recording-audio").pause();
    liveController = new AbortController();
    try {
      // Create/resume directly from the tap; no microphone permission or HTTPS-only worklet is required.
      audioContext = new AudioContextClass({ latencyHint: "interactive" });
      audioGain = audioContext.createGain();
      audioGain.gain.value = Number(byId("listen-volume").value) / 100;
      audioGain.connect(audioContext.destination);
      await audioContext.resume();
      if (generation !== liveGeneration) return;
      if (audioContext.state !== "running") throw new Error("Your browser paused audio. Tap Start listening again, and keep this page in the foreground.");
      emitLiveAudio();
      const response = await fetch(url, { cache: "no-store", credentials: "same-origin", signal: liveController.signal });
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).error; } catch (_) { /* Plain HTTP error. */ }
        throw new Error(typeof detail === "string" ? detail : `The radio stream is unavailable (${response.status}). Check the Pi audio setup.`);
      }
      if (!response.body || !response.body.getReader) throw new Error("This browser cannot stream live audio. Try another browser or use the audio archives.");
      const reader = response.body.getReader();
      let remainder = new Uint8Array(0);
      while (generation === liveGeneration) {
        const { value, done } = await reader.read();
        if (done) throw new Error("The live stream ended. Check the Pi connection and tap Start listening to reconnect.");
        if (!value?.byteLength) continue;
        const joined = new Uint8Array(remainder.length + value.length);
        joined.set(remainder); joined.set(value, remainder.length);
        const usable = joined.length - joined.length % 4;
        if (usable) scheduleAudio(joined.subarray(0, usable), live.sample_rate, generation);
        remainder = joined.slice(usable);
      }
    } catch (error) {
      if (generation !== liveGeneration) return;
      stopListening();
      if (error.name !== "AbortError") message("live-error", error instanceof TypeError
        ? "Cannot reach the live stream. Stay connected to the box’s Wi-Fi, then try again."
        : error.message || "Live audio is unavailable. Check the receiver setup on the Pi.", "error");
    }
  }

  for (const name of tabNames) {
    const tab = byId(`tab-${name}`);
    tab.addEventListener("click", () => selectTab(name));
    tab.addEventListener("keydown", (event) => {
      let next = tabNames.indexOf(name);
      if (event.key === "ArrowRight") next = (next + 1) % tabNames.length;
      else if (event.key === "ArrowLeft") next = (next + tabNames.length - 1) % tabNames.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabNames.length - 1;
      else return;
      event.preventDefault(); selectTab(tabNames[next], true);
    });
  }
  window.addEventListener("hashchange", () => selectTab(location.hash.slice(1)));
  byId("radio-tools").addEventListener("toggle",() => {
    if (!byId("radio-tools").open && radioToolsNeedAttention()) byId("radio-tools").open=true;
  });
  byId("recording-admin-link").addEventListener("click", () => selectTab("settings", true));
  byId("admin-login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (mutationBusy) return;
    mutationBusy = true; renderAdmin(); message("admin-message", "Signing in…");
    try {
      const data = await postJson("/api/admin/login", { password: byId("admin-password").value }, false);
      session = { configured: data.configured === true, authenticated: data.authenticated === true, csrf_token: typeof data.csrf_token === "string" ? data.csrf_token : null };
      sessionKnown = true;
      if (!session.authenticated) throw new Error("Sign-in was not accepted. Check the admin password.");
      byId("admin-password").value = "";
      message("admin-message", "Signed in on this browser.", "success");
      await refreshAfterMutation();
      await loadSession();
    } catch (error) { message("admin-message", error.message, "error"); }
    finally { mutationBusy = false; renderAdmin(); }
  });
  byId("logout-button").addEventListener("click", async () => {
    if (mutationBusy) return;
    mutationBusy = true; renderAdmin();
    try {
      await postJson("/api/admin/logout", {});
      session.authenticated = false; session.csrf_token = null;
      byId("admin-password").value = "";
      settingsDirty = false;
      closeWifiConfirmation();
      closeSoftwareConfirmation();
      closeRadioConfirmation();
      closeRadioPairConfirmation();
      wifiVisibilityError = "";
      wifiVisibilityAttempt = null;
      renderSettings(latestStatus?.settings);
      renderWifiVisibility(latestStatus?.wifi_visibility);
      message("admin-message", "Signed out. Listening and archives remain available.", "success");
      message("settings-message", "");
    } catch (error) { message("admin-message", error.message, "error"); }
    finally { mutationBusy = false; renderAdmin(); }
  });
  byId("settings-form").addEventListener("input", () => { settingsDirty = true; renderSettingsSwitches(); renderRecordingThreshold(); message("settings-message", "Unsaved changes. Select Save settings to apply."); });
  function closeWifiConfirmation() {
    wifiVisibilityProposal = null;
    const dialog = byId("wifi-confirm-dialog");
    if (dialog.open) dialog.close();
    renderWifiVisibility(latestStatus?.wifi_visibility);
    if (!byId("wifi-visibility-fields").disabled) byId("wifi-broadcast").focus();
  }

  byId("wifi-broadcast").addEventListener("click", () => {
    const wifi = latestStatus?.wifi_visibility;
    const preview = wifi?.preview === true;
    if (!sessionKnown || !session.authenticated || mutationBusy || softwareUpdateBusy || latestStatus?.software_updates?.pending === true || radioTaskPending() || dtmfCodePending() || messageTaskPending() || wifiVisibilityBusy || wifi?.pending === true || (!preview && (wifi?.available !== true || typeof wifi.profile_hidden !== "boolean"))) return;
    const hidden = wifi?.profile_hidden !== true;
    wifiVisibilityProposal = { hidden, priorHidden: wifi?.profile_hidden, priorRequestId: wifi?.request_id, ssid: wifi?.ssid, csrf: session.csrf_token };
    wifiVisibilityError = "";
    wifiVisibilityAttempt = null;
    const network = wifi?.ssid || "Repeater-Box";
    setText("wifi-confirm-title", hidden ? "Stop broadcasting the Wi-Fi name?" : "Broadcast the Wi-Fi name?");
    setText("wifi-confirm-description", hidden
      ? `“${network}” will disappear from nearby Wi-Fi lists. Wi-Fi may briefly disconnect. Rejoin manually using this name and your existing password, then open http://10.42.0.1.`
      : `“${network}” will appear in nearby Wi-Fi lists. Wi-Fi may briefly disconnect while this change is applied. The name and password will stay the same.`);
    setText("wifi-confirm-apply", hidden ? "Stop broadcasting" : "Broadcast name");
    byId("wifi-confirm-preview").hidden = !preview;
    renderAdmin();
    const dialog = byId("wifi-confirm-dialog");
    dialog.setAttribute("aria-describedby", preview ? "wifi-confirm-description wifi-confirm-preview" : "wifi-confirm-description");
    if (typeof dialog.showModal !== "function") {
      wifiVisibilityProposal = null;
      message("wifi-visibility-message", "This browser cannot show the confirmation dialog. Use a current browser to change visibility.", "error");
      return;
    }
    dialog.showModal();
    byId("wifi-confirm-cancel").focus();
  });

  byId("wifi-confirm-cancel").addEventListener("click", closeWifiConfirmation);
  byId("wifi-confirm-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeWifiConfirmation();
  });
  byId("wifi-confirm-dialog").addEventListener("close", () => {
    if (!byId("wifi-confirm-dialog").open) wifiVisibilityProposal = null;
  });
  byId("wifi-confirm-apply").addEventListener("click", async () => {
    const proposal = wifiVisibilityProposal;
    if (!proposal || mutationBusy || softwareUpdateBusy || latestStatus?.software_updates?.pending === true || radioTaskPending() || dtmfCodePending() || messageTaskPending() || wifiVisibilityBusy || latestStatus?.wifi_visibility?.preview === true) return;
    // Recheck the saved profile and session before accepting a possibly stale dialog.
    mutationBusy = true;
    renderAdmin();
    const previousUpdate = lastSuccess;
    await refreshAfterMutation();
    const wifi = latestStatus?.wifi_visibility;
    const unchanged = lastSuccess !== previousUpdate && wifi?.available === true && wifi?.preview !== true && wifi.pending !== true &&
      wifi.profile_hidden === proposal.priorHidden && wifi.request_id === proposal.priorRequestId &&
      wifi.ssid === proposal.ssid && session.authenticated && session.csrf_token === proposal.csrf && latestStatus?.software_updates?.pending !== true && !radioTaskPending() && !dtmfCodePending() && !messageTaskPending();
    if (!unchanged || !wifiVisibilityProposal || wifiVisibilityProposal !== proposal) {
      mutationBusy = false;
      closeWifiConfirmation();
      wifiVisibilityError = "The Wi-Fi state or admin session changed. Review its current visibility and try again.";
      renderWifiVisibility(wifi);
      return;
    }
    const hidden = proposal.hidden;
    closeWifiConfirmation();
    wifiVisibilityAttempt = { hidden, priorRequestId: wifi.request_id };
    wifiVisibilityBusy = true;
    mutationBusy = true;
    wifiVisibilityError = "";
    renderWifiVisibility(wifi);
    try {
      const result = await postJson("/api/wifi-visibility", { hidden });
      if (!result.wifi_visibility || typeof result.wifi_visibility !== "object") throw new Error("The Pi returned an unreadable Wi-Fi status. Reconnect and refresh to check the saved visibility.");
      latestStatus = { ...latestStatus, wifi_visibility: result.wifi_visibility };
    } catch (error) {
      wifiVisibilityError = `${error.message} If Wi-Fi disconnected, reconnect using the network name and refresh to check whether the change was applied.`;
    } finally {
      wifiVisibilityBusy = false;
      mutationBusy = false;
      renderWifiVisibility(latestStatus?.wifi_visibility);
      await refreshAfterMutation();
    }
  });

  function closeSoftwareConfirmation() {
    softwareUpdateProposal = null;
    const dialog = byId("software-confirm-dialog");
    if (dialog.open) dialog.close();
    renderSoftwareUpdateControls();
    if (!byId("software-install").disabled) byId("software-install").focus();
  }

  async function requestSoftwareTask(action, body) {
    const updates = latestStatus?.software_updates;
    softwareUpdateAttempt = { priorRequestId: updates?.request_id };
    const check = action === "check" ? { csrf: session.csrf_token, installed: softwareInstalledKey(updates), requestId: null, target: null } : null;
    softwareUpdateCheck = check;
    softwareUpdateBusy = true;
    softwareUpdateAction = action;
    mutationBusy = true;
    softwareUpdateError = "";
    renderAdmin();
    message("software-updates-message", action === "install" ? "Requesting the dashboard update. Keep the Pi powered and connected to Ethernet." : "Requesting an update check…");
    try {
      const result = await postJson(`/api/software-updates/${action}`, body);
      if (!result.software_updates || typeof result.software_updates !== "object" || Array.isArray(result.software_updates)) {
        throw new Error("The Pi returned an unreadable update status. Refresh to check whether the task started.");
      }
      if (action === "check") {
        const saved = result.software_updates;
        if (softwareUpdateCheck === check && session.csrf_token === check.csrf && session.authenticated &&
            softwareInstalledKey(saved) === check.installed && saved.action === "check" &&
            typeof saved.request_id === "string" && saved.request_id && saved.request_id !== updates?.request_id) {
          check.requestId = saved.request_id;
        } else softwareUpdateCheck = null;
      }
      softwareUpdateRevision += 1;
      latestStatus = { ...latestStatus, software_updates: result.software_updates };
    } catch (error) {
      softwareUpdateCheck = null;
      softwareUpdateError = `${error.message} If the dashboard restarted, wait for it to reconnect before trying again.`;
    } finally {
      softwareUpdateBusy = false;
      softwareUpdateAction = null;
      mutationBusy = false;
      renderSoftwareUpdates(latestStatus?.software_updates);
      await refreshAfterMutation();
    }
  }

  byId("station-id-enabled").addEventListener("click",() => {
    if (!canManageStationIdentification()) return;
    const enabled=!latestStatus.station_identification.enabled;
    stationIdProposal={enabled,state:stationIdentificationKey(latestStatus.station_identification),csrf:session.csrf_token,configured:session.configured};
    stationIdError="";
    setText("station-id-confirm-title",enabled ? "Enable automatic station identification?" : "Disable automatic station identification?");
    setText("station-id-confirm-description",enabled
      ? "Allow automatic station identification after new radio traffic. Spoken DTMF prompts keep their current setting."
      : "Stop future automatic station identification. An ID already playing may finish. You must provide required station identification manually. Spoken DTMF prompts are unaffected.");
    setText("station-id-confirm-apply",enabled ? "Enable automatic ID" : "Disable automatic ID");
    const dialog=byId("station-id-confirm-dialog");
    if (typeof dialog.showModal !== "function") {
      stationIdProposal=null;stationIdError="This browser cannot show the confirmation dialog. Use a current browser to change automatic identification.";renderStationIdentification();return;
    }
    dialog.showModal();renderStationIdentification();byId("station-id-confirm-cancel").focus();
  });
  byId("station-id-confirm-cancel").addEventListener("click",closeStationIdentificationConfirmation);
  byId("station-id-confirm-dialog").addEventListener("cancel",(event) => { event.preventDefault();closeStationIdentificationConfirmation(); });
  byId("station-id-confirm-dialog").addEventListener("close",() => { stationIdProposal=null;renderStationIdentification(); });
  byId("station-id-confirm-apply").addEventListener("click",async() => {
    const proposal=stationIdProposal;
    if (!proposal || !byId("station-id-confirm-dialog").open || !canManageStationIdentification()) return;
    mutationBusy=true;renderAdmin();
    const previousUpdate=lastSuccess;await refreshAfterMutation();
    const unchanged=lastSuccess !== previousUpdate && stationIdProposal === proposal && byId("station-id-confirm-dialog").open &&
      canManageStationIdentification(true) && session.csrf_token === proposal.csrf && session.configured === proposal.configured &&
      stationIdentificationKey(latestStatus?.station_identification) === proposal.state;
    if (!unchanged) {
      mutationBusy=false;closeStationIdentificationConfirmation();stationIdError="The identification state, maintenance task or admin session changed. Review it again.";renderAdmin();return;
    }
    stationIdProposal=null;byId("station-id-confirm-dialog").close();
    try {
      const result=await postJson("/api/station-identification",{enabled:proposal.enabled});
      if (!result.station_identification || result.station_identification.enabled !== proposal.enabled || typeof result.station_identification.available !== "boolean") throw new Error("The Pi returned an unreadable identification status. Refresh to check the saved setting.");
      latestStatus={...latestStatus,station_identification:result.station_identification};stationIdError="";
    } catch(error) { stationIdError=error.message; }
    finally { mutationBusy=false;renderAdmin();await refreshAfterMutation(); }
  });

  function reviewDtmfChange(field) {
    if (!canManageDtmf()) return;
    const value=field === "enabled" ? !latestStatus.dtmf_control.enabled : latestStatus.dtmf_control.status_announcements === false;
    dtmfProposal={field,value,state:dtmfKey(latestStatus.dtmf_control),csrf:session.csrf_token};
    dtmfError="";message("dtmf-confirm-error","");
    setText("dtmf-confirm-title",field === "enabled" ? value ? "Enable radio DTMF control?" : "Disable radio DTMF control?" : value ? "Enable spoken status after DTMF actions?" : "Disable spoken status after DTMF actions?");
    const controls = latestStatus.dtmf_control.data_controls?.available === true ? "spoken status, recording, automatic identification, APRS decoding, and SSTV decoding" : "spoken status, recording, and automatic identification";
    setText("dtmf-confirm-description",field === "enabled" ? value ? `Allow the private password to open the radio menu for ${controls}?` : `Stop accepting radio DTMF commands for ${controls}? Current recording and the saved startup preference will stay as they are.` : value ? "Add the current channel-pair number and recording state after each successful DTMF action?" : "Omit the additional channel-pair and recording status after DTMF actions? Menu prompts and basic confirmation will still play.");
    byId("dtmf-confirm-dialog").showModal();renderDtmf();byId("dtmf-confirm-cancel").focus();
  }
  byId("dtmf-enabled").addEventListener("click",() => reviewDtmfChange("enabled"));
  byId("dtmf-announcements").addEventListener("click",() => reviewDtmfChange("status_announcements"));
  byId("dtmf-confirm-cancel").addEventListener("click",closeDtmfConfirmation);
  byId("dtmf-confirm-dialog").addEventListener("cancel",(event) => { event.preventDefault();closeDtmfConfirmation(); });
  byId("dtmf-confirm-dialog").addEventListener("close",() => { dtmfProposal=null;renderDtmf(); });
  byId("dtmf-confirm-apply").addEventListener("click",async() => {
    const proposal=dtmfProposal;
    if (!proposal || !byId("dtmf-confirm-dialog").open || !canManageDtmf()) return;
    mutationBusy=true;renderAdmin();
    const previousUpdate=lastSuccess;await refreshAfterMutation();
    const unchanged=lastSuccess !== previousUpdate && dtmfProposal === proposal && byId("dtmf-confirm-dialog").open &&
      canManageDtmf(true) && session.csrf_token === proposal.csrf && dtmfKey(latestStatus?.dtmf_control) === proposal.state;
    if (!unchanged) {
      mutationBusy=false;closeDtmfConfirmation();dtmfError="The DTMF state, radio task or admin session changed. Review it again.";renderAdmin();return;
    }
    dtmfProposal=null;byId("dtmf-confirm-dialog").close();
    try {
      const result=await postJson("/api/dtmf-control",{[proposal.field]:proposal.value});
      if (!result.dtmf_control || typeof result.dtmf_control.enabled !== "boolean") throw new Error("The Pi returned an unreadable DTMF status.");
      latestStatus={...latestStatus,dtmf_control:result.dtmf_control};dtmfError="";
    } catch(error) { dtmfError=error.message; }
    finally { mutationBusy=false;renderAdmin();await refreshAfterMutation(); }
  });
  function filteredDtmfCode(value) {
    return value.toUpperCase().replace(/[^0-9ABCD]/g, "").slice(0,6);
  }
  byId("dtmf-new-code").addEventListener("input",(event) => {
    const field=event.target, value=field.value, cursor=field.selectionStart;
    field.value=filteredDtmfCode(value);
    if (typeof cursor === "number" && typeof field.setSelectionRange === "function") {
      const position=filteredDtmfCode(value.slice(0,cursor)).length;
      field.setSelectionRange(position,position);
    }
  });
  byId("dtmf-new-code").addEventListener("paste",(event) => {
    if (!event.clipboardData || typeof event.clipboardData.getData !== "function") return;
    event.preventDefault();
    const field=event.target, start=field.selectionStart ?? field.value.length, end=field.selectionEnd ?? start;
    const inserted=filteredDtmfCode(event.clipboardData.getData("text"));
    field.value=filteredDtmfCode(field.value.slice(0,start)+inserted+field.value.slice(end));
    const position=Math.min(start+inserted.length,field.value.length);
    if (typeof field.setSelectionRange === "function") field.setSelectionRange(position,position);
  });
  byId("dtmf-code-form").addEventListener("submit",async(event) => {
    event.preventDefault();
    if (!canChangeDtmfPassword()) return;
    const code=byId("dtmf-new-code").value;
    const password=byId("dtmf-admin-password").value, csrf=session.csrf_token;
    if (!/^[0-9ABCD]{6}$/.test(code) || !password || password.length > 256) {
      clearDtmfPasswords();dtmfCodeError="Enter a six-character DTMF password using 0–9, uppercase A–D, and your current admin password.";renderDtmf();return;
    }
    const state=dtmfKey(latestStatus.dtmf_control);
    clearDtmfPasswords();dtmfCodeError="";mutationBusy=true;renderAdmin();
    const previousUpdate=lastSuccess;await refreshAfterMutation();
    if (lastSuccess === previousUpdate || !canChangeDtmfPassword(true) || session.csrf_token !== csrf || dtmfKey(latestStatus?.dtmf_control) !== state) {
      mutationBusy=false;dtmfCodeError="The DTMF state, radio task or admin session changed. Enter the passwords again after reviewing it.";renderAdmin();return;
    }
    try {
      const result=await postJson("/api/dtmf-password",{code,admin_password:password});
      if (!result.code_change || result.code_change.pending !== true) throw new Error("The Pi did not confirm a pending password change. Check its status before retrying.");
      latestStatus={...latestStatus,dtmf_control:{...latestStatus.dtmf_control,code_change:result.code_change}};
    } catch(error) { dtmfCodeError=error.message; }
    finally { clearDtmfPasswords();mutationBusy=false;renderAdmin();await refreshAfterMutation(); }
  });

  byId("software-check").addEventListener("click", async () => {
    if (!canManageSoftware()) return;
    await requestSoftwareTask("check", {});
  });
  byId("software-install").addEventListener("click", () => {
    const updates = latestStatus?.software_updates;
    if (!canManageSoftware(updates) || !freshSoftwareCheck(updates) || !softwareUpdateOffered(updates)) return;
    softwareUpdateProposal = { commit: updates.latest_commit, priorInstalled: softwareInstalledKey(updates), priorRequestId: updates.request_id, csrf: session.csrf_token, check: softwareUpdateCheck };
    softwareUpdateError = "";
    setText("software-confirm-description", `Install ${softwareVersion(updates.latest_version, updates.latest_commit)} from the project’s GitHub repository?${settingsDirty ? " Unsaved station-setting edits will not be applied." : ""}`);
    renderSoftwareUpdateControls();
    const dialog = byId("software-confirm-dialog");
    if (typeof dialog.showModal !== "function") {
      softwareUpdateProposal = null;
      message("software-updates-message", "This browser cannot show the confirmation dialog. Use a current browser to install updates.", "error");
      return;
    }
    dialog.showModal();
    byId("software-confirm-cancel").focus();
  });
  byId("software-confirm-cancel").addEventListener("click", closeSoftwareConfirmation);
  byId("software-confirm-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeSoftwareConfirmation();
  });
  byId("software-confirm-dialog").addEventListener("close", () => {
    if (!byId("software-confirm-dialog").open) softwareUpdateProposal = null;
  });
  byId("software-confirm-apply").addEventListener("click", async () => {
    const proposal = softwareUpdateProposal;
    if (!proposal || !canManageSoftware() || !freshSoftwareCheck()) return;
    mutationBusy = true;
    renderAdmin();
    const previousUpdate = lastSuccess;
    await refreshAfterMutation();
    const updates = latestStatus?.software_updates;
    const unchanged = lastSuccess !== previousUpdate && updates?.available === true && updates.preview !== true && updates.pending !== true &&
      softwareUpdateOffered(updates) && updates.latest_commit === proposal.commit &&
      softwareInstalledKey(updates) === proposal.priorInstalled && updates.request_id === proposal.priorRequestId &&
      freshSoftwareCheck(updates) && softwareUpdateCheck === proposal.check &&
      session.authenticated && session.csrf_token === proposal.csrf && !wifiVisibilityBusy && latestStatus?.wifi_visibility?.pending !== true && !radioTaskPending() && !dtmfCodePending() && !messageTaskPending();
    if (!unchanged || softwareUpdateProposal !== proposal) {
      mutationBusy = false;
      closeSoftwareConfirmation();
      softwareUpdateError = "The available update or admin session changed. Review the current version and try again.";
      renderSoftwareUpdates(updates);
      return;
    }
    mutationBusy = false;
    closeSoftwareConfirmation();
    await requestSoftwareTask("install", { commit: proposal.commit });
  });
  function closeRadioPairConfirmation() {
    radioPairProposal=null;
    byId("radio-pair-confirm-password").value="";
    const dialog=byId("radio-pair-confirm-dialog");
    if (dialog.open) dialog.close();
    renderRadioPairControls();
    if (!byId("radio-pair-change").disabled) byId("radio-pair-change").focus();
  }

  function showRadioPairConfirmation() {
    const state=latestStatus?.radio_programming;
    if (!canChangeRadioPair(state)) return;
    const preset=pairPresets(state).find((item) => item.id === byId("radio-pair-select").value);
    if (!preset) return;
    radioPairProposal={pair_id:preset.id,preset:pairPresetKey(preset),priorRequestId:state.request_id,pair:radioPairKey(state),
      catalogDigest:state.catalog_digest,rxIdentity:radioIdentityKey(state,"rx"),txIdentity:radioIdentityKey(state,"tx"),plan:radioPlanKey(state.plan),csrf:session.csrf_token};
    radioPairError="";
    byId("radio-pair-confirm-password").value="";
    message("radio-pair-confirm-error","");
    setText("radio-pair-confirm-title",`Configure ${preset.label}?`);
    const pair=state.pair;
    const stored=pair?.stored_verified === true && pairRoleVerified(pair,"rx") && pairRoleVerified(pair,"tx");
    setText("radio-pair-confirm-old",stored ? pairPlanText(pair) : "No complete verified pair record; per-radio settings may differ.");
    setText("radio-pair-confirm-new",`${preset.label} · ${preset.name} · RX input ${pairFrequency(preset.input_hz)} → TX output ${pairFrequency(preset.output_hz)} · NFM · High power · Tone ${preset.tone_hz === null ? "Off" : preset.tone_hz + " Hz"}`);
    const dialog=byId("radio-pair-confirm-dialog");
    if (typeof dialog.showModal !== "function") {
      radioPairProposal=null;
      radioPairError="This browser cannot show the confirmation dialog. Use a current browser to change the pair.";
      renderRadioPair(state); return;
    }
    dialog.showModal(); renderRadioPairControls(); byId("radio-pair-confirm-password").focus();
  }

  async function applyRadioPair() {
    const proposal=radioPairProposal;
    const password=byId("radio-pair-confirm-password").value;
    if (!proposal || !byId("radio-pair-confirm-dialog").open || !canChangeRadioPair() || !password || password.length > 1024) return;
    byId("radio-pair-confirm-password").value="";
    mutationBusy=true; renderAdmin();
    const previousUpdate=lastSuccess;
    await refreshAfterMutation();
    mutationBusy=false;
    const state=latestStatus?.radio_programming;
    const preset=pairPresets(state).find((item) => item.id === proposal.pair_id);
    const unchanged=lastSuccess !== previousUpdate && radioPairProposal === proposal && byId("radio-pair-confirm-dialog").open &&
      session.authenticated && session.csrf_token === proposal.csrf && canChangeRadioPair(state) &&
      state.catalog_digest === proposal.catalogDigest && state.request_id === proposal.priorRequestId && radioPairKey(state) === proposal.pair &&
      radioIdentityKey(state,"rx") === proposal.rxIdentity && radioIdentityKey(state,"tx") === proposal.txIdentity && radioPlanKey(state.plan) === proposal.plan &&
      pairPresetKey(preset) === proposal.preset && byId("radio-pair-select").value === proposal.pair_id;
    if (!unchanged) {
      closeRadioPairConfirmation();
      radioPairError="The channel pair, task status, connection, or admin session changed. Review the current information and enter the password again.";
      renderRadioPair(state); renderAdmin(); return;
    }
    closeRadioPairConfirmation();
    radioPairAttempt={priorRequestId:state.request_id};
    radioProgrammingAttempt={priorRequestId:state.request_id};
    radioProgrammingBusy=true;
    radioPairError=""; radioProgrammingError=""; renderAdmin();
    message("radio-pair-message",`Requesting ${preset.label}. The Pi will back up, program and verify both radios…`);
    try {
      const result=await postJson("/api/radio-pair",{pair_id:proposal.pair_id,password,catalog_digest:proposal.catalogDigest});
      if (!result.radio_programming || typeof result.radio_programming !== "object" || Array.isArray(result.radio_programming)) {
        throw new Error("The Pi returned an unreadable paired radio status.");
      }
      latestStatus={...latestStatus,radio_programming:result.radio_programming};
    } catch (error) {
      radioPairError=`${error.message} Check the task status before repeating the request. A new attempt requires the admin password again.`;
    } finally {
      byId("radio-pair-confirm-password").value="";
      radioProgrammingBusy=false;
      renderRadioProgramming(latestStatus?.radio_programming);
      await refreshAfterMutation();
    }
  }

  byId("radio-pair-change").addEventListener("click",showRadioPairConfirmation);
  byId("radio-pair-select").addEventListener("change",renderRadioPairControls);
  byId("radio-pair-confirm-password").addEventListener("input",renderRadioPairControls);
  byId("radio-pair-confirm-apply").addEventListener("click",applyRadioPair);
  byId("radio-pair-confirm-cancel").addEventListener("click",closeRadioPairConfirmation);
  byId("radio-pair-confirm-dialog").addEventListener("cancel",(event) => { event.preventDefault(); closeRadioPairConfirmation(); });
  byId("radio-pair-confirm-dialog").addEventListener("close",() => {
    radioPairProposal=null; byId("radio-pair-confirm-password").value=""; renderRadioPairControls();
  });

  function closeRadioConfirmation() {
    const proposal = radioProgrammingProposal;
    radioProgrammingProposal = null;
    const dialog = byId("radio-confirm-dialog");
    if (dialog.open) dialog.close();
    renderRadioProgrammingControls();
    const trigger=proposal && byId(`radio-${proposal.action === "repair-upload" ? "repair" : proposal.action}-${proposal.role}`);
    if (trigger && !trigger.disabled) trigger.focus();
  }

  function radioIdentityKey(state, role) {
    const radio = state?.roles?.[role];
    return JSON.stringify({ inspected: radio?.inspected, compatible: radio?.compatible, firmware: radio?.firmware, identity: radio?.identity,
      verification_required: radio?.verification_required, programmed: radio?.programmed, verified: radio?.verified,
      program_kind:radio?.program_kind,pending_id:radio?.pending_id });
  }

  function showRadioConfirmation(role, action) {
    const state = latestStatus?.radio_programming;
    if (!canRequestRadio(role, action, state)) return;
    radioProgrammingProposal = { role, action, priorRequestId: state.request_id, plan: radioPlanKey(state.plan),
      catalogDigest:state.catalog_digest,identity: radioIdentityKey(state, role), pendingId:action === "repair-upload" ? state.roles[role].pending_id : null, csrf: session.csrf_token };
    radioProgrammingError = "";
    const label = role.toUpperCase();
    const names = { inspect: "Inspect", program: "Program", verify: "Verify", "repair-upload":"Repair saved upload" };
    setText("radio-confirm-title", action === "repair-upload" ? `Repair the saved ${label} upload?` : `${names[action]} the ${label} radio?`);
    setText("radio-confirm-apply", action === "repair-upload" ? `Repair saved ${label} upload` : `${names[action]} ${label}`);
    setText("radio-confirm-description", action === "repair-upload"
      ? `Restore the private saved candidate for the unfinished ${label} upload using the configured UV-5RX / 5RX profile (firmware ${textOrAbsent(state.roles?.[role]?.firmware)}). This includes the neighboring bytes in affected memory pages that may have been cleared. It restores that reviewed upload; it does not apply a new default plan.`
      : action === "program"
      ? `Use the configured UV-5RX / 5RX profile (firmware ${textOrAbsent(state.roles?.[role]?.firmware)}) to set ${state.plan[role].name} at ${Number(state.plan[role].frequency_mhz).toFixed(3)} MHz. ${radioPlanDescription(role, state.plan)}.`
      : action === "inspect" ? `Read and back up the ${label} radio using the configured UV-5RX / 5RX profile, then show its firmware information for review.`
      : `Read the ${label} radio using the configured UV-5RX / 5RX profile and compare it with the saved upload. Keep the radio connected and powered.`);
    setText("radio-confirm-impact", action === "repair-upload"
      ? "Confirm the same physical radio is attached to its assigned AIOC. The helper checks the profile, makes a fresh backup, and writes only affected pages from the saved candidate. Existing backups are retained within the five-set limit. Keep the radio and Pi powered. Radio audio stays paused until the repair and every unfinished upload verify successfully. If the radio does not reconnect, restart it before retrying."
      : action === "program"
      ? "Receiver audio will pause. The helper makes a fresh backup of this radio before writing memory 1, High power and Auto Lock on. Other memories and calibration are preserved. Select memory 1 on the radio after programming. The helper waits and reads back the result automatically. Keep the radio connected and powered. Audio stays paused until verification succeeds. If verification fails, review the result and use Verify; restart the radio only if it does not reconnect."
      : "Receiver audio will pause while the radio is read. This action does not write radio settings.");
    renderRadioProgrammingControls();
    const dialog = byId("radio-confirm-dialog");
    if (typeof dialog.showModal !== "function") {
      radioProgrammingProposal = null;
      message("radio-programming-message", "This browser cannot show the confirmation dialog. Use a current browser to manage radio programming.", "error");
      return;
    }
    dialog.showModal();
    byId("radio-confirm-cancel").focus();
  }

  for (const role of ["rx", "tx"]) {
    for (const action of ["inspect", "program", "verify"]) byId(`radio-${action}-${role}`).addEventListener("click", () => showRadioConfirmation(role, action));
    byId(`radio-repair-${role}`).addEventListener("click",() => showRadioConfirmation(role,"repair-upload"));
  }
  byId("radio-confirm-cancel").addEventListener("click", closeRadioConfirmation);
  byId("radio-confirm-dialog").addEventListener("cancel", (event) => {
    event.preventDefault(); closeRadioConfirmation();
  });
  byId("radio-confirm-dialog").addEventListener("close", () => {
    if (!byId("radio-confirm-dialog").open) radioProgrammingProposal = null;
  });
  byId("radio-confirm-apply").addEventListener("click", async () => {
    const proposal = radioProgrammingProposal;
    if (!proposal || !canRequestRadio(proposal.role, proposal.action)) return;
    mutationBusy = true;
    renderAdmin();
    const previousUpdate = lastSuccess;
    await refreshAfterMutation();
    const state = latestStatus?.radio_programming;
    const unchanged = lastSuccess !== previousUpdate && state?.available === true && state.configured === true &&
      state.preview !== true && state.pending !== true && state.request_id === proposal.priorRequestId &&
      (proposal.action !== "program" || state.catalog_digest === proposal.catalogDigest) && radioPlanKey(state.plan) === proposal.plan && radioIdentityKey(state, proposal.role) === proposal.identity &&
      radioActionAllowed(proposal.role, proposal.action, state) &&
      session.authenticated && session.csrf_token === proposal.csrf && latestStatus?.software_updates?.pending !== true &&
      latestStatus?.wifi_visibility?.pending !== true && !wifiVisibilityBusy && !softwareUpdateBusy;
    if (!unchanged || radioProgrammingProposal !== proposal) {
      mutationBusy = false;
      closeRadioConfirmation();
      radioProgrammingError = "The radio plan, task status, or admin session changed. Review the current information and try again.";
      renderRadioProgramming(state);
      return;
    }
    mutationBusy = false;
    closeRadioConfirmation();
    radioProgrammingAttempt = { priorRequestId: state.request_id };
    radioProgrammingBusy = true;
    radioProgrammingError = "";
    renderAdmin();
    message("radio-programming-message", `Requesting ${proposal.action} for the ${proposal.role.toUpperCase()} radio…`);
    try {
      const payload={role:proposal.role,action:proposal.action};
      if (proposal.action === "repair-upload") payload.pending_id=proposal.pendingId;
      if (proposal.action === "program") payload.catalog_digest=proposal.catalogDigest;
      const result = await postJson("/api/radio-programming", payload);
      if (!result.radio_programming || typeof result.radio_programming !== "object" || Array.isArray(result.radio_programming)) {
        throw new Error("The Pi returned an unreadable radio status.");
      }
      latestStatus = { ...latestStatus, radio_programming: result.radio_programming };
    } catch (error) {
      radioProgrammingError = `${error.message} Refresh the task status before repeating a programming request. Keep the radio connected and powered.`;
    } finally {
      radioProgrammingBusy = false;
      renderRadioProgramming(latestStatus?.radio_programming);
      await refreshAfterMutation();
    }
  });

  byId("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (mutationBusy || softwareUpdateBusy || latestStatus?.software_updates?.pending === true || radioTaskPending() || dtmfCodePending() || messageTaskPending()) return;
    const values = {
      station_name: byId("station-name").value.trim(),
      input_enabled: byId("input-enabled").checked,
      monitor_transmit_audio: byId("monitor-transmit").checked,
      record_on_startup: byId("record-startup").checked,
      keep_indefinitely: byId("keep-indefinitely").checked,
      retention_days: Number(byId("retention-days").value),
      max_archive_mb: Number(byId("archive-limit").value),
      recording_threshold_dbfs: Number(byId("recording-threshold").value)
    };
    mutationBusy = true; renderAdmin(); message("settings-message", "Saving settings…");
    try {
      await postJson("/api/settings", values);
      settingsDirty = false;
      await refreshAfterMutation();
      message("settings-message", "Settings saved on the Pi.", "success");
    } catch (error) { message("settings-message", error.message, "error"); }
    finally { mutationBusy = false; renderAdmin(); }
  });
  byId("recording-toggle").addEventListener("change", async () => {
    if (mutationBusy || softwareUpdateBusy || latestStatus?.software_updates?.pending === true || radioTaskPending() || dtmfCodePending() || messageTaskPending()) return;
    const enabled = byId("recording-toggle").checked;
    mutationBusy = true; renderAdmin(); message("recording-action-message", "Updating recording…");
    try {
      const result = await postJson("/api/recording", { enabled });
      if (result.recorder) renderRecorder(result.recorder);
      message("recording-action-message", enabled ? "Recording enabled. It will save audio when the selected radio feed is active." : "Recording turned off. Saved archives are kept.", "success");
    } catch (error) { message("recording-action-message", error.message, "error"); }
    finally {
      mutationBusy = false; await refreshAfterMutation(); renderAdmin();
      byId("recording-toggle").checked = latestStatus?.recorder?.enabled === true;
    }
  });
  byId("listen-button").addEventListener("click", startListening);
  byId("stop-button").addEventListener("click", () => stopListening(true));
  byId("strip-stop").addEventListener("click", () => stopListening(true));
  byId("listen-volume").addEventListener("input", (event) => {
    const volume = Number(event.target.value);
    setText("volume-value", `${volume}%`);
    if (audioGain && audioContext) audioGain.gain.setTargetAtTime(volume / 100, audioContext.currentTime, 0.02);
    emitLiveAudio();
  });
  byId("recording-audio").addEventListener("play", () => { if (liveState !== "stopped") stopListening(true); });
  window.addEventListener("pagehide", () => { closeMediaDeleteConfirmation(false); dataPageActive = false; stopDataPolling(); renderSstv(); softwareCheckPageActive = false; stopSoftwareCheckPolling(); stopListening(); stopRadioActivity(); });
  window.addEventListener("pageshow", startRadioActivity);
  window.addEventListener("pageshow", () => { softwareCheckPageActive = true; renderSoftwareCheckProgress(); });
  window.addEventListener("pageshow", () => { dataPageActive = true; renderData(); });
  selectTab(location.hash.slice(1));
  loadSession();
  byId("refresh-button").addEventListener("click", () => { refresh(); loadSession(); });
  byId("recording-select").addEventListener("change", (event) => selectRecording(event.target.value));
  window.addEventListener("online", refresh);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopRadioActivity();
    else { refresh(); startRadioActivity(); }
    renderSoftwareCheckProgress();
    renderData();
  });
  refresh();
  startRadioActivity();
  setInterval(() => { if (!document.hidden) refresh(); }, 10000);
  setInterval(refreshRadioProgress, 1000);
  setInterval(refreshRadioMessages, 2000);
})();

"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const stable = (value) => JSON.stringify(value, (_, item) => item && typeof item === "object" && !Array.isArray(item)
    ? Object.keys(item).sort().reduce((result, key) => { result[key] = item[key]; return result; }, {}) : item);
  const same = (a, b) => stable(a) === stable(b);
  const validId = (value) => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
  const roles = ["rx", "tx", "programmer"];
  const models = ["5RX", "UV5R"];
  let session = { configured: false, authenticated: false, csrf_token: null };
  let status = null;
  let snapshot = null;
  let draft = null;
  let role = "rx";
  let externalModel = "5RX";
  let externalReadRequired = false;
  let busy = false;
  let refreshing = false;
  let connected = false;
  let proposal = null;
  let repairProposal = null;
  let discardContext = null;
  let loadedReadId = null;
  let errorMessage = "";
  let readAttempt = null;
  let configuredRolesKey = "";
  let supportedModelsKey = "";
  const deletedMemories = new Map();
  let addContext = null;
  let deleteProposal = null;
  let presetContext = null;
  function configuredPairs() {
    const radio = status?.radio_programming;
    if (radio?.catalog_configured !== true || !/^[0-9a-f]{64}$/.test(radio.catalog_digest || "")) return [];
    const pairs = Array.isArray(radio.pair_switching?.presets) ? radio.pair_switching.presets : [];
    const seen = new Set();
    return pairs.filter((pair) => {
      if (!pair || typeof pair.id !== "string" || !/^[a-z][a-z0-9_-]{0,15}$/.test(pair.id) || seen.has(pair.id) ||
          !Number.isInteger(pair.index) || pair.index < 1 || pair.index > 9 ||
          pair.rx_name !== `BOX-RX${pair.index}` || pair.tx_name !== `BOX-TX${pair.index}` ||
          typeof pair.label !== "string" || !pair.label.trim() || pair.label.length > 80 ||
          !Number.isInteger(pair.input_hz) || pair.input_hz < 144000000 || pair.input_hz >= 148000000 || pair.input_hz % 10 ||
          !Number.isInteger(pair.output_hz) || pair.output_hz < 420000000 || pair.output_hz >= 450000000 || pair.output_hz % 10 ||
          !(pair.tone_hz === null || typeof pair.tone_hz === "number" && Number.isFinite(pair.tone_hz) && pair.tone_hz >= 60 && pair.tone_hz <= 260)) return false;
      seen.add(pair.id); return true;
    }).slice(0,9);
  }

  function formatMHz(value) {
    return Number.isInteger(value) && value > 0 ? (value / 1000000).toFixed(6) : "";
  }

  function parseMHz(value) {
    if (typeof value !== "string" || !/^\d{1,3}(?:\.\d{1,6})?$/.test(value.trim())) return null;
    const hz = Math.round(Number(value.trim()) * 1000000);
    return Number.isSafeInteger(hz) && hz > 0 ? hz : null;
  }

  function inBands(value, bands) {
    return Number.isInteger(value) && value % 10 === 0 && Array.isArray(bands) && bands.some((band) => Array.isArray(band) && value >= band[0] && value <= band[1]);
  }

  function changesFor(base, candidate) {
    if (!base || !candidate) return { changes: [], settings: {} };
    const originals = new Map(base.memories.map((memory) => [memory.number, memory]));
    return { changes: candidate.memories.filter((memory) => !same(memory, originals.get(memory.number))).map(clone),
      settings: Object.fromEntries(Object.entries(candidate.settings).filter(([key, value]) => !same(value, base.settings[key]))) };
  }

  function changedCount() {
    const changes = changesFor(snapshot, draft);
    return changes.changes.length + Object.keys(changes.settings).length;
  }

  function memoryChangeCounts() {
    const counts={added:0,deleted:0,updated:0};
    if (!snapshot || !draft) return counts;
    for (const memory of changesFor(snapshot,draft).changes) {
      const original=snapshot.memories.find((item) => item.number === memory.number);
      counts[memory.empty ? "deleted" : original?.empty ? "added" : "updated"]++;
    }
    return counts;
  }

  function toneText(tone) {
    return !tone ? "Off" : tone.type === "CTCSS" ? `${tone.value} Hz` : `DCS ${String(tone.value).padStart(3, "0")}${tone.polarity}`;
  }

  function summary(memory, labelled = false) {
    if (!memory || memory.empty) return "Empty";
    const transmit = memory.tx_hz === null ? "TX disabled" : `TX ${formatMHz(memory.tx_hz)} MHz`;
    return `${labelled ? "Name: " : ""}${memory.name || "Unnamed"} · RX ${formatMHz(memory.rx_hz)} MHz · ${transmit} · TX tone ${toneText(memory.tx_tone)} · RX tone ${toneText(memory.rx_tone)} · ${memory.power} · ${memory.mode} · ${labelled ? `Scan: ${memory.skip ? "Skip" : "Include"}` : memory.skip ? "Skip scan" : "Include in scan"}`;
  }

  function validationErrors(base, candidate) {
    if (!base || !candidate) return ["Read a radio before editing."];
    const caps = base.capabilities || {};
    const originals = new Map(base.memories.map((item) => [item.number, item]));
    const changes = changesFor(base, candidate);
    const errors = [];
    for (const memory of changes.changes) {
      if (memory.empty) continue;
      const prefix = `Memory ${memory.number}: `;
      if (!inBands(memory.rx_hz, caps.bands_hz)) errors.push(`${prefix}enter a supported receive frequency in 10 Hz increments (five decimal places in MHz).`);
      const original = originals.get(memory.number);
      if (memory.tx_hz !== null && memory.tx_hz !== original?.tx_hz && !inBands(memory.tx_hz, caps.tx_bands_hz)) errors.push(`${prefix}a new transmit frequency must be within the configured amateur bands.`);
      if (typeof memory.name !== "string" || memory.name.length > caps.name_length || [...memory.name].some((char) => !caps.valid_characters?.includes(char))) errors.push(`${prefix}use up to ${caps.name_length || 7} supported name characters.`);
      if (!["High", "Low"].includes(memory.power) || !["FM", "NFM"].includes(memory.mode)) errors.push(`${prefix}select a supported power and mode.`);
      for (const key of ["tx_tone", "rx_tone"]) {
        const tone = memory[key];
        if (tone && (tone.type === "CTCSS" ? !caps.ctcss?.includes(tone.value) : tone.type !== "DCS" || !caps.dcs?.includes(tone.value) || !["N", "R"].includes(tone.polarity))) errors.push(`${prefix}select a supported ${key === "tx_tone" ? "transmit" : "receive"} tone.`);
      }
    }
    for (const [key, value] of Object.entries(changes.settings)) {
      const descriptor = caps.settings?.[key];
      if (!descriptor || (descriptor.type === "boolean" ? typeof value !== "boolean"
        : descriptor.type === "integer" ? !Number.isInteger(value) || value < descriptor.min || value > descriptor.max
          : descriptor.type !== "select" || !descriptor.options?.includes(value))) errors.push(`Check ${descriptor?.label || key}.`);
    }
    return errors;
  }

  function taskPending() {
    return status?.radio_programming?.pending === true || status?.software_updates?.pending === true || status?.wifi_visibility?.pending === true;
  }

  function verificationRoles() {
    const radio = status?.radio_programming;
    const unfinished = roles.filter((item) => radio?.roles?.[item]?.verification_required === true);
    if (radio?.verification_required === true && roles.includes(radio.role) && !unfinished.includes(radio.role)) unfinished.push(radio.role);
    return unfinished;
  }

  function configuredRoles() {
    const selected = status?.radio_programming?.configured_roles;
    return Array.isArray(selected) ? roles.filter((item) => selected.includes(item)) : ["rx", "tx"];
  }

  function roleName(selected = role) { return selected === "programmer" ? `external programmer radio (${modelName()})` : `${selected.toUpperCase()} radio`; }

  function supportedModels(selectedRole = role) {
    if (selectedRole !== "programmer") return ["5RX"];
    const supported = status?.radio_programming?.supported_models?.programmer;
    return Array.isArray(supported) ? models.filter((model) => supported.includes(model)) : ["5RX"];
  }

  function selectedModel() { return role === "programmer" ? externalModel : "5RX"; }
  function snapshotModel(value) { return value?.model === undefined ? "5RX" : value.model; }
  function modelName(model = selectedModel()) { return model === "UV5R" ? "UV-5R / UV5R" : "UV-5RX / 5RX"; }
  function pendingModel() {
    const item = status?.radio_programming?.roles?.programmer;
    return item?.verification_required === true ? snapshotModel(item) : null;
  }

  function renderModels() {
    const supported = supportedModels("programmer");
    const key = supported.join(",");
    const select = byId("radio-model");
    if (key !== supportedModelsKey) {
      supportedModelsKey = key; select.replaceChildren();
      for (const model of supported) { const option = create("option", modelName(model)); option.value = model; select.append(option); }
    }
    select.value = externalModel;
    byId("radio-model-label").hidden = role !== "programmer";
    byId("radio-model-help").hidden = role !== "programmer";
    select.disabled = role !== "programmer" || busy || taskPending() || pendingModel() !== null || supported.length < 2;
  }

  function recoveryId(value = snapshot) {
    return value?.recovery?.required === true && validId(value.recovery.pending_id) ? value.recovery.pending_id : null;
  }

  function canReview() {
    if (!canEdit() || (!changedCount() && !recoveryId()) || snapshot.recovery?.protected_mismatch === true) return false;
    const required = verificationRoles().includes(role);
    return required ? !!recoveryId() : snapshot.recovery?.required !== true;
  }

  function authenticated() { return session.authenticated === true && typeof session.csrf_token === "string" && !!session.csrf_token; }
  function available(ignoreBusy = false) { return authenticated() && connected && status?.radio_programming?.available === true && status?.radio_programming?.configured === true && status?.radio_programming?.preview !== true && (ignoreBusy || !busy) && !taskPending(); }
  function canEdit() { return available() && configuredRoles().includes(role) && snapshot?.role === role && snapshotModel(snapshot) === selectedModel() && supportedModels().includes(selectedModel()) && snapshot?.compatible === true && validId(snapshot.base_id); }

  function repairableUpload() {
    const state=status?.radio_programming;
    const radio=state?.roles?.[role];
    return configuredRoles().includes(role) && radio?.verification_required === true &&
      ["editor","repeater"].includes(radio.program_kind) && validId(radio.pending_id) &&
      state?.pair?.verification_required !== true && !roles.some((item) =>
        state?.roles?.[item]?.verification_required === true && state.roles[item].program_kind === "pair");
  }

  function canRepairSavedUpload(ignoreBusy = false) { return available(ignoreBusy) && repairableUpload(); }

  function repairIdentityKey() {
    const radio=status?.radio_programming?.roles?.[role];
    return stable({inspected:radio?.inspected,compatible:radio?.compatible,firmware:radio?.firmware,identity:radio?.identity,
      verification_required:radio?.verification_required,programmed:radio?.programmed,verified:radio?.verified,
      program_kind:radio?.program_kind,pending_id:radio?.pending_id,model:snapshotModel(radio)});
  }

  function message(id, text, kind = "") {
    byId(id).textContent = text;
    byId(id).className = `form-message ${kind}`;
  }

  function pill(id, text, kind = "muted") {
    byId(id).textContent = text;
    byId(id).className = `pill pill-${kind}`;
  }

  function setSession(data) {
    const wasAuthenticated = session.authenticated;
    session = { configured: data.configured === true, authenticated: data.authenticated === true,
      csrf_token: typeof data.csrf_token === "string" ? data.csrf_token : null };
    if (!authenticated() && wasAuthenticated) {
      clearSnapshot();
      closeDialogs();
    }
    renderAccess();
  }

  function renderAccess() {
    const signedIn = authenticated();
    byId("login-form").hidden = signedIn || !session.configured;
    byId("logout-button").hidden = !signedIn;
    byId("editor-workspace").hidden = !signedIn;
    byId("login-button").disabled = busy;
    byId("logout-button").disabled = busy;
    pill("admin-state", signedIn ? "Signed in" : session.configured ? "Signed out" : "Not configured", signedIn ? "good" : "amber");
    byId("admin-description").textContent = signedIn ? "Signed in with your dashboard admin account. Drafts stay in this browser until you confirm a write."
      : session.configured ? "Sign in with the dashboard admin password to read or edit radio configuration."
        : "Set the dashboard admin password on the Pi before using the radio editor.";
    renderControls();
  }

  function renderControls() {
    const dirty = changedCount() > 0;
    const unfinished = verificationRoles();
    renderModels();
    byId("radio-role").disabled = busy || taskPending();
    byId("read-radio").disabled = !available();
    byId("verify-radio").hidden = !unfinished.includes(role);
    byId("verify-radio").disabled = !available() || !unfinished.includes(role);
    byId("repair-radio").hidden = !repairableUpload();
    byId("repair-radio").disabled = !canRepairSavedUpload();
    byId("repair-confirm").disabled = !canRepairSavedUpload() || !repairProposal || repairProposal.csrf !== session.csrf_token ||
      repairProposal.role !== role || repairProposal.model !== selectedModel() || repairProposal.identity !== repairIdentityKey();
    byId("editor-fields").disabled = !canEdit();
    byId("discard-draft").disabled = !canEdit() || !dirty;
    byId("review-changes").disabled = !canReview();
    byId("review-changes").textContent = recoveryId() ? dirty ? "Review repair" : "Review current values" : "Review changes";
    byId("read-confirm").disabled = !available();
    byId("verify-confirm").disabled = !available() || !unfinished.includes(role);
    byId("write-confirm").disabled = !canReview() || !proposal || validationErrors(snapshot, draft).length > 0;
    byId("discard-confirm").disabled = !canEdit();
    const emptyCount=draft?.memories.filter((memory) => memory.empty).length || 0;
    byId("add-channel").disabled = !canEdit() || emptyCount === 0;
    byId("add-channel-confirm").disabled = !canEdit() || emptyCount === 0 || !addContext;
    byId("repeater-presets").disabled = !canEdit() || presetSlots().length === 0 || configuredPairs().length === 0;
    byId("repeater-presets-confirm").disabled = !validPresetContext() || presetCandidate().errors.length > 0;
    byId("delete-channel-confirm").disabled = !canEdit() || !deleteProposal;
    byId("channel-capacity").textContent = draft ? emptyCount ? `${emptyCount} empty ${emptyCount === 1 ? "slot" : "slots"} available` : "All 128 memories are in use. Delete a channel in the draft to free a slot." : "";
    pill("draft-state", dirty ? `${changedCount()} ${changedCount() === 1 ? "change" : "changes"} in draft` : recoveryId() ? "Current values read" : "No changes", dirty || recoveryId() ? "amber" : "muted");
    pill("connection-state", connected ? "Connected to Pi" : "Connection unavailable", connected ? "good" : "amber");
  }

  function renderTask() {
    const state = status?.radio_programming || {};
    const configured = configuredRoles();
    const key = configured.join(",");
    if (key !== configuredRolesKey) {
      configuredRolesKey = key;
      const select = byId("radio-role"); select.replaceChildren();
      for (const item of configured) { const option = create("option", item === "programmer" ? "External programmer" : `${item.toUpperCase()} radio`); option.value=item; select.append(option); }
      if (!configured.includes(role)) { role=configured[0] || "rx"; clearSnapshot(); }
      select.value=role;
    }
    const requiredModel = pendingModel();
    const nextModel = requiredModel || (supportedModels("programmer").includes(externalModel) ? externalModel : supportedModels("programmer")[0]);
    if (nextModel && externalModel !== nextModel) {
      externalModel = nextModel;
      if (role === "programmer") { closeDialogs(); clearSnapshot(); readAttempt = null; }
    }
    const pending = state.pending === true;
    const unfinished = verificationRoles();
    pill("task-state", pending ? "Working" : unfinished.length ? "Verify upload" : state.success === false ? "Check radio" : state.available === true ? "Ready" : "Unavailable", pending || unfinished.length ? "amber" : state.success === false ? "error" : "muted");
    let text = state.message || "Read the selected radio to create a local backup and an editable snapshot.";
    if (!state.available) text = state.message || "Install the current Pi programming helper and configure its RX and TX AIOC assignments first.";
    if (unfinished.length) {
      text += ` Radio audio remains paused for ${unfinished.map((item) => item.toUpperCase()).join(" and ")}.`;
      if (!pending) text += " Use Verify upload to read back the saved candidate, or read the current settings to prepare a reviewed repair. If the radio does not reconnect, restart it before retrying.";
    }
    if (status?.software_updates?.pending === true) text = "A software update is in progress. Wait until it completes before accessing a radio.";
    message("task-message", errorMessage || text, errorMessage || state.success === false ? "error" : state.success === true ? "success" : "");
    const backup = validId(state.backup_id) ? state.backup_id : null;
    byId("backup-note").hidden = !backup;
    byId("backup-note").textContent = backup ? `Local backup: ${backup} · Five sets per radio maximum` : "";
    byId("task-progress").hidden = !pending;
    byId("task-phase").textContent = typeof state.phase === "string" ? state.phase.replace(/[-_]/g," ") : "Waiting for the radio worker";
    const progress = state.progress;
    const knownProgress = Number.isInteger(progress?.current) && Number.isInteger(progress?.total) && progress.total > 0 && progress.current >= 0 && progress.current <= progress.total;
    const bar = byId("task-progress-bar");
    if (knownProgress) { bar.max=progress.total; bar.value=progress.current; }
    else bar.removeAttribute("value");
    byId("task-progress-detail").textContent = knownProgress ? `${progress.current} / ${progress.total} ${progress.unit === "blocks" ? "blocks" : "steps"}` : "";
    const events = Array.isArray(state.events) ? state.events.slice(-20) : [];
    byId("task-activity").hidden = events.length === 0;
    const list = byId("task-events"); list.replaceChildren();
    for (const event of events) {
      if (typeof event?.message !== "string") continue;
      const date = new Date(event.time);
      list.append(create("li", `${Number.isFinite(date.getTime()) ? date.toLocaleTimeString() + " · " : ""}${event.message}`));
    }
    renderControls();
  }

  function clearSnapshot() {
    snapshot = null; draft = null; loadedReadId = null;
    deletedMemories.clear(); addContext=null; deleteProposal=null; presetContext=null;
    byId("snapshot-panel").hidden = true;
    byId("snapshot-empty").hidden = false;
    byId("memory-rows").replaceChildren();
    byId("radio-settings").replaceChildren();
    byId("recovery-notice").hidden=true;
    byId("recovery-mismatch").textContent="";
    byId("recovery-differences").replaceChildren();
    byId("review-recovery-differences").replaceChildren();
    byId("change-list").replaceChildren();
    renderControls();
  }

  function loadSnapshot(value) {
    if (!value || value.schema !== 1 || value.role !== role || snapshotModel(value) !== selectedModel() || !supportedModels().includes(snapshotModel(value)) || !validId(value.base_id) || !Array.isArray(value.memories) || !value.settings || !value.capabilities) return false;
    snapshot = clone(value); draft = clone(value); loadedReadId = value.base_id;
    deletedMemories.clear(); addContext=null; deleteProposal=null; presetContext=null;
    byId("snapshot-empty").hidden = true;
    byId("snapshot-panel").hidden = false;
    byId("snapshot-title").textContent = `${role === "programmer" ? "External programmer" : role.toUpperCase()} radio memories`;
    const date = new Date(value.read_at);
    byId("snapshot-meta").textContent = `${modelName(snapshotModel(value))} · Firmware ${value.firmware || "Unknown"}`;
    byId("snapshot-detail").textContent = `Read ${Number.isFinite(date.getTime()) ? date.toLocaleString() : "time unavailable"}. ${value.compatible === true ? "Edit memories or operating settings below." : "This snapshot is read-only because the profile needs review."}`;
    byId("recovery-notice").hidden = !recoveryId(value);
    const mismatch = typeof value.recovery?.message === "string" ? value.recovery.message.trim() : "";
    byId("recovery-mismatch").hidden = !mismatch;
    byId("recovery-mismatch").textContent = mismatch ? `Previous verification result: ${mismatch}` : "";
    byId("recovery-detail").textContent = value.recovery?.protected_mismatch === true
      ? "Protected radio data differs from the previous candidate. You can inspect this read, but a repair write requires a local profile review. Radio audio remains paused."
      : "This fresh read shows the radio’s current settings. The previous candidate stays protected until its replacement is verified. Edit a draft or review adopting the current values. Radio audio stays paused until all unfinished uploads verify.";
    renderRecoveryDifferences("recovery-differences", false);
    renderMemories(); renderSettings(); renderControls();
    return true;
  }

  function create(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function renderRecoveryDifferences(id, reviewing) {
    const list = byId(id); list.replaceChildren();
    const differences = Array.isArray(snapshot?.recovery?.differences) ? snapshot.recovery.differences.slice(0,20) : [];
    list.hidden = !recoveryId() || !differences.length;
    for (const difference of differences) {
      if (typeof difference?.field !== "string" || typeof difference.expected !== "string" || typeof difference.actual !== "string") continue;
      const item = create("li"); item.append(create("strong", snapshot.capabilities.settings?.[difference.field]?.label || difference.field));
      let desired = difference.actual;
      if (difference.field in draft.settings) desired=String(draft.settings[difference.field]);
      else if (changedCount()) desired="Current value unless listed as a draft change below";
      item.append(create("span", `Previous candidate: ${difference.expected} · Radio read: ${difference.actual}${reviewing ? ` · Desired repair value: ${desired}` : ""}`)); list.append(item);
    }
  }

  function optionControl(options, value, label, onChange) {
    const select = create("select");
    select.setAttribute("aria-label", label);
    for (const item of options) {
      const option = create("option", typeof item === "object" ? item.label : String(item));
      option.value = typeof item === "object" ? String(item.value) : String(item);
      select.append(option);
    }
    select.value = String(value);
    select.addEventListener("change", () => onChange(select.value));
    return select;
  }

  function inputControl(type, value, label, onChange) {
    const input = create("input"); input.type = type; input.value = value;
    input.setAttribute("aria-label", label);
    input.addEventListener("input", () => onChange(input.value, input));
    return input;
  }

  function markDraft(number) {
    const row = byId(`memory-row-${number}`);
    const memory = draft.memories.find((item) => item.number === number);
    const original = snapshot.memories.find((item) => item.number === number);
    row?.classList.toggle("changed", !same(memory, original));
    message("memory-error", validationErrors(snapshot, draft).slice(0, 4).join(" "));
    renderControls();
  }

  function defaultMemory(number) {
    return { number, empty: false, name: "", rx_hz: 0, tx_hz: null, mode: "NFM", power: "High", skip: false, tx_tone: null, rx_tone: null };
  }

  function presetSlots() {
    const originals = new Map((snapshot?.memories || []).map((memory) => [memory.number,memory]));
    return (draft?.memories || []).filter((memory) => memory.number !== 1 && memory.empty === true && originals.get(memory.number)?.empty === true)
      .sort((left,right) => (left.number === 0 ? 128 : left.number) - (right.number === 0 ? 128 : right.number));
  }

  function validPresetContext() {
    return !!presetContext && byId("repeater-presets-dialog").open && canEdit()
      && presetContext.role === role && presetContext.model === selectedModel() && presetContext.catalogDigest === status?.radio_programming?.catalog_digest && configuredPairs().length > 0 && presetContext.base_id === snapshot.base_id && presetContext.csrf === session.csrf_token;
  }

  function selectedPreset() {
    return configuredPairs().find((pair) => pair.id === byId("preset-pair").value) || null;
  }

  function presetMemory(preset, number, selectedRole = role) {
    if (!roles.includes(selectedRole)) return null;
    const tone = preset.tone_hz === null ? null : {type:"CTCSS",value:preset.tone_hz};
    return {...defaultMemory(number),name:selectedRole === "rx" ? preset.rx_name : selectedRole === "tx" ? preset.tx_name : `BOX-CH${preset.index}`,
      rx_hz:selectedRole === "rx" ? preset.input_hz : preset.output_hz,
      tx_hz:selectedRole === "rx" ? null : selectedRole === "tx" ? preset.output_hz : preset.input_hz,
      tx_tone:selectedRole === "rx" ? null : clone(tone),rx_tone:selectedRole === "rx" ? clone(tone) : null};
  }

  function presetCandidate() {
    const preset = selectedPreset();
    if (!preset) return {errors:["Configure channel pairs locally on the Pi, then select one."]};
    const value = byId("preset-slot").value;
    const number = Number(value);
    if (!/^\d{1,3}$/.test(value) || !presetSlots().some((memory) => memory.number === number)) return {errors:["Choose a memory that was empty when read and is still empty in this draft. Memory 1 is reserved."]};
    const candidate = clone(draft);
    const memory = presetMemory(preset,number);
    candidate.memories[candidate.memories.findIndex((item) => item.number === number)] = memory;
    return {preset,memory,candidate,errors:validationErrors(snapshot,candidate)};
  }

  function renderPresetPreview() {
    const preset = selectedPreset();
    const result = presetCandidate();
    const noPair = !preset;
    byId("preset-pair").disabled = noPair || !validPresetContext();
    byId("preset-slot").disabled = noPair || !validPresetContext() || presetSlots().length === 0;
    byId("preset-role-note").textContent = role === "rx"
      ? "RX radio: receive the configured input and tone; transmit stays disabled."
      : role === "tx" ? "TX radio: transmit the configured output and tone. Receive tone squelch stays off."
        : "External programmer handheld: listen on the configured output and transmit on the input with the configured tone. Receive tone squelch stays off so other traffic is audible.";
    byId("preset-preview").textContent = result.memory ? `Memory ${result.memory.number} · ${summary(result.memory)}`
      : noPair ? "No private channel pairs are configured on this Pi." : "Select an empty memory to preview this channel.";
    byId("preset-slot-note").textContent = preset ? `Suggested matching memory: ${(preset.index + 1)}. ${result.memory && result.memory.number !== (preset.index + 1) ? `Memory ${(preset.index + 1)} is unavailable or you selected another slot; use memory ${result.memory.number} on both box radios and the handhelds.` : "Use this same memory number on both box radios and the handhelds."} Existing occupied channels and memory 1 are never replaced by a preset.` : "";
    message("preset-error", noPair ? "" : result.errors.slice(0,4).join(" "));
    byId("repeater-presets-confirm").disabled = !validPresetContext() || result.errors.length > 0;
  }

  function renderPresetChoices(resetSlot = true) {
    const pairs = configuredPairs();
    const pairSelect = byId("preset-pair");
    const previousPair = pairSelect.value;
    pairSelect.replaceChildren();
    for (const pair of pairs) {
      const option = create("option",`${pair.label} · ${pair.rx_name} / ${pair.tx_name} · Input ${formatMHz(pair.input_hz)} / output ${formatMHz(pair.output_hz)} MHz`);
      option.value=pair.id; pairSelect.append(option);
    }
    pairSelect.value = pairs.some((pair) => pair.id === previousPair) ? previousPair : pairs[0]?.id || "";
    byId("preset-guidance").textContent = pairs.length ? "These pairs come from this Pi’s private configuration. Select an empty memory to stage a draft for the connected radio." : "Configure channel pairs locally on the Pi first.";
    if (resetSlot) {
      const slots = presetSlots();
      const slotSelect = byId("preset-slot"); slotSelect.replaceChildren();
      for (const memory of slots) { const option=create("option",`Memory ${memory.number}`); option.value=String(memory.number); slotSelect.append(option); }
      const preferred = selectedPreset()?.index + 1;
      slotSelect.value = String(slots.some((memory) => memory.number === preferred) ? preferred : slots[0]?.number ?? "");
    }
    renderPresetPreview();
  }

  function openRepeaterPresets() {
    if (!canEdit() || !presetSlots().length || !configuredPairs().length) return;
    presetContext={role,model:selectedModel(),catalogDigest:status.radio_programming.catalog_digest,base_id:snapshot.base_id,csrf:session.csrf_token};
    message("preset-error",""); showDialog("repeater-presets-dialog","preset-pair"); renderPresetChoices(); renderControls();
  }

  function confirmRepeaterPreset() {
    if (!validPresetContext()) return;
    const result=presetCandidate();
    if (result.errors.length) { message("preset-error",result.errors.slice(0,4).join(" ")); renderControls(); return; }
    const number=result.memory.number;
    draft=result.candidate;
    deletedMemories.delete(number);
    presetContext=null; byId("repeater-presets-dialog").close(); revealMemory(number); renderControls();
  }

  function openAddChannel() {
    if (!canEdit()) return;
    const empty=draft.memories.filter((memory) => memory.empty);
    if (!empty.length) return;
    const select=byId("add-channel-slot"); select.replaceChildren();
    for (const memory of empty) {
      const option=create("option",`Memory ${memory.number}${deletedMemories.has(memory.number) ? " · deleted in draft" : ""}`); option.value=String(memory.number); select.append(option);
    }
    select.value=String(empty[0].number);
    addContext={role,model:selectedModel(),base_id:snapshot.base_id,csrf:session.csrf_token};
    message("add-channel-error",""); showDialog("add-channel-dialog","add-channel-slot"); renderControls();
  }

  function revealMemory(number) {
    byId("memory-search").value="";
    byId("used-only").checked=false;
    selectView("memories"); renderMemories(); markDraft(number);
    const input=byId(`memory-rx-${number}`);
    input?.focus();
    input?.scrollIntoView?.({block:"nearest",inline:"nearest"});
  }

  function addChannel(number) {
    if (!canEdit() || !Number.isInteger(number)) return false;
    const index=draft.memories.findIndex((memory) => memory.number === number);
    if (index < 0 || !draft.memories[index].empty) return false;
    draft.memories[index]=defaultMemory(number);
    deletedMemories.delete(number);
    revealMemory(number);
    return true;
  }

  function confirmAddChannel() {
    if (!addContext || !byId("add-channel-dialog").open || !canEdit() || addContext.role !== role || addContext.model !== selectedModel() || addContext.base_id !== snapshot.base_id || addContext.csrf !== session.csrf_token) return;
    const value=byId("add-channel-slot").value;
    const number=Number(value);
    if (!/^\d{1,3}$/.test(value) || !draft.memories.some((memory) => memory.number === number && memory.empty)) { message("add-channel-error","Choose an empty memory slot from the list."); return; }
    addContext=null; byId("add-channel-dialog").close(); addChannel(number); renderControls();
  }

  function requestDeleteChannel(number) {
    if (!canEdit()) return;
    const memory=draft.memories.find((item) => item.number === number);
    if (!memory || memory.empty) return;
    deleteProposal={number,role,model:selectedModel(),base_id:snapshot.base_id,csrf:session.csrf_token,memoryKey:stable(memory)};
    byId("delete-channel-title").textContent=`Delete memory ${number}?`;
    byId("delete-channel-description").textContent=`${memory.name || "Unnamed channel"} · ${formatMHz(memory.rx_hz) || "Receive frequency not set"}${memory.rx_hz ? " MHz" : ""}`;
    showDialog("delete-channel-dialog","delete-channel-cancel"); renderControls();
  }

  function confirmDeleteChannel() {
    const selected=deleteProposal;
    if (!selected || !byId("delete-channel-dialog").open || !canEdit() || selected.role !== role || selected.model !== selectedModel() || selected.base_id !== snapshot.base_id || selected.csrf !== session.csrf_token) return;
    const index=draft.memories.findIndex((memory) => memory.number === selected.number);
    if (index < 0 || draft.memories[index].empty || stable(draft.memories[index]) !== selected.memoryKey) return;
    deletedMemories.set(selected.number,clone(draft.memories[index]));
    draft.memories[index]={number:selected.number,empty:true};
    deleteProposal=null; byId("delete-channel-dialog").close(); renderMemories(); markDraft(selected.number);
    byId(`memory-action-${selected.number}`)?.focus();
  }

  function undoDeleteChannel(number) {
    if (!canEdit() || !deletedMemories.has(number)) return;
    const index=draft.memories.findIndex((memory) => memory.number === number);
    if (index < 0 || !draft.memories[index].empty) return;
    draft.memories[index]=clone(deletedMemories.get(number));
    deletedMemories.delete(number); revealMemory(number);
  }

  function memoryMatches(memory) {
    const search = byId("memory-search").value.trim().toLowerCase();
    const searchable=deletedMemories.get(memory.number) || memory;
    return (!byId("used-only").checked || !memory.empty || deletedMemories.has(memory.number)) && (!search || `${memory.number} ${searchable.name || ""} ${formatMHz(searchable.rx_hz)} ${formatMHz(searchable.tx_hz)}`.toLowerCase().includes(search));
  }

  function renderMemories() {
    const body = byId("memory-rows"); body.replaceChildren();
    if (!draft) return;
    let shown = 0;
    for (const memory of draft.memories) {
      if (!memoryMatches(memory)) continue;
      shown++;
      const row = create("tr"); row.id = `memory-row-${memory.number}`;
      row.className = memory.empty ? "empty" : "";
      const original = snapshot.memories.find((item) => item.number === memory.number);
      row.classList.toggle("changed", !same(memory, original));
      row.append(create("td", String(memory.number)));
      const cell = (node) => { const td = create("td"); if (node) td.append(node); row.append(td); return td; };
      const deleted=deletedMemories.has(memory.number);
      const action=create("button",memory.empty ? deleted ? "Undo" : "Add" : "Delete","memory-action"); action.id=`memory-action-${memory.number}`; action.type="button";
      action.setAttribute("aria-label",`${memory.empty ? deleted ? "Undo deletion of" : "Add" : "Delete"} memory ${memory.number}`);
      action.addEventListener("click",() => memory.empty ? deleted ? undoDeleteChannel(memory.number) : addChannel(memory.number) : requestDeleteChannel(memory.number));
      cell(action);
      if (memory.empty) {
        const previous=deletedMemories.get(memory.number);
        const note = cell(create("span", previous ? `Deleted in draft · ${previous.name || "Unnamed"} · ${formatMHz(previous.rx_hz) || "No frequency"}${previous.rx_hz ? " MHz" : ""} · Undo restores your edits` : "Empty memory — select Add to create a channel")); note.colSpan = 13;
        body.append(row); continue;
      }
      const label = (name) => `Memory ${memory.number} ${name}`;
      const name = inputControl("text", memory.name, label("name"), (value) => { memory.name = value; markDraft(memory.number); });
      name.maxLength = snapshot.capabilities.name_length || 7; cell(name);
      const rx = inputControl("text", formatMHz(memory.rx_hz), label("receive frequency in MHz"), (value, input) => {
        const hz = parseMHz(value); memory.rx_hz = hz ?? 0; input.classList.toggle("invalid", hz === null);
        if (txMode.value === "simplex") memory.tx_hz = memory.rx_hz;
        else if (["+", "-"].includes(txMode.value)) setTransmit(offset.value);
        markDraft(memory.number);
      });
      rx.id=`memory-rx-${memory.number}`; rx.inputMode = "decimal"; rx.placeholder = "MHz required"; cell(rx);
      const mode = memory.tx_hz === null ? "off" : memory.tx_hz === memory.rx_hz ? "simplex" : "split";
      const txMode = optionControl([{value:"off",label:"Disabled"},{value:"simplex",label:"Simplex"},{value:"+",label:"+ offset"},{value:"-",label:"− offset"},{value:"split",label:"TX frequency"}], mode, label("transmit mode"), (value) => {
        if (value === "off") memory.tx_hz = null;
        else if (value === "simplex") memory.tx_hz = memory.rx_hz;
        else if (["+", "-"].includes(value)) { offset.value = "0.600000"; setTransmit(offset.value); }
        else { offset.value = formatMHz(memory.tx_hz ?? memory.rx_hz); setTransmit(offset.value); }
        offset.disabled = ["off", "simplex"].includes(value);
        if (offset.disabled) offset.value = "";
        markDraft(memory.number);
      });
      cell(txMode);
      function setTransmit(value) {
        const hz = parseMHz(value);
        memory.tx_hz = hz === null ? 0 : txMode.value === "+" ? memory.rx_hz + hz : txMode.value === "-" ? memory.rx_hz - hz : hz;
      }
      const offset = inputControl("text", mode === "split" ? formatMHz(memory.tx_hz) : "", label("offset or transmit frequency in MHz"), (value, input) => {
        setTransmit(value); input.classList.toggle("invalid", parseMHz(value) === null); markDraft(memory.number);
      });
      offset.inputMode = "decimal"; offset.disabled = ["off", "simplex"].includes(mode); cell(offset);
      for (const direction of ["tx", "rx"]) {
        const key = `${direction}_tone`;
        let tone = memory[key];
        const type = optionControl([{value:"",label:"Off"},{value:"CTCSS",label:"CTCSS"},{value:"DCS",label:"DCS"}], tone?.type || "", label(`${direction.toUpperCase()} tone type`), (value) => {
          memory[key] = !value ? null : value === "CTCSS" ? {type:value,value:snapshot.capabilities.ctcss[0]}
            : {type:value,value:snapshot.capabilities.dcs[0],polarity:"N"};
          renderMemories(); markDraft(memory.number);
        });
        cell(type);
        const values = tone?.type === "DCS" ? snapshot.capabilities.dcs : snapshot.capabilities.ctcss;
        const value = optionControl((values || []).map((number) => ({value:number,label:tone?.type === "DCS" ? String(number).padStart(3,"0") : `${number} Hz`})), tone?.value ?? "", label(`${direction.toUpperCase()} tone value`), (selected) => {
          if (memory[key]) memory[key].value = Number(selected); markDraft(memory.number);
        });
        value.disabled = !tone; cell(value);
        const polarity = optionControl([{value:"N",label:"Normal"},{value:"R",label:"Reverse"}], tone?.polarity || "N", label(`${direction.toUpperCase()} DCS polarity`), (selected) => {
          if (memory[key]?.type === "DCS") memory[key].polarity = selected; markDraft(memory.number);
        });
        polarity.disabled = tone?.type !== "DCS"; cell(polarity);
      }
      cell(optionControl(["High","Low"], memory.power, label("power"), (value) => { memory.power=value; markDraft(memory.number); }));
      cell(optionControl(["NFM","FM"], memory.mode, label("modulation"), (value) => { memory.mode=value; markDraft(memory.number); }));
      cell(optionControl([{value:"false",label:"Include"},{value:"true",label:"Skip"}], String(memory.skip), label("scan"), (value) => { memory.skip=value === "true"; markDraft(memory.number); }));
      body.append(row);
    }
    byId("memory-count").textContent = `${shown} of ${draft.memories.length} memories shown`;
    renderControls();
  }

  function renderSettings() {
    const container = byId("radio-settings"); container.replaceChildren();
    if (!draft) return;
    for (const [key, descriptor] of Object.entries(snapshot.capabilities.settings || {})) {
      if (!(key in draft.settings)) continue;
      const item = create("div", undefined, "editor-setting");
      const label = create("label", descriptor.label || key); const id = `setting-${key.replace(/[^a-z0-9]/gi,"-")}`;
      label.htmlFor = id; item.append(label);
      const update = (value) => { draft.settings[key] = value; item.classList.toggle("changed", !same(value,snapshot.settings[key])); renderControls(); };
      let control;
      if (descriptor.type === "boolean") {
        control = optionControl([{value:"false",label:"Off"},{value:"true",label:"On"}], String(draft.settings[key]), descriptor.label, (value) => update(value === "true"));
      } else if (descriptor.type === "select") {
        control = optionControl(descriptor.options || [], draft.settings[key], descriptor.label, update);
      } else if (descriptor.type === "integer") {
        control = inputControl("number", draft.settings[key], descriptor.label, (value) => update(value.trim() === "" ? null : Number(value)));
        control.min = descriptor.min; control.max = descriptor.max; control.step = 1;
      } else continue;
      control.id = id; item.append(control); item.classList.toggle("changed", !same(draft.settings[key],snapshot.settings[key])); container.append(item);
    }
  }

  async function request(path, body) {
    const options = { credentials:"same-origin", cache:"no-store", headers:{Accept:"application/json"} };
    if (body !== undefined) {
      options.method = "POST"; options.headers["Content-Type"] = "application/json";
      if (path !== "/api/admin/login") {
        if (!authenticated()) throw new Error("Sign in as admin before continuing.");
        options.headers["X-CSRF-Token"] = session.csrf_token;
      }
      options.body = JSON.stringify(body);
    }
    const controller = new AbortController(); options.signal = controller.signal;
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, options);
      let data; try { data = await response.json(); } catch (_) { throw new Error("The Pi returned an unreadable response."); }
      if (!response.ok) {
        if ([401,403].includes(response.status)) setSession({...session,authenticated:false,csrf_token:null});
        throw new Error(data.error || data.message || `Request failed (${response.status}).`);
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The Pi took too long to respond. Check task status before retrying an operation.");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  async function refreshStatus() {
    const data = await request("/api/status"); status = data; connected = true;
    if (data.admin?.authenticated === false) setSession({...session,authenticated:false,csrf_token:null});
    renderTask(); return data;
  }

  async function fetchSnapshot(force = false) {
    if (!authenticated() || (role === "programmer" && externalReadRequired)) return;
    const selectedRole = role;
    const model = selectedModel();
    const selectedCsrf = session.csrf_token;
    const startingSnapshot = snapshot;
    const startingDraft = stable(draft);
    const data = await request(`/api/radio-editor?role=${selectedRole}${selectedRole === "programmer" ? `&model=${encodeURIComponent(model)}` : ""}`);
    if (selectedRole !== role || model !== selectedModel() || !authenticated() || selectedCsrf !== session.csrf_token) return;
    if (data.radio_programming) { status = {...status,radio_programming:data.radio_programming}; renderTask(); }
    if (selectedRole !== role || model !== selectedModel()) return;
    const unchanged = snapshot === startingSnapshot && stable(draft) === startingDraft;
    if (data.editor && unchanged && (force || (!changedCount() && data.editor.base_id !== loadedReadId))) loadSnapshot(data.editor);
    return data.editor;
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      await refreshStatus();
      const task = status?.radio_programming;
      if (authenticated() && task?.pending !== true && task?.action === "editor-read" && task.success === true && readAttempt && (readAttempt.model || "5RX") === selectedModel() && task.request_id !== readAttempt.priorRequestId && task.role === readAttempt.role && task.role === role) {
        readAttempt = null;
        if (role === "programmer") externalReadRequired = false;
        if (task.request_id !== loadedReadId) await fetchSnapshot(true);
      } else if (authenticated() && task?.pending !== true && ["editor-read","editor-write"].includes(task?.action) && task.success === true && task.role === role && task.request_id !== loadedReadId && !changedCount()) {
        await fetchSnapshot();
      }
    } catch (error) { connected = false; errorMessage = error.message; renderTask(); }
    finally { refreshing = false; renderControls(); }
  }

  function closeDialogs() {
    proposal = null;
    repairProposal = null;
    discardContext = null;
    addContext=null; deleteProposal=null; presetContext=null;
    for (const id of ["read-dialog","review-dialog","discard-dialog","verify-dialog","repair-dialog","add-channel-dialog","delete-channel-dialog","repeater-presets-dialog"]) if (byId(id).open) byId(id).close();
  }

  function showDialog(id, focusId) {
    byId(id).showModal(); byId(focusId).focus();
  }

  function closeRepairConfirmation() {
    repairProposal=null;
    if (byId("repair-dialog").open) byId("repair-dialog").close();
    renderControls();
    if (!byId("repair-radio").disabled) byId("repair-radio").focus();
  }

  function showRepairConfirmation() {
    if (!canRepairSavedUpload()) return;
    repairProposal={role,model:selectedModel(),pendingId:status.radio_programming.roles[role].pending_id,requestId:status.radio_programming.request_id,
      identity:repairIdentityKey(),csrf:session.csrf_token,draftKey:stable(draft)};
    byId("repair-title").textContent=`Repair the saved ${roleName()} upload?`;
    byId("repair-description").textContent=`Restore the private saved candidate for this unfinished ${roleName()} upload using the configured ${modelName()} profile. This restores the affected memory pages, including neighboring bytes that may have been cleared. It restores that reviewed upload; it does not apply a new default plan.`;
    byId("repair-draft-warning").hidden=!changedCount();
    message("repair-error","");
    showDialog("repair-dialog","repair-cancel");renderControls();
  }

  async function repairSavedUpload() {
    const selected=repairProposal;
    if (!selected || !byId("repair-dialog").open || !canRepairSavedUpload() || selected.role !== role || selected.model !== selectedModel() ||
        selected.csrf !== session.csrf_token || selected.identity !== repairIdentityKey() || selected.draftKey !== stable(draft)) return;
    busy=true;errorMessage="";renderControls();
    try {
      await refreshStatus();
      if (repairProposal !== selected || !byId("repair-dialog").open || !canRepairSavedUpload(true) || selected.role !== role || selected.model !== selectedModel() ||
          selected.csrf !== session.csrf_token || selected.requestId !== status?.radio_programming?.request_id ||
          selected.pendingId !== status?.radio_programming?.roles?.[role]?.pending_id || selected.identity !== repairIdentityKey() ||
          selected.draftKey !== stable(draft)) throw new Error("The unfinished upload, radio connection, draft, or admin session changed. Review the saved-upload repair again.");
      const data=await request("/api/radio-programming",{role:selected.role,action:"repair-upload",pending_id:selected.pendingId});
      if (!data.radio_programming || typeof data.radio_programming !== "object" || Array.isArray(data.radio_programming)) throw new Error("The Pi returned an unreadable radio status. Check task status before retrying.");
      status={...status,radio_programming:data.radio_programming};
      closeDialogs();clearSnapshot();renderTask();
    } catch (error) { errorMessage=error.message;closeDialogs();renderTask(); }
    finally { busy=false;renderControls(); }
  }

  function reviewDraft() {
    if (!canReview()) return;
    const errors = validationErrors(snapshot,draft);
    message("memory-error", errors.slice(0,4).join(" "));
    if (errors.length) return;
    const changes = changesFor(snapshot,draft);
    proposal = {role,model:selectedModel(),base_id:snapshot.base_id,changes,csrf:session.csrf_token,draftKey:stable(draft),supersede_id:recoveryId()};
    const list = byId("change-list"); list.replaceChildren();
    const counts=memoryChangeCounts();
    byId("review-change-counts").textContent=`${counts.added} added · ${counts.deleted} deleted · ${counts.updated} updated ${Object.keys(changes.settings).length ? `· ${Object.keys(changes.settings).length} radio setting changes` : ""}`;
    for (const memory of changes.changes) {
      const original=snapshot.memories.find((item) => item.number === memory.number);
      const operation=memory.empty ? "Deleted" : original?.empty ? "Added" : "Updated";
      const item = create("li"); item.append(create("strong", `${operation} · Memory ${memory.number}`),
        create("span", `Before: ${summary(original,true)}`, "editor-change-before"),
        create("span", `After: ${memory.empty ? "Clear this memory on the radio · Empty memory" : summary(memory,true)}`, "editor-change-after")); list.append(item);
    }
    for (const [key,value] of Object.entries(changes.settings)) {
      const item = create("li"); item.append(create("strong",snapshot.capabilities.settings[key].label || key),
        create("span",`Before: ${String(snapshot.settings[key])}`,"editor-change-before"),
        create("span",`After: ${String(value)}`,"editor-change-after")); list.append(item);
    }
    if (proposal.supersede_id && !changedCount()) {
      const item=create("li"); item.append(create("strong","Adopt the current values from this fresh read"),create("span","No additional field edits. Replace the old expectation with the radio’s current operating settings; protected calibration and reserved data must still match.")); list.append(item);
    }
    byId("review-title").textContent = `${proposal.supersede_id ? "Repair" : "Write"} ${roleName()}?`;
    byId("review-recovery-warning").hidden = !proposal.supersede_id;
    renderRecoveryDifferences("review-recovery-differences", true);
    byId("write-confirm").textContent = proposal.supersede_id ? changedCount() ? "Repair and write changes" : "Adopt current values" : "Write changes";
    message("review-error", ""); showDialog("review-dialog","review-cancel"); renderControls();
  }

  async function writeDraft() {
    const selected = proposal;
    if (!selected || !canReview() || selected.csrf !== session.csrf_token || selected.role !== role || selected.model !== selectedModel() || selected.base_id !== snapshot.base_id || selected.draftKey !== stable(draft) || selected.supersede_id !== recoveryId()) return;
    try {
      await refreshStatus();
      if (proposal !== selected || !canReview() || selected.csrf !== session.csrf_token || selected.role !== role || selected.model !== selectedModel() || selected.base_id !== snapshot.base_id || selected.draftKey !== stable(draft) || selected.supersede_id !== recoveryId()) throw new Error("The task or session changed. Keep editing and review again.");
      const current = await fetchSnapshot(false);
      if (proposal !== selected || !canReview() || selected.csrf !== session.csrf_token || selected.role !== role || selected.model !== selectedModel() ||
          selected.base_id !== snapshot.base_id || selected.draftKey !== stable(draft) || selected.supersede_id !== recoveryId() ||
          current?.base_id !== selected.base_id || snapshotModel(current) !== selected.model || recoveryId(current) !== selected.supersede_id) throw new Error("The radio snapshot, draft, admin session, or unfinished candidate changed. Keep editing and review again before writing.");
      busy = true; errorMessage = ""; renderControls();
      const payload = {role:selected.role,base_id:selected.base_id,...selected.changes};
      if (selected.supersede_id) payload.supersede_id=selected.supersede_id;
      const data = await request("/api/radio-editor/write",payload);
      status = {...status,radio_programming:data.radio_programming};
      closeDialogs(); clearSnapshot(); renderTask();
    } catch (error) { errorMessage = error.message; message("review-error",error.message); renderTask(); }
    finally { busy = false; renderControls(); }
  }

  async function startOperation(action) {
    if (!available() || (action === "verify" && !verificationRoles().includes(role))) return;
    const selectedRole = role; const csrf = session.csrf_token;
    const model = selectedModel();
    try {
      await refreshStatus();
      if (!available() || role !== selectedRole || model !== selectedModel() || csrf !== session.csrf_token || !byId(`${action}-dialog`).open || (action === "verify" && !verificationRoles().includes(role))) return;
      busy = true; errorMessage = ""; renderControls();
      const priorRequestId = status?.radio_programming?.request_id;
      const payload = action === "read" ? {role:selectedRole,...(selectedRole === "programmer" ? {model} : {})} : {role:selectedRole,action:"verify"};
      const data = await request(action === "read" ? "/api/radio-editor/read" : "/api/radio-programming", payload);
      if (selectedRole !== role || model !== selectedModel() || csrf !== session.csrf_token) return;
      status = {...status,radio_programming:data.radio_programming};
      if (action === "read") { readAttempt = {role:selectedRole,model,priorRequestId}; clearSnapshot(); }
      closeDialogs(); renderTask();
    } catch (error) { errorMessage = error.message; closeDialogs(); renderTask(); }
    finally { busy = false; renderControls(); }
  }

  function selectView(name) {
    byId("memories-view").hidden = name !== "memories"; byId("settings-view").hidden = name !== "settings";
    for (const view of ["memories","settings"]) { byId(`${view}-tab`).classList.toggle("active",view === name); byId(`${view}-tab`).setAttribute("aria-pressed",String(view === name)); }
  }

  function discardDraft() {
    if (!snapshot) return;
    draft = clone(snapshot); deletedMemories.clear(); renderMemories(); renderSettings(); message("memory-error",""); renderControls();
  }

  function applySelection(selectedRole, model = externalModel) {
    const modelChanged = selectedRole === "programmer" && role === "programmer" && model !== externalModel;
    closeDialogs(); role = selectedRole;
    if (selectedRole === "programmer") { externalModel = model; if (modelChanged) externalReadRequired = true; }
    byId("radio-role").value = role;
    clearSnapshot(); readAttempt = null; errorMessage = ""; renderTask();
    if (!modelChanged) fetchSnapshot(true).catch((error) => { errorMessage=error.message; renderTask(); });
  }

  function askDiscard(targetRole = null, targetModel = null) {
    discardContext = {role,model:selectedModel(),base_id:snapshot?.base_id,csrf:session.csrf_token,targetRole,targetModel};
    byId("discard-description").textContent = targetModel
      ? `Switch to the ${modelName(targetModel)} profile and discard this draft? The loaded snapshot will be cleared. Read the connected radio using the selected profile before editing. No radio settings will change.`
      : "Your saved radio snapshot and the radio itself will stay unchanged.";
    showDialog("discard-dialog","discard-cancel");
  }

  function selectRole(selected) {
    if (busy || taskPending() || !configuredRoles().includes(selected) || selected === role) return;
    if (changedCount()) { byId("radio-role").value=role; askDiscard(selected); return; }
    applySelection(selected);
  }

  function selectModel(selected) {
    byId("radio-model").value = externalModel;
    if (role !== "programmer" || busy || taskPending() || pendingModel() !== null || !supportedModels().includes(selected) || selected === externalModel) return;
    if (changedCount()) { askDiscard("programmer",selected); return; }
    applySelection("programmer",selected);
  }

  function confirmDiscard() {
    const selected = discardContext;
    if (!selected || !byId("discard-dialog").open || !canEdit() || selected.role !== role || selected.model !== selectedModel() || selected.csrf !== session.csrf_token || selected.base_id !== snapshot?.base_id) return;
    if (selected.targetModel && (pendingModel() !== null || !supportedModels().includes(selected.targetModel))) return;
    if (selected.targetRole && !configuredRoles().includes(selected.targetRole)) return;
    byId("discard-dialog").close(); discardContext = null;
    if (selected.targetRole) applySelection(selected.targetRole,selected.targetModel || externalModel);
    else discardDraft();
  }

  // boot
  byId("login-form").addEventListener("submit",async(event) => {
    event.preventDefault(); if (busy) return; busy=true; renderAccess();
    try { const data=await request("/api/admin/login",{password:byId("admin-password").value}); setSession(data); byId("admin-password").value=""; if (!authenticated()) throw new Error("Sign-in was not accepted."); errorMessage=""; message("admin-message",""); await refreshStatus(); await fetchSnapshot(true); }
    catch(error) { message("admin-message",error.message,"error"); }
    finally { busy=false; renderAccess(); }
  });
  byId("logout-button").addEventListener("click",async() => {
    if (busy) return; busy=true;
    try { await request("/api/admin/logout",{}); setSession({...session,authenticated:false,csrf_token:null}); message("admin-message","Signed out. The editor draft was cleared."); }
    catch(error) { message("admin-message",error.message,"error"); }
    finally { busy=false; renderAccess(); }
  });
  byId("radio-role").addEventListener("change",() => selectRole(byId("radio-role").value));
  byId("radio-model").addEventListener("change",() => selectModel(byId("radio-model").value));
  byId("read-radio").addEventListener("click",() => { if (!available()) return; byId("read-title").textContent=`Read ${roleName()}?`; byId("read-draft-warning").hidden=!changedCount(); showDialog("read-dialog","read-cancel"); renderControls(); });
  byId("read-confirm").addEventListener("click",() => startOperation("read"));
  byId("verify-radio").addEventListener("click",() => { if (!available() || !verificationRoles().includes(role)) return; byId("verify-title").textContent=`Verify ${roleName()}?`; showDialog("verify-dialog","verify-cancel"); renderControls(); });
  byId("verify-confirm").addEventListener("click",() => startOperation("verify"));
  byId("repair-radio").addEventListener("click",showRepairConfirmation);
  byId("repair-confirm").addEventListener("click",repairSavedUpload);
  byId("repair-cancel").addEventListener("click",closeRepairConfirmation);
  byId("repair-dialog").addEventListener("cancel",(event) => { event.preventDefault();closeRepairConfirmation(); });
  byId("repair-dialog").addEventListener("close",() => { repairProposal=null;renderControls(); });
  byId("review-changes").addEventListener("click",reviewDraft);
  byId("write-confirm").addEventListener("click",writeDraft);
  byId("add-channel").addEventListener("click",openAddChannel);
  byId("add-channel-confirm").addEventListener("click",confirmAddChannel);
  byId("repeater-presets").addEventListener("click",openRepeaterPresets);
  byId("preset-pair").addEventListener("change",() => renderPresetChoices());
  byId("preset-slot").addEventListener("change",renderPresetPreview);
  byId("repeater-presets-confirm").addEventListener("click",confirmRepeaterPreset);
  byId("repeater-presets-cancel").addEventListener("click",() => { presetContext=null; byId("repeater-presets-dialog").close(); renderControls(); });
  byId("repeater-presets-dialog").addEventListener("cancel",(event) => { event.preventDefault(); byId("repeater-presets-cancel").click(); });
  byId("repeater-presets-dialog").addEventListener("close",() => { presetContext=null; renderControls(); });
  byId("delete-channel-confirm").addEventListener("click",confirmDeleteChannel);
  for (const name of ["add-channel","delete-channel"]) {
    byId(`${name}-cancel`).addEventListener("click",() => { byId(`${name}-dialog`).close(); if(name === "add-channel")addContext=null; else deleteProposal=null; renderControls(); });
    byId(`${name}-dialog`).addEventListener("cancel",(event) => { event.preventDefault(); byId(`${name}-cancel`).click(); });
  }
  byId("discard-draft").addEventListener("click",() => { if (canEdit() && changedCount()) askDiscard(); });
  byId("discard-confirm").addEventListener("click",confirmDiscard);
  for (const name of ["read","review","discard","verify"]) {
    byId(`${name}-cancel`).addEventListener("click",() => { byId(`${name}-dialog`).close(); if (name === "review") proposal=null; discardContext=null; });
    byId(`${name}-dialog`).addEventListener("cancel",(event) => { event.preventDefault(); byId(`${name}-cancel`).click(); });
    byId(`${name}-dialog`).addEventListener("close",() => { if (name === "review") proposal=null; renderControls(); });
  }
  byId("memory-search").addEventListener("input",renderMemories);
  byId("used-only").addEventListener("change",renderMemories);
  byId("memories-tab").addEventListener("click",() => selectView("memories"));
  byId("settings-tab").addEventListener("click",() => selectView("settings"));
  window.addEventListener("beforeunload",(event) => { if (changedCount()) { event.preventDefault(); event.returnValue=""; } });
  (async() => {
    try { setSession(await request("/api/session")); await refreshStatus(); if (authenticated()) await fetchSnapshot(true); }
    catch(error) { connected=false; message("admin-message",error.message,"error"); renderControls(); }
    let idlePolls = 0;
    setInterval(async() => {
      if (!taskPending()) { if (++idlePolls % 5 === 0) await refresh(); return; }
      if (refreshing) return;
      refreshing = true;
      try {
        const data = await request("/api/radio-programming/status");
        status = {...status,radio_programming:data.radio_programming}; connected=true; renderTask();
        if (data.radio_programming?.pending !== true) { refreshing=false; await refresh(); }
      } catch(error) { connected=false; errorMessage=error.message; renderTask(); }
      finally { refreshing=false; }
    },1000);
  })();
})();

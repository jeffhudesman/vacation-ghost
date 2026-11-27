const eveningScenes = [
  { name: "kitchen", title: "Sunset", time: "19:34", label: "7:34 PM", lamp: "kitchen", text: "Near sunset, the kitchen lamp switches on." },
  { name: "living", title: "After dinner", time: "20:10", label: "8:10 PM", lamp: "living", text: "The kitchen goes dark and the living room lamp switches on." },
  { name: "tv", title: "TV time", time: "20:18", label: "8:18 PM", lamp: "living", glow: "sports", cue: "tv",
    text: "The living room lamp stays on with a fast TV glow. The sound cue is a TV." },
  { name: "break", title: "Kitchen break", time: "21:38", label: "9:38 PM", lamp: "kitchen", cue: "cabinet",
    text: "The kitchen lamp is back on and the TV glow is off. The sound cue is a closing cabinet." },
  { name: "drama", title: "Late TV", time: "21:48", label: "9:48 PM", lamp: "living", glow: "drama",
    text: "The living room lamp is back on. A slower TV glow starts if enabled." },
  { name: "bedtime", title: "Bedtime", time: "22:45", label: "10:45 PM", text: "The last lamp switches off." },
  { name: "morning", title: "Morning kitchen", time: "07:18", label: "7:18 AM", lamp: "kitchen", period: "dawn", cue: "kettle",
    text: "The kitchen lamp is on. The sound cue is a kettle." },
  { name: "report", title: "Morning note", time: "08:00", label: "8:00 AM", period: "dawn" },
];

function setupEveningDemo() {
  const card = document.querySelector(".routine-card");
  const playButton = card?.querySelector("#scene-play");
  const restartButton = card?.querySelector("#scene-restart");
  const soundButton = card?.querySelector("#scene-sound");
  const audio = card?.querySelector("#scene-audio");
  if (!playButton || !restartButton || !soundButton || !audio) return;

  const scene = card.querySelector(".routine-scene");
  const frame = card.querySelector(".routine-frame");
  const caption = card.querySelector(".scene-caption");
  const sceneName = card.querySelector("#scene-name");
  const sceneTime = card.querySelector("#scene-time");
  const sceneText = card.querySelector("#scene-text");
  const soundState = card.querySelector("#scene-sound-state");
  const previous = card.querySelector("#scene-previous");
  const next = card.querySelector("#scene-next");
  const progress = card.querySelector("#scene-progress");
  const report = card.querySelector(".demo-report");
  const sceneButtons = [...card.querySelectorAll("[data-scene-button]")];
  const extras = card.querySelector("#scene-extras");
  const audioStatus = card.querySelector("#scene-audio-status");
  const modeLabel = card.querySelector("#demo-mode");
  const receipt = report.querySelector("blockquote");
  // Both paths reach the receipt after the 21 seconds advertised in the hero.
  const demoDuration = 21000;
  const lightsOnly = eveningScenes.filter(({ cue, glow }) => !cue && !glow);
  let demoScenes = lightsOnly;
  let lastScene = demoScenes.length - 1;
  let sceneDuration = demoDuration / lastScene;
  progress.max = demoDuration / 1000;
  let sceneIndex = null;
  let playState = "ready";
  let timer;
  let progressFrame;
  let nextSceneAt;
  let remaining = sceneDuration;
  let soundEnabled = false;
  let audioRequest = 0;

  function updateProgress(timeLeft = sceneDuration) {
    const elapsed = sceneDuration - Math.min(sceneDuration, Math.max(0, timeLeft));
    progress.value = ((sceneIndex ?? 0) * sceneDuration + elapsed) / 1000;
  }

  function animateProgress(now) {
    updateProgress(nextSceneAt - now);
    progressFrame = requestAnimationFrame(animateProgress);
  }

  function setPlayback(state) {
    playState = state;
    const playLabels = {
      ready: sceneIndex === null ? "Play demo" : "Play from here",
      playing: "Pause",
      paused: "Resume",
      finished: "Complete",
    };
    playButton.textContent = playLabels[state];
    const finished = state === "finished";
    playButton.setAttribute("aria-disabled", String(finished));
    cancelAnimationFrame(progressFrame);
    if (state === "playing") {
      card.dataset.playing = "true";
      progressFrame = requestAnimationFrame(animateProgress);
    } else {
      delete card.dataset.playing;
    }
  }

  function stopAudio() {
    audioRequest++;
    audio.pause();
    audio.currentTime = 0;
  }

  function pausePlayback() {
    if (playState === "playing") {
      clearTimeout(timer);
      remaining = Math.max(0, nextSceneAt - performance.now());
      updateProgress(remaining);
      setPlayback("paused");
    }
    stopAudio();
  }

  function playCue(cue = demoScenes[sceneIndex]?.cue) {
    stopAudio();
    if (!soundEnabled || !cue) return;
    const source = `assets/demo-${cue}.mp3`;
    if (audio.getAttribute("src") !== source) audio.src = source;
    audio.volume = 0.12;
    const request = audioRequest;
    audio.play().catch(() => {
      if (request !== audioRequest) return;
      setSound(false);
      audioStatus.textContent = "Sound unavailable. Select Sound to try again.";
    });
  }

  function showScene(index = null) {
    sceneIndex = index;
    const setting = demoScenes[index] ?? { name: "ready", title: "Dusk", text: "Both lamps are off at dusk." };
    const { name, title, time, label, text, lamp = "none", glow = "none", period = "evening", cue } = setting;
    Object.assign(scene.dataset, { scene: name, lamp, glow, period, cue: cue ?? "none" });
    sceneName.textContent = title;
    sceneTime.hidden = !time;
    if (time) {
      sceneTime.dateTime = time;
      sceneTime.textContent = label;
    }
    const isReport = index === lastScene;
    sceneText.textContent = isReport ? receipt.textContent : text;
    updateProgress();
    report.hidden = !isReport;
    if (isReport) report.dataset.current = "true";
    else delete report.dataset.current;
    previous.setAttribute("aria-disabled", String(index === null || index === 0));
    next.setAttribute("aria-disabled", String(isReport));
    for (const button of sceneButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.sceneButton === name));
    }
    playCue(cue);
  }

  function advanceAfter(delay) {
    nextSceneAt = performance.now() + delay;
    timer = setTimeout(() => {
      showScene(sceneIndex + 1);
      if (sceneIndex === lastScene) setPlayback("finished");
      else advanceAfter(sceneDuration);
    }, delay);
  }

  function keepFrameInView() {
    const { top, bottom } = frame.getBoundingClientRect();
    if (top >= 0 && bottom <= innerHeight) return false;
    card.scrollIntoView({ block: "start", behavior: "instant" });
    return true;
  }

  playButton.addEventListener("click", () => {
    if (playState === "playing") {
      pausePlayback();
      return;
    }
    if (playState === "finished") return;
    keepFrameInView();
    if (sceneIndex === null) showScene(0);
    else playCue();
    advanceAfter(remaining);
    setPlayback("playing");
  });

  function restartPlayback() {
    keepFrameInView();
    clearTimeout(timer);
    remaining = sceneDuration;
    showScene(0);
    advanceAfter(remaining);
    setPlayback("playing");
  }

  restartButton.addEventListener("click", restartPlayback);

  function setSound(enabled) {
    soundEnabled = enabled;
    soundButton.setAttribute("aria-pressed", String(soundEnabled));
    soundState.textContent = soundEnabled ? "on" : "off";
    audioStatus.textContent = "";
    playCue();
  }

  soundButton.addEventListener("click", () => setSound(!soundEnabled));

  function selectScene(index) {
    if (!demoScenes[index]) return false;
    clearTimeout(timer);
    remaining = sceneDuration;
    showScene(index);
    if (index === lastScene) setPlayback("finished");
    else if (playState === "playing" || playState === "paused") setPlayback("paused");
    else setPlayback("ready");
    return true;
  }

  function resetWalkthrough() {
    clearTimeout(timer);
    remaining = sceneDuration;
    showScene();
    setPlayback("ready");
  }

  function setScenes() {
    const prior = demoScenes[sceneIndex];
    demoScenes = extras.checked ? eveningScenes : lightsOnly;
    lastScene = demoScenes.length - 1;
    sceneDuration = demoDuration / lastScene;
    for (const button of sceneButtons) {
      button.hidden = !demoScenes.some(({ name }) => button.dataset.sceneButton === name);
    }
    soundButton.hidden = !extras.checked;
    modeLabel.textContent = extras.checked ? "Two lamps + extras" : "Two lamps";
    if (!extras.checked) setSound(false);
    if (prior && prior.name !== "report") {
      // An optional chapter falls back to the most recent lights-only chapter.
      const preceding = eveningScenes.slice(0, eveningScenes.indexOf(prior) + 1);
      const retained = preceding.filter(setting => demoScenes.includes(setting)).pop();
      selectScene(demoScenes.indexOf(retained));
    } else {
      resetWalkthrough();
    }
  }

  extras.addEventListener("change", setScenes);

  for (const [button, step] of [[previous, -1], [next, 1]]) {
    button.addEventListener("click", () => {
      if (selectScene((sceneIndex ?? -1) + step)) keepFrameInView();
    });
  }

  for (const button of sceneButtons) {
    button.addEventListener("click", () => {
      selectScene(demoScenes.findIndex(({ name }) => name === button.dataset.sceneButton));
      if (keepFrameInView()) {
        (sceneIndex === lastScene ? report : caption).focus({ preventScroll: true });
      }
    });
  }

  const picker = card.querySelector(".routine-picker");
  if (picker) {
    const compact = window.matchMedia("(max-width: 480px)");
    const setDefaultPicker = () => { picker.open = !compact.matches; };
    setDefaultPicker();
    compact.addEventListener("change", setDefaultPicker);
  }

  new IntersectionObserver(([entry]) => {
    if (!entry.isIntersecting) pausePlayback();
  }).observe(frame);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) pausePlayback();
  });
  window.addEventListener("pagehide", pausePlayback);

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const watch = document.querySelector('.hero a[href="#sample-evening"]');
  watch?.addEventListener("click", (event) => {
    if (!followsHere(event)) return;
    event.preventDefault();
    if (location.hash !== "#sample-evening") history.pushState(null, "", "#sample-evening");
    card.scrollIntoView({ block: "start", behavior: "instant" });
    if (reduced.matches) {
      resetWalkthrough();
      next.focus({ preventScroll: true });
    } else {
      restartPlayback();
      playButton.focus({ preventScroll: true });
    }
  });

  card.dataset.interactive = "true";
  setScenes();
  card.querySelector(".routine-interactions").hidden = false;
  return () => selectScene(lastScene);
}

const showEveningReport = setupEveningDemo();

const transcript = document.querySelector(".routine-transcript");
const transcriptHash = "#sample-evening-transcript";
transcript.open = location.hash === transcriptHash;
window.addEventListener("hashchange", () => {
  if (location.hash === transcriptHash) transcript.open = true;
});

const setupTabs = document.querySelector(".setup-tabs");
const tabs = [...setupTabs.querySelectorAll("[role=tab]")];
const panels = tabs.map((tab) => document.getElementById(tab.getAttribute("aria-controls")));
const setupHeading = document.getElementById("setup");
setupHeading.tabIndex = -1;

function selectTab(selected) {
  for (const [index, tab] of tabs.entries()) {
    const active = tab === selected;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    panels[index].hidden = !active;
  }
}

function activeSetupTab() {
  return tabs.find((tab) => tab.getAttribute("aria-selected") === "true");
}

function setupTabForHistoryEntry() {
  const state = window.history.state;
  const saved = tabs.find((tab) => tab.id === state?.setupTab);
  if (saved) return saved;
  const active = activeSetupTab();
  window.history.replaceState({ ...state, setupTab: active.id }, "");
  return active;
}

function activateTab(selected) {
  if (location.hash === "#setup") setupTabForHistoryEntry();
  selectTab(selected);
  const hash = `#${selected.getAttribute("aria-controls")}`;
  if (window.location.hash !== hash) {
    window.history.pushState(null, "", hash);
  }
}

function reachSetupHeading() {
  setupHeading.focus({ preventScroll: true });
  setupHeading.scrollIntoView();
}

function restoreReportOrigin() {
  const returnTo = window.history.state?.reportReturn;
  const savedTab = tabs.find((tab) => tab.id === returnTo?.tab);
  if (!savedTab || !Number.isFinite(returnTo.top)) return false;
  selectTab(savedTab);
  requestAnimationFrame(() => {
    window.scrollTo({ top: returnTo.top, behavior: "instant" });
    document.getElementById(returnTo.focus)?.focus({ preventScroll: true });
  });
  return true;
}

function selectTabFromHash(initialLoad = false) {
  const hash = window.location.hash;
  if (hash === "#setup") {
    selectTab(setupTabForHistoryEntry());
    return;
  }
  const index = panels.findIndex((panel) => `#${panel.id}` === hash);
  if (index === -1) return;
  selectTab(tabs[index]);
  if (initialLoad) {
    // Let the browser finish its initial fragment and reload scroll restoration.
    window.addEventListener("pageshow", () => {
      if (window.location.hash === hash) requestAnimationFrame(reachSetupHeading);
    }, { once: true });
  } else {
    requestAnimationFrame(reachSetupHeading);
  }
}

function reachReport() {
  showEveningReport();
  const report = document.getElementById("sample-report");
  report.focus({ preventScroll: true });
  report.scrollIntoView({ block: "nearest", behavior: "instant" });
}

function followsHere(event) {
  return !event.defaultPrevented && event.button === 0 && !(event.altKey || event.ctrlKey || event.metaKey || event.shiftKey);
}

for (const [index, tab] of tabs.entries()) {
  const panel = panels[index];
  panel.setAttribute("role", "tabpanel");
  panel.setAttribute("aria-labelledby", tab.id);
  panel.tabIndex = 0;
  tab.addEventListener("click", () => activateTab(tab));
  tab.addEventListener("keydown", (event) => {
    const targets = {
      ArrowLeft: (index + tabs.length - 1) % tabs.length,
      ArrowRight: (index + 1) % tabs.length,
      Home: 0,
      End: tabs.length - 1,
    };
    if (!Object.hasOwn(targets, event.key)) return;
    event.preventDefault();
    const selected = tabs[targets[event.key]];
    activateTab(selected);
    selected.focus({ preventScroll: true });
  });
}

for (const [index, panel] of panels.entries()) {
  for (const link of document.querySelectorAll(`a[href="#${panel.id}"]`)) {
    link.addEventListener("click", (event) => {
      if (!followsHere(event)) return;
      event.preventDefault();
      activateTab(tabs[index]);
      reachSetupHeading();
    });
  }
}

for (const link of document.querySelectorAll('a[href="#sample-report"]')) {
  link.addEventListener("click", (event) => {
    if (!followsHere(event)) return;
    event.preventDefault();
    if (window.location.hash !== "#sample-report") {
      const reportReturn = { tab: activeSetupTab().id, top: window.scrollY, focus: link.id };
      window.history.replaceState({ ...window.history.state, reportReturn }, "");
      window.history.pushState(null, "", "#sample-report");
    }
    reachReport();
  });
}

setupTabs.hidden = false;
selectTab(tabs[0]);
selectTabFromHash(true);
if (location.hash === "#sample-report") showEveningReport();
window.addEventListener("pageshow", () => {
  if (location.hash === "#sample-report") reachReport();
});
window.addEventListener("hashchange", (event) => {
  if (location.hash === "#sample-report") reachReport();
  else if (new URL(event.oldURL).hash !== "#sample-report" || !restoreReportOrigin()) selectTabFromHash();
});

const feedback = document.getElementById("copy-feedback");
for (const button of document.querySelectorAll("[data-copy]")) {
  const code = document.getElementById(button.dataset.copy);
  let reset;
  button.addEventListener("click", async () => {
    clearTimeout(reset);
    feedback.textContent = "";
    try {
      await navigator.clipboard.writeText(code.textContent);
      button.textContent = "Copied!";
      feedback.textContent = "Commands copied to the clipboard.";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(code);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = "Text selected";
      feedback.textContent = "Commands selected. Copy them from your browser.";
    }
    reset = setTimeout(() => { button.textContent = "Copy commands"; }, 2500);
  });
  button.hidden = false;
}

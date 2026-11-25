const demoScenes = [
  { name: "kitchen", title: "Sunset", time: "19:34", label: "7:34 PM", lamp: "kitchen", text: "The kitchen lamp switches on." },
  { name: "living", title: "After dinner", time: "20:10", label: "8:10 PM", lamp: "living", text: "The kitchen goes dark and the living room lamp switches on." },
  { name: "tv", title: "TV time", time: "20:18", label: "8:18 PM", lamp: "living", glow: "sports", cue: "tv",
    text: "The living room lamp stays on. Optional effects add a quicker TV glow and a muffled TV sound." },
  { name: "break", title: "Kitchen break", time: "21:38", label: "9:38 PM", lamp: "kitchen", cue: "cabinet",
    text: "The TV glow fades and the kitchen lamp comes back on, with an optional cabinet sound." },
  { name: "drama", title: "Late TV", time: "21:48", label: "9:48 PM", lamp: "living", glow: "drama",
    text: "The living room lamp comes back on, with an optional slower TV glow." },
  { name: "bedtime", title: "Bedtime", time: "22:45", label: "10:45 PM", text: "The last lamp switches off." },
  { name: "morning", title: "Morning kitchen", time: "07:18", label: "7:18 AM", lamp: "kitchen", period: "dawn", cue: "kettle",
    text: "The kitchen lamp comes on briefly after dawn, with an optional kettle cue." },
  { name: "report", title: "Morning note", time: "08:00", label: "8:00 AM", period: "dawn",
    text: "The Pi saves the morning note." },
];

function setupEveningDemo() {
  const card = document.querySelector(".routine-card");
  const playButton = card?.querySelector("#scene-play");
  const restartButton = card?.querySelector("#scene-restart");
  const soundButton = card?.querySelector("#scene-sound");
  const audio = card?.querySelector("#scene-audio");
  if (!playButton || !restartButton || !soundButton || !audio) return;

  const scene = card.querySelector(".routine-scene");
  const sceneName = card.querySelector("#scene-name");
  const sceneTime = card.querySelector("#scene-time");
  const sceneText = card.querySelector("#scene-text");
  const soundState = card.querySelector("#scene-sound-state");
  const readNote = card.querySelector("#scene-read-note");
  const progress = card.querySelector("#scene-progress");
  const report = card.querySelector(".demo-report");
  const sceneButtons = [...card.querySelectorAll("[data-scene-button]")];
  const sceneDuration = 3000;
  const lastScene = demoScenes.length - 1;
  progress.max = lastScene * sceneDuration / 1000;
  let sceneIndex = null;
  let playState = "ready";
  let timer;
  let progressFrame;
  let nextSceneAt;
  let remaining = sceneDuration;
  let soundEnabled = false;

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
    readNote.hidden = !finished;
    cancelAnimationFrame(progressFrame);
    if (state === "playing") {
      card.dataset.playing = "true";
      progressFrame = requestAnimationFrame(animateProgress);
    } else {
      delete card.dataset.playing;
    }
  }

  function stopAudio() {
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
    // Media errors or browser playback restrictions do not stop the visual demo.
    audio.play().catch(() => {});
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
    sceneText.textContent = text;
    updateProgress();
    if (index === lastScene) report.dataset.current = "true";
    else delete report.dataset.current;
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

  playButton.addEventListener("click", () => {
    if (playState === "playing") {
      pausePlayback();
      return;
    }
    if (playState === "finished") return;
    if (sceneIndex === null) showScene(0);
    else playCue();
    advanceAfter(remaining);
    setPlayback("playing");
  });

  restartButton.addEventListener("click", () => {
    clearTimeout(timer);
    remaining = sceneDuration;
    showScene(0);
    advanceAfter(remaining);
    setPlayback("playing");
  });

  soundButton.addEventListener("click", () => {
    soundEnabled = !soundEnabled;
    soundButton.setAttribute("aria-pressed", String(soundEnabled));
    soundState.textContent = soundEnabled ? "on" : "off";
    playCue();
  });

  for (const button of sceneButtons) {
    const index = demoScenes.findIndex(({ name }) => name === button.dataset.sceneButton);
    if (index === -1) continue;
    button.addEventListener("click", () => {
      clearTimeout(timer);
      remaining = sceneDuration;
      showScene(index);
      if (index === lastScene) setPlayback("finished");
      else if (playState === "playing" || playState === "paused") setPlayback("paused");
      else setPlayback("ready");
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
  }).observe(card);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) pausePlayback();
  });
  window.addEventListener("pagehide", pausePlayback);

  showScene();
  setPlayback("ready");
  card.querySelector(".routine-interactions").hidden = false;
}

setupEveningDemo();

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

function activateTab(selected) {
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

function selectTabFromHash(restoreScroll = false, initialLoad = false) {
  const returnTo = window.history.state?.reportReturn;
  const savedTab = tabs.find((tab) => tab.id === returnTo?.tab);
  if (restoreScroll && savedTab && Number.isFinite(returnTo.top)) {
    selectTab(savedTab);
    requestAnimationFrame(() => window.scrollTo({ top: returnTo.top, behavior: "instant" }));
    return;
  }

  const hash = window.location.hash;
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
    if (!followsHere(event) || window.location.hash === "#sample-report") return;
    const tab = tabs.find((option) => option.getAttribute("aria-selected") === "true");
    window.history.replaceState({ ...window.history.state, reportReturn: { tab: tab.id, top: window.scrollY } }, "");
  });
}

setupTabs.hidden = false;
selectTab(tabs[0]);
selectTabFromHash(false, true);
window.addEventListener("hashchange", (event) => selectTabFromHash(new URL(event.oldURL).hash === "#sample-report"));

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

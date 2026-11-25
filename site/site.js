const demoScenes = [
  { name: "kitchen", time: "19:34", label: "7:34 PM", lamp: "kitchen", text: "The kitchen lamp switches on." },
  { name: "living", time: "20:10", label: "8:10 PM", lamp: "living", text: "The kitchen goes dark and the living room lamp switches on." },
  { name: "tv", time: "20:18", label: "8:18 PM", lamp: "living", glow: "sports", cue: "tv",
    text: "The living room window picks up a quicker TV glow, with a short, muffled TV cue if sound is on." },
  { name: "break", time: "21:38", label: "9:38 PM", lamp: "kitchen", cue: "cabinet",
    text: "The TV glow fades and the kitchen lamp comes back on, with an optional cabinet sound." },
  { name: "drama", time: "21:48", label: "9:48 PM", lamp: "living", glow: "drama",
    text: "The living room lamp comes back on, with a slower TV glow this time." },
  { name: "bedtime", time: "22:45", label: "10:45 PM", text: "The last lamp switches off." },
  { name: "morning", time: "07:18", label: "7:18 AM", lamp: "kitchen", period: "dawn", cue: "kettle",
    text: "The kitchen lamp comes on briefly after dawn, with an optional kettle cue." },
  { name: "report", time: "08:00", label: "8:00 AM", period: "dawn",
    text: "The lamps are off. The Pi saves a note about last night’s lamp activity." },
];

function setupEveningDemo() {
  const card = document.querySelector(".routine-card");
  const playButton = card?.querySelector("#scene-play");
  const soundButton = card?.querySelector("#scene-sound");
  const audio = card?.querySelector("#scene-audio");
  if (!playButton || !soundButton || !audio) return;

  const scene = card.querySelector(".routine-scene");
  const sceneTime = card.querySelector("#scene-time");
  const sceneText = card.querySelector("#scene-text");
  const progress = card.querySelector("#scene-progress");
  const report = card.querySelector(".demo-report");
  const sceneButtons = [...card.querySelectorAll("[data-scene-button]")];
  const sceneDuration = 3000;
  const lastScene = demoScenes.length - 1;
  const playLabels = { ready: "Play evening", playing: "Pause", paused: "Resume", finished: "Replay" };
  let sceneIndex = 0;
  let playState = "ready";
  let timer;
  let nextSceneAt;
  let remaining = sceneDuration;
  let soundEnabled = false;

  function setPlayback(state) {
    playState = state;
    playButton.textContent = playLabels[state];
    if (state === "playing") card.dataset.playing = "true";
    else delete card.dataset.playing;
  }

  function stopAudio() {
    audio.pause();
    audio.currentTime = 0;
  }

  function playCue(cue = demoScenes[sceneIndex].cue) {
    stopAudio();
    if (!soundEnabled || !cue) return;
    const source = `assets/demo-${cue}.mp3`;
    if (audio.getAttribute("src") !== source) audio.src = source;
    audio.volume = 0.12;
    // Media errors or browser playback restrictions do not stop the visual demo.
    audio.play().catch(() => {});
  }

  function showScene(index) {
    sceneIndex = index;
    const { name, time, label, text, lamp = "none", glow = "none", period = "evening", cue } = demoScenes[index];
    Object.assign(scene.dataset, { scene: name, lamp, glow, period, cue: cue ?? "none" });
    sceneTime.dateTime = time;
    sceneTime.textContent = label;
    sceneText.textContent = text;
    progress.value = index + 1;
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
      clearTimeout(timer);
      remaining = Math.max(0, nextSceneAt - performance.now());
      stopAudio();
      setPlayback("paused");
      return;
    }
    if (playState === "paused") {
      playCue();
    } else {
      remaining = sceneDuration;
      showScene(0);
    }
    setPlayback("playing");
    advanceAfter(remaining);
  });

  soundButton.addEventListener("click", () => {
    soundEnabled = !soundEnabled;
    soundButton.setAttribute("aria-pressed", String(soundEnabled));
    playCue();
  });

  for (const button of sceneButtons) {
    const index = demoScenes.findIndex(({ name }) => name === button.dataset.sceneButton);
    if (index === -1) continue;
    button.addEventListener("click", () => {
      clearTimeout(timer);
      remaining = sceneDuration;
      if (index === lastScene) setPlayback("finished");
      else if (playState === "playing" || playState === "paused") setPlayback("paused");
      else setPlayback("ready");
      showScene(index);
    });
  }

  showScene(0);
  setPlayback("ready");
  card.querySelector(".routine-interactions").hidden = false;
}

setupEveningDemo();

const setupTabs = document.querySelector(".setup-tabs");
const tabs = [...setupTabs.querySelectorAll("[role=tab]")];
const panels = tabs.map((tab) => document.getElementById(tab.getAttribute("aria-controls")));

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

function selectTabFromHash() {
  const index = panels.findIndex((panel) => `#${panel.id}` === window.location.hash);
  if (index === -1) return;
  selectTab(tabs[index]);
  panels[index].scrollIntoView();
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
setupTabs.hidden = false;
selectTab(tabs[0]);
selectTabFromHash();
window.addEventListener("hashchange", selectTabFromHash);

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

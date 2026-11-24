const evening = {
  kitchen: { time: "19:34", label: "7:34 PM", text: "The kitchen lamp switches on." },
  living: { time: "20:10", label: "8:10 PM", text: "The kitchen goes dark and the living room lamp switches on." },
  bedtime: { time: "22:45", label: "10:45 PM", text: "The last lamp switches off." },
};

const scene = document.querySelector(".routine-scene");
const sceneTime = document.querySelector("#scene-time");
const sceneText = document.querySelector("#scene-text");
const sceneButtons = [...document.querySelectorAll("[data-scene-button]")];

for (const button of sceneButtons) {
  button.addEventListener("click", () => {
    const name = button.dataset.sceneButton;
    const setting = evening[name];
    scene.dataset.scene = name;
    sceneTime.dateTime = setting.time;
    sceneTime.textContent = setting.label;
    sceneText.textContent = setting.text;
    for (const option of sceneButtons) {
      option.setAttribute("aria-pressed", String(option === button));
    }
  });
}
document.querySelector(".routine-steps").hidden = false;

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

for (const [index, tab] of tabs.entries()) {
  const panel = panels[index];
  panel.setAttribute("role", "tabpanel");
  panel.setAttribute("aria-labelledby", tab.id);
  panel.tabIndex = 0;
  tab.addEventListener("click", () => selectTab(tab));
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
    selectTab(selected);
    selected.focus();
  });
}
selectTab(tabs[0]);
setupTabs.hidden = false;

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
